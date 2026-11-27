"""@pm's understanding path: a message addressed to @pm that the phrase rules don't recognize goes to
the model (pm_brain.py, or with PM_BRAIN=owner the room owner's agent: pm_owner_brain.py) with the
room's context, in the background so "stop" is never held up. Whatever the model picks that changes
anything is only its reading of the words, so @pm asks a 👍 first; read-only answers come at once.
Messages without "@pm" that may still be for me: pm_implicit.py. A stop, from any path, invalidates
every thought in flight and the question waiting (invalidate). Mixed into loop.Loop alongside
pm_chat.Chat (say, act, confirm, pending, ...).
"""
import asyncio
import logging
import time

from nio import MatrixRoom, RoomGetEventResponse

from common.pm_actions import INTENT_OF as BRAIN_ACTIONS
from server import pm_brain
from common import room_state
from server.pm_chat import CONFIRM_S, LEVEL_WORDS, ONLY_MODERATORS, without_quote
from server.pm_implicit import SPECULATIVE, TO_CONFIRM, Implicit, mode_question
from server.pm_owner_brain import SIMPLE, OwnerBrain

log = logging.getLogger("loop")
SETTING_QUESTIONS = {"follow_always": "Bring your agent along whenever you join a project room?",
                     "follow_ask": "Ask you first before bringing your agent into a room?",
                     "follow_never": "Never bring your agent along?"}
ANSWER_FIRST = "Please answer my question above first; then ask me again."
CLICK_TO_APPROVE = "To approve it, click 👍 on my question above (or type \"ok\" in reply)."
REPLACEABLE = ("setting", "mode", "implicit")    # my small 👍 questions: the same person may ask again
MAX_JOBS = 12                 # understanding jobs at once (each may wait ~50 s for an agent); past it, the rules
TOO_MANY = "I'm reading a lot of messages just now," + SIMPLE


