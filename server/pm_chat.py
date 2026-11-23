"""How @pm answers people. They talk in plain words ("@pm take it all the way", "@pm stop",
"@pm where are we?"); intents.py turns that into one of a few fixed intents, and this code
decides what is allowed:
  - stop: anyone in the room, at once, no confirmation (the safe direction is always open);
  - assist: the room owner or a maintainer;
  - autonomous: the owner or a maintainer, at once (they asked in plain words; "stop" undoes it);
  - turning the discussion into issues: the plan needs a ✅ from whoever asked for it.
`!help`, `!status`, `!level …` still work as shortcuts for people who like commands.
Mixed into loop.Loop (client, user_id, say, level, boards, agents_in, start_plan, file_plan).
"""
import asyncio
import re
import time

from nio import MatrixRoom, ReactionEvent, RoomMessageText

from common import github
from common import customize
from common import intents
import logging

from server import pm_brain
from server.pm_address import Addressing, without_quote  # noqa: F401 - without_quote: other modules import it from here
from common.saved_dict import SavedDict
from common import room_state
from server import pm_paused

log = logging.getLogger("loop")
CONFIRM_S = 300
FOLLOW_SETTINGS = {intents.FOLLOW_ALWAYS: "always", intents.FOLLOW_ASK: "ask", intents.FOLLOW_NEVER: "never"}
NOTE_WORDS = 6               # a longer message to @pm that isn't a command is a requirement, not noise
LEVEL_WORDS = {"off": "stopped: no new work is handed out and nothing is merged",
               "assist": "assist: agents build and review; a person merges each change",
               "autonomous": "autonomous: agents build, review and merge on their own"}
WEB_ANSWER = ("There's no web switch to change: when you ask your own agent something, it runs your CLI "
              "with your own settings, web included, in every room. Work I hand out never has the web. To "
              "limit your agent, use your CLI's own settings (a deny rule, or web_search = \"disabled\").")
HELP = ("Talk to me in plain words, for example:\n"
        "- **@pm take it all the way**: agents build, review and merge on their own (owner or maintainer)\n"
        "- **@pm let us approve each change**: agents stop at each change for a person to merge\n"
        "- **@pm stop**: no new work and no merges, right away (anyone)\n"
        "- **@pm where are we?**: what's happening now\n"
        "- **@pm let's build it**: your agent turns this discussion into issues; 👍 to open them\n"
        "- **@pm show the plan**: the plan waiting for your 👍\n"
        "- **@pm send me my console link**: settings in a page (also the Hagency panel in this room)\n"
        "- **@pm retry #9**: a paused issue goes back in the queue (owner or maintainer)\n"
        "- **@pm this room works on owner/name**: the GitHub repo for this room (one room per repo); "
        "**@pm remove the repo** takes it off\n"
        "- **@pm make me a project room**, **@pm find a project**, **@pm ask to join owner/name**\n"
        "- **@pm invite link**: a link that lets one person in, even without an account yet (owner or maintainer)\n"
        "- **@pm make this room listed** (or private, or open): who can find and join it (owner or maintainer)\n"
        "The pinned **Project status** and the **Hagency** panel show progress live.")

# The answer to "what should we do first?" (intents.START): the path from here, for this room.
REPO_STEP = ("**Pick the GitHub repo** this room works on, in the Hagency panel above "
             "(or say \"@pm this room works on owner/name\").")
FIRST_STEPS = ("Here's how a project goes:\n1. {repo}\n"
               "2. **Say what you want built**, here in the chat. People and agents can talk it through.\n"
               "3. Say **@pm let's build it**: your agent turns the discussion into issues for your 👍, then agents "
               "build them and open pull requests.\nSay **@pm help** for everything I understand.")


def replied_to(event: RoomMessageText) -> str | None:
    rel = event.source.get("content", {}).get("m.relates_to", {})
    reply = rel.get("m.in_reply_to", {}) if isinstance(rel, dict) else {}
    return reply.get("event_id") if isinstance(reply, dict) else None


