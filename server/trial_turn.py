"""One answer from the trial agent (ADR 0025): who it answers, the caps, the model call.

It answers a message only when ALL hold: it's new (not from before it started), from a person (never
an agent, a bot, @pm or itself), from a visitor who chose the trial and still has no connected agent
of their own, who brought it into this room and is joined here, and the message is for it (anything
in its private chat with them; elsewhere, a mention). Then the visitor's and the server's daily caps
(trial_limits.py); a reached cap is said once a day, with when it resets and how to bring their own.
One model call at a time for the whole server (`slot`): a message waits up to TRIAL_QUEUE_WAIT_S, then
is told the agent is busy (not counted). The plan's monthly budget (trial_budget.py) is checked first;
a 429 or 402 from Ollama pauses every trial, said plainly to the visitor and logged for the operator.
Mixed into trial_agent.TrialAgent (client, me, started_ms, visitors, rooms, usage, budget, slot,
ask_model, history, locks, is_visitor, sponsors, post).
"""
import asyncio
import logging
import math
import re

from nio import MatrixRoom, RoomMessageText

from common import identity
from common import lifecycle
from common import room_state
from server import trial_budget
from server import trial_limits
from server import trial_model
from server import trial_models
from server import trial_reads

log = logging.getLogger("trial")
HISTORY_TURNS = 8                     # this visitor's last turns in this room, kept in memory only
NAME_RE = re.compile(r"^\W*(@?hagency\.)?trial[ -]agent\b", re.I)   # "Trial agent, …" at the start
BRING = "To bring your own agent (no daily cap, with tools and the GitHub loop): type \"bring my own\" in your chat with Product Manager."
CAPPED = {trial_limits.VISITOR: "You've used today's {n} trial messages.",
          trial_limits.SERVER: "The trial agent has used all of this server's trial messages for today."}
HAS_OWN = "You have your own agent now, so the trial agent steps aside: talk to your agent instead."
FAILED = "⚠️ I couldn't answer just now: {why}. Please try again in a minute."
QUEUE_WAIT_S = 20                     # TRIAL_QUEUE_WAIT_S: how long a message waits for the one model slot
BUSY = "I'm busy answering someone else right now. Please try again in a minute."
RESTING = "The trial agent has used its share of this month's AI budget, so it's resting until next month."
STOPPED = {trial_budget.PAUSED: "The trial agent is paused: the AI service it uses said it has reached its limit for now. "
                                "It should be back in about {mins} min.",
           trial_budget.SHARE: RESTING, trial_budget.TOKENS: RESTING}


def is_agent_or_bot(user: str) -> bool:
    return identity.split(user)[0].endswith(("-agent", "-bot"))


def mentions(event: RoomMessageText, me: str) -> bool:
    content = event.source.get("content", {}) if isinstance(event.source, dict) else {}
    named = (content.get("m.mentions") or {}).get("user_ids") if isinstance(content.get("m.mentions"), dict) else None
    return (isinstance(named, list) and me in named) or me in (event.body or "") or bool(NAME_RE.search(event.body or ""))


def cap_words(cap: str, now: float) -> str:
    said = CAPPED[cap].format(n=trial_limits.number("TRIAL_PER_VISITOR"))
    return f"{said} They come back {trial_limits.resets_in(now)}. {BRING}"


class Turns:
    def for_me(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """Who and where (see the module docstring); the visitor's eligibility is checked by the caller."""
        sender = event.sender
        if (sender == self.me() or is_agent_or_bot(sender) or lifecycle.is_stale(event, self.started_ms)
                or sender not in self.visitors or sender not in self.sponsors(room.room_id)):
            return False
        if sender not in room.users or sender in getattr(room, "invited_users", {}):
            return False                  # only in rooms that visitor is in
        if (self.visitors.get(sender) or {}).get("dm") == room.room_id:
            return True                   # our private chat: every message is for me
        return mentions(event, self.me())

    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if not self.for_me(room, event):
            return
        lock = self.locks.setdefault(event.sender, asyncio.Lock())   # one answer at a time per visitor
        async with lock:
            try:
                await self.answer(room, event)
            except RuntimeError as e:     # Matrix refused the post: nothing more can reach them there
                log.error("trial agent couldn't answer %s in %s: %s", event.sender, room.room_id, e)

    async def answer(self, room: MatrixRoom, event: RoomMessageText) -> None:
        visitor, now = event.sender, event.server_timestamp / 1000
        if not self.eligible(visitor):
            if self.usage.first_notice(visitor, "own"):
                await self.post(room.room_id, HAS_OWN)
            return
        try:
            await asyncio.wait_for(self.slot.acquire(), trial_budget.setting_or("TRIAL_QUEUE_WAIT_S", QUEUE_WAIT_S))
        except asyncio.TimeoutError:
            await self.post(room.room_id, BUSY)    # not counted: nothing was asked of the model
            return
        try:
            reply = await self.in_slot(room.room_id, visitor, event.body or "", now)
        finally:
            self.slot.release()
        if reply:
            await self.post(room.room_id, reply)

    def stop_words(self, stop: str) -> str:
        return f"{STOPPED[stop].format(mins=max(1, math.ceil(self.budget.paused_for() / 60)))} {BRING}"

    async def in_slot(self, room_id: str, visitor: str, text: str, now: float) -> str | None:
        """Holding the one model slot: the budget, the caps, then the model; what to post (None: nothing)."""
        if stop := await self.budget.stopped():
            return self.stop_words(stop) if self.usage.first_notice(visitor, stop) else None
        if cap := self.usage.take(visitor):
            return cap_words(cap, now) if self.usage.first_notice(visitor, cap) else None
        await self.typing(room_id, True)
        try:
            return await self.think(room_id, visitor, text)
        except trial_model.TrialLimited as e:
            self.budget.pause(e.retry_after or trial_budget.setting("TRIAL_PAUSE_S"), f"Ollama answered {e.status}")
            return self.stop_words(trial_budget.PAUSED)
        except trial_model.TrialModelError as e:
            return FAILED.format(why=e)
        finally:
            await self.typing(room_id, False)

    async def think(self, room_id: str, visitor: str, text: str) -> str:
        """The model's answer to `text`, with this visitor's recent turns here and a PR's diff if asked."""
        repo = await self.repo(room_id)
        n = trial_reads.pr_number(text, repo)
        diff = await trial_reads.pr_diff(repo, n) if n and repo else None
        turns = self.history.setdefault((room_id, visitor), [])
        chat = trial_model.messages(turns, text, diff)
        model = trial_models.chosen((self.visitors.get(visitor) or {}).get("model"))
        weight = trial_models.weight(model)       # a bigger model's tokens count more against the month
        reply = await (self.ask_model or trial_model.ask)(chat, count=lambda n: self.budget.add_tokens(n * weight), model=model)
        turns += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        del turns[:-HISTORY_TURNS]
        return f"{reply}\n\n— Trial agent · {model}"      # which model answered, on every reply

    async def repo(self, room_id: str) -> str | None:
        try:
            return await room_state.repo_of(self.client, room_id)
        except (RuntimeError, OSError, asyncio.TimeoutError, KeyError, TypeError):   # unreadable: talk without the repo
            return None

    async def typing(self, room_id: str, on: bool) -> None:
        try:
            await self.client.room_typing(room_id, on, timeout=trial_limits.number("TRIAL_TIMEOUT_S") * 1000 or 30000)
        except Exception as e:            # noqa: BLE001 — "typing…" is cosmetic; the answer still goes out
            log.warning("typing in %s: %s", room_id, e)