class Understanding(OwnerBrain, Implicit):
    def invalidate(self, room_id: str) -> None:
        """Every stop, whatever triggered it ("@pm stop", a 👍 on "stop?", !level off, the console): thoughts
        still in flight are dropped (generation) and so is the question waiting. Before any await."""
        self.generation[room_id] = self.generation.get(room_id, 0) + 1
        self.pending.pop(room_id, None)

    def still(self, room_id: str, gen: int, waiting: object) -> bool:
        """No stop since gen, and the question waiting is still that one (or still none)."""
        return self.generation.get(room_id, 0) == gen and self.pending.get(room_id) is waiting

    def background(self, job_coro) -> bool:
        """Run one understanding job beside the sync loop; False (and not run) when MAX_JOBS already are."""
        if sum(1 for j in self.busy if getattr(j, "understanding", False)) >= MAX_JOBS:
            job_coro.close()
            return False
        job = asyncio.create_task(job_coro)
        job.understanding = True
        self.busy.add(job)
        job.add_done_callback(self.busy.discard)
        return True

    async def understand_later(self, room: MatrixRoom, sender: str, text: str, intent: str, reply_to: str | None) -> None:
        """pm_chat.on_human: a named message the rules don't know. A stop meanwhile cancels the decision."""
        gen = self.generation.get(room.room_id, 0)
        if not self.background(self.decide(room, sender, text, intent, reply_to, gen)):
            log.warning("too many messages being understood; %s's goes to the phrase rules", sender)
            await self.by_rules(room, sender, text, intent, TOO_MANY)

    async def decide(self, room: MatrixRoom, sender: str, text: str, intent: str, reply_to: str | None,
                     gen: int, how: str | None = None) -> None:
        """how: why an unaddressed message may be for me (pm_implicit.py); None: it named me."""
        try:
            decision = await self.think(room, sender, text, reply_to, how)
            if self.generation.get(room.room_id, 0) != gen:
                return                        # someone said "stop" while I was thinking
            if how is not None:
                await self.after_implicit(room, sender, decision, text, how, gen)
            elif not isinstance(decision, tuple):     # None, or why the owner's agent didn't answer
                await self.by_rules(room, sender, text, intent, decision or "")
            else:
                await self.apply(room, sender, *decision, text, reply_to, gen, named=True)
        except (RuntimeError, OSError) as e:  # SendError / GitHub / network: tell the log, keep running
            log.error("couldn't act on %s's message: %s", sender, e)
        except Exception:                     # noqa: BLE001 — a background task: never fail silently
            log.exception("unexpected error acting on %s's message", sender)

    async def think(self, room: MatrixRoom, sender: str, text: str, reply_to: str | None,
                    how: str | None = None) -> tuple | str | None:
        """Ask the understanding step with the room's context; None (or why not, PM_BRAIN=owner) means
        use the rules. The owner's agent's budgets are spent BEFORE any context is read, so a refused
        burst costs nothing. Only a speculative read (SPECULATIVE) shows no typing indicator."""
        if pm_brain.BRAIN == "off":
            return None
        agent = None
        if pm_brain.BRAIN == "owner":
            agent, why = await self.admit_owner(room, sender)    # pm_owner_brain.py
            if agent is None:
                return why
        ctx = await self.context(room, sender, text, reply_to, how)
        shown = how not in SPECULATIVE        # they knowingly talk to me: show I'm on it
        if shown:
            await self.typing(room.room_id, True)
        try:
            return await (self.ask_owner(room, agent, ctx) if agent else pm_brain.understand(ctx))
        finally:
            if shown:
                await self.typing(room.room_id, False)

    async def context(self, room: MatrixRoom, sender: str, text: str, reply_to: str | None, how: str | None) -> dict:
        rid = room.room_id
        waiting = self.pending.get(rid)
        plan = self.last_plans.get(rid, {}).get("items", []) if hasattr(self, "last_plans") else []
        ctx = {"repo": self.room_repos.get(rid) or "(no repo set for this room)", "mode": await self.level(rid), "sender": sender.split(":")[0].lstrip("@"),
               "role": "owner or maintainer" if await room_state.is_moderator(self.client, room, sender) else "contributor",
               "waiting": (f"{waiting.get('kind') or 'autonomous'} (asked by {str(waiting.get('by', '')).split(':')[0]})"
                           if waiting else "nothing"),
               "plan": "; ".join(i["title"] for i in plan) or "none",
               "board": self.boards.get(rid, {}).get("text", "(none yet)"),
               "transcript": "", "replied": "", "text": text, "unaddressed": how is not None, "how": how or ""}
        try:
            ctx["transcript"] = await asyncio.to_thread(self.discussion, rid, True)
            ctx["replied"] = await self.replied_text(rid, reply_to)
        except OSError as e:                  # history unavailable: decide from the message alone
            log.warning("no history for %s: %s", rid, e)
        return ctx

    async def typing(self, room_id: str, on: bool) -> None:
        try:
            await self.client.room_typing(room_id, on, timeout=30_000)
        except Exception as e:                # noqa: BLE001 — only a typing indicator; never block on it
            log.debug("typing indicator failed: %s", e)

    async def replied_text(self, room_id: str, event_id: str | None) -> str:
        if not event_id:
            return ""
        r = await self.client.room_get_event(room_id, event_id)
        if not isinstance(r, RoomGetEventResponse):
            return ""
        what = self.outcomes().get(event_id)
        body = without_quote(getattr(r.event, "body", "") or "")[:1500]
        return f"{r.event.sender.split(':')[0].lstrip('@')}: {body}" + (f"\n(@pm's note: this was {what})" if what else "")

    async def apply(self, room: MatrixRoom, sender: str, action: str, reply: str, text: str,
                    reply_to: str | None, gen: int, named: bool) -> None:
        """Code decides. Recognized phrases never get here (pm_chat.on_human: the rules answer them), so
        anything that changes how the room runs is only the model's reading: one 👍 first, even when
        named (act_implicit). Other people's messages in the context must never be able to steer it."""
        rid = room.room_id
        if action == "approve" and self.pending.get(rid):
            await self.say(rid, CLICK_TO_APPROVE)    # an approval is a click, never a reading
        elif action == "approve":
            if named:
                await self.answer_stale_ok(rid, reply_to, sender)
        elif action == "none":
            if reply or named:                # empty and unnamed: "not for @pm" (pm_actions.UNADDRESSED)
                await self.say(rid, reply or "Noted.", plain=True)   # the model's words: plain text, no links
        else:
            await self.act_implicit(room, sender, BRAIN_ACTIONS[action], text, gen, named)

    async def ask_once(self, room_id: str, sender: str, gen: int, question: str, entry: dict, named: bool = True) -> bool:
        """Say my 👍 question and wait for it, unless another question waits (never replaced: a plan, a plan
        request, someone else's), or a stop or another question came while I said it (GPT review)."""
        waiting = self.pending.get(room_id)
        if waiting and time.time() < waiting.get("until", 0) and (
                waiting.get("kind") not in REPLACEABLE or waiting.get("by") != sender):
            if named:
                await self.say(room_id, ANSWER_FIRST)
            return False
        if not self.still(room_id, gen, waiting):
            return False
        event_id = await self.say(room_id, question)
        if not self.still(room_id, gen, waiting):
            self.note_outcome(event_id, "dropped: things changed before it could be answered")
            return False
        self.pending[room_id] = {**entry, "event_id": event_id, "until": time.time() + CONFIRM_S, "by": sender,
                                 "unnamed": not named}   # pm_implicit.unnamed_ask: no bare "yes" for those
        await self.offer_thumb(room_id, event_id)
        return True

    async def ask_setting(self, room: MatrixRoom, sender: str, action: str, gen: int | None = None,
                          named: bool = True) -> None:
        """A change to the sender's OWN agent settings, not in plain words; they confirm it with one click."""
        gen = self.generation.get(room.room_id, 0) if gen is None else gen
        await self.ask_once(room.room_id, sender, gen, SETTING_QUESTIONS[action] + TO_CONFIRM,
                            {"kind": "setting", "intent": BRAIN_ACTIONS[action]}, named)

    async def ask_mode(self, room: MatrixRoom, sender: str, action: str, gen: int | None = None) -> None:
        """A mode change their own words don't plainly say (a voice message, a question): ask first."""
        gen = self.generation.get(room.room_id, 0) if gen is None else gen
        if not await room_state.is_moderator(self.client, room, sender):
            await self.say(room.room_id, f"**{LEVEL_WORDS[action]}**. {ONLY_MODERATORS}")   # what it does, anyway
            return
        await self.ask_once(room.room_id, sender, gen, mode_question(action), {"kind": "mode", "level": action})
