"""One answer from the trial agent (ADR 0025): who it answers, the caps, the model call.

It answers a message only when ALL hold: it's new (not from before it started), from a person (never
an agent, a bot, @pm or itself), from a visitor who chose the trial and still has no connected agent
of their own, who brought it into this room and is joined here, and the message is for it (anything
in its private chat with them; elsewhere, a mention). A reply's quoted part is never read: only what
the visitor wrote goes to the model (GPT review). Then the visitor's and the server's daily caps
(trial_limits.py); a reached cap is said once a day, with when it resets and how to bring their own.

Matrix sync never waits on a model: each answer runs as its own task (at most MAX_WAITING at once,
else "busy"), one model call at a time for the whole server (`slot`): a message waits up to
TRIAL_QUEUE_WAIT_S, then is told the agent is busy (not counted). The plan's monthly budget
(trial_budget.py) is checked first and a conservative token estimate reserved; a 429 or 402 from
Ollama pauses every trial, said plainly to the visitor and logged for the operator. Before a PR is read
and before the answer is posted, the visitor must still be in the room (and the room's repo unchanged).
Mixed into trial_agent.TrialAgent (client, me, started_ms, visitors, rooms, usage, budget, slot,
ask_model, history, locks, eligible, sponsors, post).
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
from server.pm_address import without_quote

log = logging.getLogger("trial")
HISTORY_TURNS = 8                     # this visitor's last turns in this room, kept in memory only
MAX_WAITING = 6                       # answers queued or running at once; more: "busy" at once
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
    own = without_quote(event.body or "")             # never a mention inside the quoted message
    return (isinstance(named, list) and me in named) or me in own or bool(NAME_RE.search(own))


def cap_words(cap: str, now: float) -> str:
    said = CAPPED[cap].format(n=trial_limits.number("TRIAL_PER_VISITOR"))
    return f"{said} They come back {trial_limits.resets_in(now)}. {BRING}"


class Turns:
    def for_me(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """Who and where (see the module docstring); the visitor's eligibility is checked by the caller."""
        sender = event.sender
        if (sender == self.me() or is_agent_or_bot(sender) or lifecycle.is_stale(event, self.started_ms)
                or sender not in self.visitors or not self.still_here(room.room_id, sender)):
            return False
        if (self.visitors.get(sender) or {}).get("dm") == room.room_id:
            return True                   # our private chat: every message is for me
        return mentions(event, self.me())

    def still_here(self, room_id: str, visitor: str) -> bool:
        """They brought me into this room and are joined there now (asked again before reading and posting)."""
        room = self.client.rooms.get(room_id)
        return (visitor in self.sponsors(room_id) and room is not None and visitor in room.users
                and visitor not in getattr(room, "invited_users", {}))

    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        """Quick: the answer is its own task, so sync (invites, leaves, others' messages) never waits."""
        if not self.for_me(room, event):
            return
        if len(self.waiting) >= MAX_WAITING:
            await self.post_safely(room.room_id, BUSY)
            return
        task = asyncio.create_task(self.handle(room.room_id, event))
        self.waiting.add(task)
        task.add_done_callback(self.waiting.discard)

    async def drain(self) -> None:
        """Wait for the answers underway (tests; a restart lets them finish)."""
        while self.waiting:
            await asyncio.gather(*list(self.waiting), return_exceptions=True)

    async def handle(self, room_id: str, event: RoomMessageText) -> None:
        lock = self.locks.setdefault(event.sender, asyncio.Lock())   # one answer at a time per visitor
        async with lock:
            try:
                await self.answer(room_id, event)
            except (RuntimeError, OSError) as e:   # Matrix refused, or a count couldn't be saved: said if possible
                log.error("trial agent couldn't answer %s in %s: %s", event.sender, room_id, e)
                await self.post_safely(room_id, FAILED.format(why="something went wrong on the server"))

    async def post_safely(self, room_id: str, text: str) -> None:
        try:
            await self.post(room_id, text)
        except RuntimeError as e:         # Matrix refuses even this: the log line is all that's left
            log.error("trial agent couldn't post in %s: %s", room_id, e)

    async def answer(self, room_id: str, event: RoomMessageText) -> None:
        visitor, now, text = event.sender, event.server_timestamp / 1000, without_quote(event.body or "")
        if not self.eligible(visitor):
            if self.usage.first_notice(visitor, "own"):
                await self.post(room_id, HAS_OWN)
            return
        try:
            await asyncio.wait_for(self.slot.acquire(), trial_budget.setting_or("TRIAL_QUEUE_WAIT_S", QUEUE_WAIT_S))
        except asyncio.TimeoutError:
            await self.post(room_id, BUSY)          # not counted: nothing was asked of the model
            return
        try:
            reply, read_repo = await self.in_slot(room_id, visitor, text, now)
        finally:
            self.slot.release()
        if not reply or not self.still_here(room_id, visitor):
            return                                    # they left meanwhile: nothing goes to that room
        if read_repo and await self.repo(room_id) != read_repo:
            log.warning("not posting a PR answer in %s: its repo changed meanwhile", room_id)
            return
        await self.post(room_id, reply)

    def stop_words(self, stop: str) -> str:
        return f"{STOPPED[stop].format(mins=max(1, math.ceil(self.budget.paused_for() / 60)))} {BRING}"

    async def in_slot(self, room_id: str, visitor: str, text: str, now: float) -> tuple[str | None, str | None]:
        """Holding the one model slot: the budget, the caps, then the model; (what to post, the repo a PR was read from)."""
        if stop := await self.budget.stopped():
            return (self.stop_words(stop) if self.usage.first_notice(visitor, stop) else None), None
        if cap := self.usage.take(visitor):
            return (cap_words(cap, now) if self.usage.first_notice(visitor, cap) else None), None
        await self.typing(room_id, True)
        try:
            return await self.think(room_id, visitor, text)
        except trial_model.TrialLimited as e:
            self.budget.pause(e.retry_after or trial_budget.setting("TRIAL_PAUSE_S"), f"Ollama answered {e.status}")
            return self.stop_words(trial_budget.PAUSED), None
        except trial_model.TrialModelError as e:
            return FAILED.format(why=e), None
        finally:
            await self.typing(room_id, False)

    async def think(self, room_id: str, visitor: str, text: str) -> tuple[str | None, str | None]:
        """The model's answer to `text`, with this visitor's recent turns here and a PR's diff if asked."""
        repo = await self.repo(room_id)
        n = trial_reads.pr_number(text, repo)
        diff = await trial_reads.pr_diff(repo, n) if n and repo and self.still_here(room_id, visitor) else None
        turns = self.history.setdefault((room_id, visitor), [])
        chat = trial_model.messages(turns, text, diff)
        model = trial_models.chosen((self.visitors.get(visitor) or {}).get("model"))
        weight = trial_models.weight(model)       # a bigger model's tokens count more against the month
        reserved = (trial_model.chars(chat) // 4 + trial_limits.number("TRIAL_MAX_TOKENS")) * weight
        if not self.budget.reserve(reserved):     # this answer could pass the month's token cap: not asked
            return (self.stop_words(trial_budget.TOKENS) if self.usage.first_notice(visitor, trial_budget.TOKENS) else None), None
        reply = await (self.ask_model or trial_model.ask)(
            chat, count=lambda used: self.budget.settle(reserved, used * weight), model=model)
        turns += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        del turns[:-HISTORY_TURNS]
        return f"{reply}\n\n— Trial agent · {model}", (repo if diff else None)   # which model answered, on every reply

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
