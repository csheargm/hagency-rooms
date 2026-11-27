"""@pm's understanding path: a message addressed to @pm goes to the model (pm_brain.py, or with
PM_BRAIN=owner the room owner's agent: pm_owner_brain.py) with the room's context, in the background
so "stop" is never held up; code then applies the chosen action under the same permission checks as
the rules, and asks for a ✅ when the person's own words don't plainly say so. Messages without "@pm"
that may still be for me: pm_implicit.py. Mixed into loop.Loop alongside pm_chat.Chat (say, act,
confirm, pending, ...).
"""
import asyncio
import logging
import time

from nio import MatrixRoom, RoomGetEventResponse

from common import intents
from common.pm_actions import INTENT_OF as BRAIN_ACTIONS
from server import pm_brain
from common import room_state
from server.pm_chat import CONFIRM_S, without_quote
from server.pm_implicit import MAYBE, Implicit, mode_question
from server.pm_owner_brain import OwnerBrain

log = logging.getLogger("loop")
SETTING_QUESTIONS = {"follow_always": "Bring your agent along whenever you join a project room?",
                     "follow_ask": "Ask you first before bringing your agent into a room?",
                     "follow_never": "Never bring your agent along?"}


class Understanding(OwnerBrain, Implicit):
    async def decide(self, room: MatrixRoom, sender: str, text: str, intent: str, reply_to: str | None,
                     gen: int, voice: bool = False, how: str | None = None) -> None:
        """how: why an unaddressed message may be for me (pm_implicit.py); None: it named me."""
        try:
            decision = await self.think(room, sender, text, reply_to, how)
            if self.generation.get(room.room_id, 0) != gen:
                return                        # someone said "stop" while I was thinking
            if how is not None:
                await self.after_implicit(room, sender, decision, text, how)
            elif not isinstance(decision, tuple):     # None, or why the owner's agent didn't answer
                await self.by_rules(room, sender, text, intent, decision or "")
            else:
                await self.apply(room, sender, *decision, text, reply_to, voice)
        except (RuntimeError, OSError) as e:  # SendError / GitHub / network: tell the log, keep running
            log.error("couldn't act on %s's message: %s", sender, e)
        except Exception:                     # noqa: BLE001 — a background task: never fail silently
            log.exception("unexpected error acting on %s's message", sender)

    async def think(self, room: MatrixRoom, sender: str, text: str, reply_to: str | None,
                    how: str | None = None) -> tuple | str | None:
        """Ask the understanding step with the room's context; None (or why not, PM_BRAIN=owner) means
        use the rules. Only the speculative read ("is it even for me?", MAYBE) shows no typing indicator."""
        if pm_brain.BRAIN == "off":
            return None
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
        shown = how != MAYBE                  # they knowingly talk to me: show I'm on it
        if shown:
            await self.typing(rid, True)
        try:
            return await (self.ask_owner(room, ctx) if pm_brain.BRAIN == "owner" else pm_brain.understand(ctx))
        finally:
            if shown:
                await self.typing(rid, False)

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
                    reply_to: str | None, voice: bool = False) -> None:
        """Code decides: the model's action goes through the same permission checks as the rules, and
        anything that changes how the room runs needs the person's OWN words to say so (else a ✅):
        other people's messages in the context must not be able to steer it."""
        rid = room.room_id
        # Spoken words only ever ASK: a transcript may hold someone else's voice or a misheard word.
        if action in ("assist", "autonomous") and (voice or intents.classify(text) != BRAIN_ACTIONS[action]):
            await self.ask_mode(room, sender, action)
            return
        if action.startswith("follow_") and (voice or not intents.plainly_says(text, BRAIN_ACTIONS[action])):
            await self.ask_setting(room, sender, action)   # understood, but not in plain words: one 👍 to be sure
            return
        waiting = self.pending.get(rid, {})
        if action == "approve" and waiting and (voice or not intents.is_approval(text)):
            await self.say(rid, "To approve it, click 👍 on my question above (or type \"ok\" in reply).")
            return
        if action == "approve":
            if not await self.confirm(room, sender):
                waiting = self.pending.get(rid)
                if waiting and waiting.get("by") not in (None, sender):
                    await self.say(rid, "Only the person who asked for it can approve that one.")
                else:
                    await self.answer_stale_ok(rid, reply_to, sender)
        elif action == "none":
            await self.say(rid, reply or "Noted.", plain=True)   # the model's words: plain text, no links
        elif action == "plan" and reply and await room_state.is_moderator(self.client, room, sender):
            await self.say(rid, reply, plain=True)               # a promise ("I'll draft…"), not a claim
            await self.act(room, sender, intents.PLAN)
        else:
            await self.act(room, sender, BRAIN_ACTIONS[action])

    async def ask_setting(self, room: MatrixRoom, sender: str, action: str) -> None:
        """A change to the sender's OWN agent settings, not in plain words; they confirm it with one click."""
        waiting = self.pending.get(room.room_id)
        if waiting and time.time() < waiting.get("until", 0) and waiting.get("kind") != "setting":
            await self.say(room.room_id, "Please answer my question above first; then ask me again.")
            return                            # never replace someone's waiting plan or mode question
        event_id = await self.say(room.room_id, f"{SETTING_QUESTIONS[action]} Click 👍 below to confirm.")
        self.pending[room.room_id] = {"kind": "setting", "intent": BRAIN_ACTIONS[action], "event_id": event_id,
                                      "until": time.time() + CONFIRM_S, "by": sender}
        await self.offer_thumb(room.room_id, event_id)

    async def ask_mode(self, room: MatrixRoom, sender: str, action: str) -> None:
        """The model thinks they want a mode change their own words don't plainly say: ask first."""
        if not await room_state.is_moderator(self.client, room, sender):
            await self.say(room.room_id, "Only the room owner or a maintainer can change how I run this project.")
            return
        event_id = await self.say(room.room_id, mode_question(action))
        self.pending[room.room_id] = {"kind": "mode", "level": action, "event_id": event_id,
                                      "until": time.time() + CONFIRM_S, "by": sender}
        await self.offer_thumb(room.room_id, event_id)
