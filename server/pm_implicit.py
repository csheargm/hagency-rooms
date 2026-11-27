"""Talking to @pm without "@pm" in a project room (docs/adr/0022-pm-understands-with-owner-ai.md).

A message that doesn't name @pm still counts as for @pm when
  (a) it replies to one of @pm's messages,
  (b) its sender is who @pm was just talking with (a question of mine waits for them, or I answered
      them within FOLLOW_S),
  (c) its sender is the only person in the room, or
  (d) the understanding step (the room owner's agent, or the host's model) judges it is: only when one
      is available, only for messages the phrase rules read as an intent or that look like a request,
      and at most PER_MINUTE such reads a room, so ordinary chat never floods the owner's agent.
Never when it is plainly for someone else (a mention, a name, an agent's name, an edit). Whatever
@pm does on such a message is low-impact (status, the plan, help, about), or ASKED first with the 👍
flow: stop, a mode change, a plan or a change to it, a repo, a retry (the same goes for a named message
whose meaning only the model found: pm_understand.apply). An explicit "@pm stop" stays instant (pm_chat.py).
Mixed into loop.Loop through pm_understand.Understanding (say, act, think, pending, offer_thumb, ...).
"""
import logging
import re
import time

from nio import MatrixRoom, RoomGetEventResponse, RoomMessageText

from common import intents
from common import room_state
from server import pm_brain
from server import project_rooms
from server.pm_address import without_pill, without_quote
from server.pm_chat import CONFIRM_S, LEVEL_WORDS, ONLY_MODERATORS

log = logging.getLogger("loop")
FOLLOW_S = 180
PER_MINUTE = 6                    # reads of unaddressed messages, per room
MINUTE_S = 60
REPLIES = "it replies to one of @pm's messages"
TALKING = "@pm was just talking with them"
ALONE = "they are the only person in the room"
MAYBE = "nobody was named: it may be for @pm, or for someone else in the room"
REQUEST_RE = re.compile(r"\?\s*$|^\W*(please|pls|can|could|would|will|let'?s|show|tell|what|where|how|when|why|who|"
                        r"is|are|do|does|did|start|stop|make|plan|build)\b", re.I)
AGENT_NAME_RE = re.compile(r"(?<![\w-])@?[a-z0-9._=-]+-agent\b|\b\w+'s agent\b", re.I)
LEADING_NAME_RE = re.compile(r"^\W*@?([\w.=-]+)\s*[:,]")
EXACT = (intents.SET_REPO, intents.RETRY)
ASKED_FIRST = (intents.OFF, intents.ASSIST, intents.AUTONOMOUS, intents.PLAN, intents.SET_REPO, intents.RETRY)
MODERATORS_ONLY = (intents.ASSIST, intents.AUTONOMOUS, intents.SET_REPO, intents.RETRY)
LOW_IMPACT = (intents.STATUS, intents.SHOW_PLAN, intents.HELP, intents.ABOUT, intents.START, intents.WEB,
              intents.CONSOLE)
TO_CONFIRM = " Click 👍 below to confirm."
HOW_TO_CONFIRM = "To confirm, click 👍 on my question above, or reply to it with yes."
QUESTIONS = {intents.OFF: "Do you want me to stop? No new work would be handed out and nothing merged.",
             intents.PLAN: "Do you want me to turn this discussion into a plan of issues?",
             intents.SET_REPO: "Should this room work on **{repo}**?",
             intents.RETRY: "Should #{issue} go back in the queue?"}
REDRAFT = "Should I redraft the plan above with that?"


def mode_question(level: str) -> str:
    return f"Do you want me to switch to **{level}** ({LEVEL_WORDS[level].split(': ', 1)[1]})?" + TO_CONFIRM


def unnamed_ask(waiting: dict) -> bool:
    """A question of mine that a message without "@pm" led to (any kind: a stop, a mode, a setting, ...):
    a bare "yes" never confirms it, only a 👍, a reply to it or "@pm yes"."""
    return waiting.get("kind") == "implicit" or waiting.get("unnamed") is True