class Chat(Addressing):
    generation: dict                     # room → bumped by "stop" (cancels in-flight decisions)
    pending: dict           # room → the autonomous switch waiting for a ✅

    async def on_human(self, room: MatrixRoom, event: RoomMessageText) -> None:
        body, sender = without_quote(event.body), event.sender
        voice = getattr(event, "voice", False)   # pm_voice.py: words I transcribed, not typed
        if body.startswith("!") and not voice:
            await self.command(room, sender, body.split())
            return
        text = self.addressed_text(event, room)
        reply_to = replied_to(event)
        waiting = self.pending.get(room.room_id, {}).get("event_id")
        said = text if text is not None else (body if reply_to and reply_to == waiting else None)
        if voice and waiting and said is not None and intents.is_approval(said):
            await self.say(room.room_id, "🎙 To confirm, click 👍 on my question above.")   # a click, never a transcript
            return
        if not voice and (intents.is_yes(body) or (said is not None and intents.is_approval(said))):
            if (reply_to in (None, waiting)) and await self.confirm(room, sender):
                return                # "yes", "@pm ok go ahead", or "sounds good" in a reply to the question
            if text is not None and (waiting or intents.is_yes(text)):
                await self.answer_stale_ok(room.room_id, reply_to, sender)   # an OK, but not one that counts
                return                # (nothing waiting + "let's build it" goes on to start a plan)
        if text is None:
            return
        if customize.GO_BACK.search(text) and not voice:   # works even when that person's own version is broken
            local, _, server = sender.lstrip("@").partition(":")
            changed = server == self.user_id.partition(":")[2] and customize.go_back(local)   # only this server's people
            await self.say(room.room_id, "↩️ Your agent is going back to the official version." if changed
                           else "Your agent already runs the official version.")
            return
        intent = intents.classify(text)
        if voice and intent in (intents.SET_REPO, intents.RETRY):
            await self.say(room.room_id, "🎙 Please type that one: repo names and issue numbers must be exact.")
            return
        if intent in intents.SETTINGS and (voice or not intents.plainly_says(text, intent)):
            await self.ask_setting(room, sender, intent)            # pm_understand.py: one 👍 to be sure
            return
        if voice and intent in (intents.ASSIST, intents.AUTONOMOUS):
            await self.ask_mode(room, sender, intent)
            return
        if intent == intents.SET_REPO:        # a repo name is exact: never left to a model
            await self.set_repo(room, sender, intents.repo_named(text) or "")
            return
        if intent == intents.RETRY:           # so is an issue number
            n = intents.issue_named(text)
            if n is None and re.search(r"\d", text):
                await self.say(room.room_id, "Which issue? Say it like \"@pm retry #9\".")   # never guess
            else:
                await self.retry(room, sender, n)
            return
        if intent == intents.OFF or pm_brain.BRAIN == "off":
            await self.by_rules(room, sender, text, intent)    # "stop" never waits for a model
            return
        # The model can take seconds: decide in the background so the sync loop (and a "stop"
        # right behind this message) is never held up; a stop meanwhile cancels the decision.
        gen = self.generation.get(room.room_id, 0)
        job = asyncio.create_task(self.decide(room, sender, text, intent, reply_to, gen, voice))
        self.busy.add(job)
        job.add_done_callback(self.busy.discard)

    async def by_rules(self, room: MatrixRoom, sender: str, text: str, intent: str) -> None:
        """The phrase rules (intents.py): the fast path for "stop", and the fallback for the model."""
        if intent == intents.UNKNOWN and await self.revise_plan(room, sender):
            return
        if intent == intents.UNKNOWN and len(text.split()) >= NOTE_WORDS:
            await self.say(room.room_id, "Noted. When you're ready, say \"let's build it\" and I'll turn the "
                                         "discussion into work.")    # it is part of the discussion that gets planned
            return
        await self.act(room, sender, intent)

    async def revise_plan(self, room: MatrixRoom, sender: str) -> bool:
        """A plan waits for ✅ and the owner/maintainer says what to change: plan again with it."""
        p = self.pending.get(room.room_id)
        if not p or p.get("kind") != "plan" or not await room_state.is_moderator(self.client, room, sender):
            return False
        self.pending.pop(room.room_id)
        await self.say(room.room_id, "Updating the plan with that…")
        await self.start_plan(room, sender)
        return True

    async def offer_thumb(self, room_id: str, event_id: str, key: str = "👍") -> None:
        """Put a 👍 (or another button) under my own question, so answering is one click on it (my own
        reaction never counts: is_person skips me). Best effort on purpose: typing still works."""
        try:
            await self.client.room_send(room_id, "m.reaction", {"m.relates_to": {
                "rel_type": "m.annotation", "event_id": event_id, "key": key}})
        except Exception as e:                 # noqa: BLE001 — a missing shortcut must not lose the question
            log.warning("couldn't add the %s shortcut in %s: %s", key, room_id, e)

    async def on_reaction(self, room: MatrixRoom, event: ReactionEvent) -> None:
        wanted = self.pending.get(room.room_id, {}).get("event_id")
        if intents.is_confirm_reaction(event.key) and event.reacts_to == wanted:
            await self.confirm(room, event.sender)

    async def act(self, room: MatrixRoom, sender: str, intent: str) -> None:
        rid = room.room_id
        if intent == intents.OFF:                                 # never gated: stopping is always safe
            self.generation[rid] = self.generation.get(rid, 0) + 1    # drop decisions still being thought about
            self.pending.pop(rid, None)
            await self.say(rid, await self.change_level(rid, "off") or
                           "Stopped. No new work is handed out and nothing is merged; steps already "
                           "running finish. Tell me when to continue.")
        elif intent in (intents.ASSIST, intents.AUTONOMOUS) and not await room_state.is_moderator(self.client, room, sender):
            await self.say(rid, "Only the room owner or a maintainer can change how I run this project.")
        elif intent == intents.ASSIST:
            await self.say(rid, await self.change_level(rid, "assist") or
                           "OK: agents build and review, and each change waits for a person to merge it."
                           + await pm_paused.after_switch(self, rid, "assist"))
        elif intent == intents.AUTONOMOUS:   # an explicit phrase from the owner/maintainer: no extra ✅
            await self.say(rid, await self.change_level(rid, "autonomous") or
                           "Running on my own now: agents build, review and merge without asking. "
                           "Say \"stop\" anytime." + await pm_paused.after_switch(self, rid, "autonomous"))
        elif intent == intents.WEB:          # ADR 0006: nothing to switch; say so instead of pretending
            await self.say(rid, WEB_ANSWER)
        elif intent == intents.CONSOLE:
            await self.send_console_link(room, sender)
        elif intent in FOLLOW_SETTINGS:      # the sender's own setting only (pm_follow.py)
            await self.set_follow(room, sender, FOLLOW_SETTINGS[intent])
        elif intent == intents.SHOW_PLAN:
            await self.show_plan(rid)
        elif intent == intents.STATUS:
            await self.say(rid, await self.status_text(rid))
        elif intent == intents.PLAN and not await room_state.is_moderator(self.client, room, sender):
            who = sender.split(":")[0].lstrip("@")         # a contributor asks; the owner decides
            event_id = await self.say(rid, f"{who} would like to turn this discussion into work. Room owner or "
                                           "a maintainer: click 👍 below and I'll plan it with your agent.")
            self.pending[rid] = {"kind": "plan_request", "event_id": event_id, "until": time.time() + CONFIRM_S}
            await self.offer_thumb(rid, event_id)
        elif intent == intents.PLAN:
            await self.start_plan(room, sender)
        elif intent == intents.HELP:
            await self.say(rid, HELP)
        elif intent == intents.START:
            repo = await room_state.repo_of(self.client, rid)
            await self.say(rid, FIRST_STEPS.format(repo=f"✓ This room works on **{repo}**." if repo else REPO_STEP))
        elif intent == intents.ABOUT:
            await self.say(rid, "I'm the Product Manager: a coordinator, with no AI model of my own. I turn your "
                                "discussion into work and keep it moving; the thinking and building are done by "
                                "your own agents, on their owners' models and accounts.")
        else:
            await self.say(rid, "I didn't catch that. You can say: **show the plan** · **let's build it** · "
                                "**let us approve each change** · **take it all the way** · **stop** · "
                                "**where are we?**")

    async def confirm(self, room: MatrixRoom, sender: str) -> bool:
        """Apply the pending autonomous switch if a moderator confirms in time."""
        p = self.pending.get(room.room_id)
        if not p or time.time() > p["until"]:
            return False
        if p.get("kind") != "plan_request" and sender != p.get("by"):
            return False              # only the person who asked confirms (a stray "yes" from anyone else doesn't)
        if p.get("kind") not in ("bring", "setting") and not await room_state.is_moderator(self.client, room, sender):
            return False              # authority re-checked now (bringing your OWN agent needs none)

        self.pending.pop(room.room_id)
        self.note_outcome(p.get("event_id"), "approved")
        if p.get("kind") == "plan_request":
            await self.start_plan(room, sender)                 # planned with the confirming owner's agent
        elif p.get("kind") == "plan":
            await self.file_plan(room, p, sender)               # pm_plan.py
        elif p.get("kind") == "setting":                       # your own agent's setting, asked with 👍
            await self.act(room, sender, p["intent"])
        elif p.get("kind") == "bring":                          # pm_follow.py: "bring your agent? ✅"
            await self.invite_agent(room.room_id, p["agent"], "Your agent is on its way in.")
        else:                                 # a mode switch asked about with ✅ (ask_mode)
            level = p.get("level", "autonomous")
            await self.say(room.room_id, await self.change_level(room.room_id, level) or
                           f"OK — {LEVEL_WORDS[level]}. Say \"stop\" anytime."
                           + await pm_paused.after_switch(self, room.room_id, level))
        return True

    def outcomes(self) -> dict:
        """What became of each question I asked (event id → "approved — opened #7", ...), on disk."""
        if "_outcomes" not in self.__dict__:
            path = getattr(self, "board_file", None)
            self._outcomes = SavedDict(path.with_name(path.stem + "-outcomes.json")) if path else {}
        return self._outcomes

    def note_outcome(self, event_id: str | None, what: str) -> None:
        if event_id:
            self.outcomes()[event_id] = what

    async def answer_stale_ok(self, room_id: str, reply_to: str | None, sender: str = "") -> None:
        """An OK for something already settled (or for nothing): say what happened, never "didn't catch"."""
        done = self.outcomes().get(reply_to) if reply_to else None
        waiting = self.pending.get(room_id, {})
        if done:
            first = f"That one is already taken care of: {done}."
        elif waiting.get("by") and waiting["by"] != sender:
            first = f"That's waiting for {waiting['by'].split(':')[0].lstrip('@')} to approve."
        else:
            first = "Nothing is waiting for your OK right now."
        await self.say(room_id, f"{first} {await self.status_text(room_id)}")

    async def status_text(self, room_id: str) -> str:
        agents = ", ".join(a.split(":")[0][1:] for a in self.agents_in(self.client.rooms[room_id])) or "none"
        board = self.boards.get(room_id, {}).get("event_id")
        link = f" Live progress: [Project status](https://matrix.to/#/{room_id}/{board})." if board else ""
        return f"Mode: {LEVEL_WORDS[await self.level(room_id)]}. Agents here: {agents}.{link}"

    async def command(self, room: MatrixRoom, sender: str, words: list[str]) -> None:
        cmd, args = words[0].lower(), words[1:]
        if cmd == "!help":
            await self.say(room.room_id, HELP)
        elif cmd == "!status":
            await self.say(room.room_id, await self.status_text(room.room_id))
        elif cmd == "!level" and not args:
            await self.say(room.room_id, f"loop level: {await self.level(room.room_id)} for "
                                         f"{await room_state.repo_of(self.client, room.room_id) or 'no repo yet'}")
        elif cmd == "!level":
            # The expert shortcut: an explicit level needs no ✅, but still needs a moderator.
            if args[0] not in github.LEVELS or len(args) != 1:
                await self.say(room.room_id, f"usage: !level {'|'.join(github.LEVELS)}")
            elif args[0] != "off" and not await room_state.is_moderator(self.client, room, sender):
                await self.say(room.room_id, "only room moderators can change the loop level")
            else:
                await self.say(room.room_id, await self.change_level(room.room_id, args[0])
                               or f"loop level: {args[0]} for {await room_state.repo_of(self.client, room.room_id)}"
                               + await pm_paused.after_switch(self, room.room_id, args[0]))