class Implicit:
    def note_talk(self, room_id: str, person: str) -> None:
        """@pm is answering this person here (an addressed message): their next words may be for me too."""
        self.__dict__.setdefault("_talks", {})[room_id] = (person, time.monotonic())

    def yes_counts(self, room_id: str, reply_to: str | None, named: bool) -> bool:
        """A typed OK confirms my question when it replies to it, or replies to nothing; for a question I
        asked without being named, only if it names me too ("@pm yes"): a stray "yes" said to someone
        else mustn't switch a room to autonomous, nor change a setting (GPT review)."""
        waiting = self.pending.get(room_id) or {}
        return reply_to == waiting.get("event_id") or (reply_to is None and (named or not unnamed_ask(waiting)))

    async def heard(self, room: MatrixRoom, event: RoomMessageText, text: str | None, reply_to: str | None,
                    voice: bool = False) -> bool:
        """True: the message didn't name me (looked at in the background: reading it may take network
        calls, and the sync loop, with a "@pm stop" behind it, never waits). Else @pm talks with its sender."""
        if text is not None:
            self.note_talk(room.room_id, event.sender)
            return False
        if not self.background(self.unaddressed_job(room, event, reply_to, voice, self.generation.get(room.room_id, 0))):
            log.warning("too many messages being understood; skipped %s's unaddressed one", event.sender)
        return True                           # (pm_understand.background: bounded; it may not be for me at all)

    async def unaddressed_job(self, room: MatrixRoom, event: RoomMessageText, reply_to: str | None, voice: bool,
                              gen: int) -> None:
        try:
            await self.on_unaddressed(room, event, without_quote(event.body), reply_to, voice, gen)
        except (RuntimeError, OSError) as e:  # SendError / network: the log says; the room keeps working
            log.error("couldn't look at %s's message: %s", event.sender, e)
        except Exception:                     # noqa: BLE001 — a background task: never fail silently
            log.exception("unexpected error looking at %s's message", event.sender)

    def for_someone_else(self, room: MatrixRoom, event: RoomMessageText, body: str) -> bool:
        content = event.source.get("content", {})
        rel = content.get("m.relates_to")
        if isinstance(rel, dict) and rel.get("rel_type") == "m.replace":
            return True                       # an edit is never a new request
        mentions = content.get("m.mentions")
        ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if (isinstance(ids, list) and ids) or without_pill(body, self.user_id)[1] or AGENT_NAME_RE.search(body):
            return True
        m = LEADING_NAME_RE.match(body)
        names = {n.lower() for u, info in room.users.items() if u != self.user_id
                 for n in (u.split(":")[0].lstrip("@"), getattr(info, "display_name", None)) if isinstance(n, str)}
        return bool(m and m.group(1).lower() in names)

    async def why_for_pm(self, room: MatrixRoom, sender: str, reply_to: str | None) -> str | None:
        """(a), (b) or (c) of the module docstring, said for the understanding step; None: none of them."""
        if reply_to:
            r = await self.client.room_get_event(room.room_id, reply_to)
            if isinstance(r, RoomGetEventResponse) and r.event.sender == self.user_id:
                return REPLIES
        waiting = self.pending.get(room.room_id) or {}
        talk = self.__dict__.get("_talks", {}).get(room.room_id)
        if (waiting.get("by") == sender and time.time() < waiting.get("until", 0)) or (
                talk and talk[0] == sender and time.monotonic() - talk[1] < FOLLOW_S):
            return TALKING
        people = [u for u in room.users if self.is_person(room, u)]
        return ALONE if people == [sender] else None

    async def brain_why_not(self, room: MatrixRoom) -> str | None:
        """None: the understanding step can read it; else why not ("" when this server runs without one)."""
        if pm_brain.BRAIN == "off":
            return ""
        return (await self.owner_agent(room))[1] or None if pm_brain.BRAIN == "owner" else None   # pm_owner_brain.py

    def spend_read(self, room_id: str) -> bool:
        """One of this room's PER_MINUTE reads of unaddressed messages, if any is left."""
        return self.spend(((("read", room_id), ((PER_MINUTE, MINUTE_S),)),))   # pm_owner_brain.py

    def asked_them(self, room_id: str, sender: str) -> bool:
        """A question I asked without being named waits for this very person."""
        waiting = self.pending.get(room_id) or {}
        return unnamed_ask(waiting) and waiting.get("by") == sender and time.time() < waiting.get("until", 0)

    async def on_unaddressed(self, room: MatrixRoom, event: RoomMessageText, body: str, reply_to: str | None,
                             voice: bool = False, gen: int = 0) -> None:
        """A message without "@pm" (pm_chat.on_human): for me only by the rules above."""
        sender, rid = event.sender, room.room_id
        if voice or self.is_private_chat(room) or project_rooms.is_front_door(room) or not body.strip() \
                or self.for_someone_else(room, event, body):
            return                            # the Lobby and Welcome room: only "@pm" counts there
        if intents.is_yes(body) and self.asked_them(rid, sender):
            await self.say(rid, HOW_TO_CONFIRM)   # a bare "yes" alone doesn't count (yes_counts); never silence
            return
        how = await self.why_for_pm(room, sender, reply_to)
        intent = intents.classify(body)
        if intent in EXACT:                   # a repo name or an issue number: never left to a model
            if how is not None:
                await self.act_implicit(room, sender, intent, body, gen)
            return
        if intent == intents.UNKNOWN and reply_to and reply_to == (self.pending.get(rid) or {}).get("event_id") \
                and await self.ask_redraft(room, sender, gen):
            return                            # a change asked for in reply to the plan waiting for ✅: 👍 first
        if how is None and intent == intents.UNKNOWN and not REQUEST_RE.search(body):
            return                            # ordinary chat: nothing is read, nothing sent
        why = await self.brain_why_not(room) if self.spend_read(rid) else ""
        if why is None:
            await self.decide(room, sender, body, intent, reply_to, gen, how=how or MAYBE)
        elif how is not None and self.generation.get(rid, 0) == gen:
            await self.fall_back(room, sender, intent, body, how, why, gen)

    async def after_implicit(self, room: MatrixRoom, sender: str, decision: object, text: str, how: str, gen: int) -> None:
        """What the understanding step made of an unaddressed message (pm_understand.decide)."""
        if isinstance(decision, tuple):
            await self.apply(room, sender, *decision, text, None, gen, named=False)
        elif how != MAYBE:                    # (a)–(c) without a model: the phrase rules
            await self.fall_back(room, sender, intents.classify(text), text, how, decision or "", gen)

    async def fall_back(self, room: MatrixRoom, sender: str, intent: str, text: str, how: str, why: str,
                        gen: int) -> None:
        """No model read it. A reply to my message was plainly said to me: never silence ("didn't catch
        that", with why). Otherwise only what the rules know, quietly (it may be for someone else)."""
        if intent == intents.UNKNOWN and (self.pending.get(room.room_id) or {}).get("kind") == "plan":
            await self.ask_redraft(room, sender, gen)   # asked first, never redrafted on a guess, even when
            return                            # it couldn't ask (GPT re-check: by_rules would redraft)
        if how == REPLIES and intent == intents.UNKNOWN:
            await self.by_rules(room, sender, text, intent, why)
        else:
            await self.act_implicit(room, sender, intent, text, gen)

    async def act_implicit(self, room: MatrixRoom, sender: str, intent: str, text: str, gen: int,
                           named: bool = False) -> None:
        """Only low-impact things at once; anything else is asked first with a 👍 (named: it said "@pm",
        but the model, not the phrase rules, read it as this: pm_understand.apply)."""
        if intent in intents.SETTINGS:
            await self.ask_setting(room, sender, intent, gen, named)  # pm_understand.py: one 👍
        elif intent == intents.PLAN and (self.pending.get(room.room_id) or {}).get("kind") == "plan":
            await self.ask_redraft(room, sender, gen)
        elif intent in ASKED_FIRST:
            await self.ask_implicit(room, sender, intent, text, gen, named)
        elif intent == intents.SHOW_PLAN:
            await self.show_plan(room.room_id, approve=False)  # shown only: an approval is asked by "@pm show the plan"
        elif intent in LOW_IMPACT:
            await self.act(room, sender, intent)

    async def ask_implicit(self, room: MatrixRoom, sender: str, intent: str, text: str, gen: int,
                           named: bool = False) -> None:
        """Ask before doing it: one 👍 from them makes sure (pm_understand.ask_once: never over another question)."""
        rid = room.room_id
        if intent in MODERATORS_ONLY and not await room_state.is_moderator(self.client, room, sender):
            if named:
                await self.say(rid, ONLY_MODERATORS)
            return                            # unnamed: nothing they could confirm; stay out of their conversation
        found = {"repo": intents.repo_named(text), "issue": intents.issue_named(text)}
        if (intent == intents.SET_REPO and not found["repo"]) or (intent == intents.RETRY and found["issue"] is None):
            return                            # a repo or an issue number is never guessed
        question = mode_question(intent) if intent in (intents.ASSIST, intents.AUTONOMOUS) else QUESTIONS[intent].format(**found) + TO_CONFIRM
        await self.ask_once(rid, sender, gen, question, {"kind": "implicit", "intent": intent, **found}, named)

    async def ask_redraft(self, room: MatrixRoom, sender: str, gen: int) -> bool:
        """A change to the plan waiting for ✅, not in a named message the rules read: ask on the side (the
        plan question stays where it is); a 👍 on it redrafts (redraft). False: no plan, or not theirs to change."""
        rid, plan = room.room_id, self.pending.get(room.room_id)
        if not plan or plan.get("kind") != "plan" or not await room_state.is_moderator(self.client, room, sender):
            return False
        if not self.still(rid, gen, plan):
            return True
        event_id = await self.say(rid, REDRAFT + TO_CONFIRM)
        if self.still(rid, gen, plan):        # a stop, or the plan filed, while I asked: nothing to redraft
            plan["redraft"] = {"event_id": event_id, "by": sender, "until": time.time() + CONFIRM_S}
            await self.offer_thumb(rid, event_id)
        return True

    async def redraft(self, room: MatrixRoom, sender: str, plan: dict) -> None:
        """Their 👍 on ask_redraft's question (pm_chat.on_reaction): only theirs, in time, that plan still waiting."""
        asked = plan.get("redraft") or {}
        if asked.get("by") == sender and time.time() < asked.get("until", 0) and self.pending.get(room.room_id) is plan:
            self.note_outcome(asked["event_id"], "approved")
            await self.revise_plan(room, sender)

    async def do_implicit(self, room: MatrixRoom, sender: str, p: dict, gen: int | None = None) -> None:
        """Their 👍 on ask_implicit's question (pm_chat.confirm): the usual checks run again now."""
        if p["intent"] == intents.SET_REPO:
            await self.set_repo(room, sender, p["repo"])
        elif p["intent"] == intents.RETRY:
            await self.retry(room, sender, p["issue"])
        else:
            await self.act(room, sender, p["intent"], gen=gen)
