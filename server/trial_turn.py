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
ask_model, history, answering, waiting, busy, eligible, sponsors, post).
"""
import asyncio
import logging
import math
import re
import time

from nio import MatrixRoom, RoomMessageText

from common import identity
from common import lifecycle
from common import room_state
from server import trial_budget
from server import trial_limits
from server import trial_model
from server import trial_models
from server import trial_reads
from common.bridge_config import SERVER_NAME
from server.pm_address import without_quote

log = logging.getLogger("trial")
HISTORY_TURNS = 8                     # this visitor's last turns in this room, kept in memory only
NOTICE_EVERY_S = 60                   # "still answering" / "busy": at most this often per visitor
MAX_WAITING = 6                       # answers queued or running at once; more: "busy" at once
NAME_RE = re.compile(r"^\W*(@?hagency\.)?trial[ -]agent\b", re.I)   # "Trial agent, …" at the start
BRING = "To bring your own agent (no daily cap, with tools and the GitHub loop): type \"bring my own\" in your chat with Product Manager."
CAPPED = {trial_limits.VISITOR: "You've used today's {n} trial messages.",
          trial_limits.TOKENS: "You've used today's trial allowance (long questions and pull requests use it faster).",
          trial_limits.SERVER: "The trial agent has used all of this server's trial messages for today."}
HAS_OWN = "You have your own agent now, so the trial agent steps aside: talk to your agent instead."
FAILED = "⚠️ I couldn't answer just now: {why}. Please try again in a minute."
QUEUE_WAIT_S = 20                     # TRIAL_QUEUE_WAIT_S: how long a message waits for the one model slot
BUSY = "I'm busy answering someone else right now. Please try again in a minute."
STILL = "I'm still answering your last message: one at a time, please."
RESTING = "The trial agent has used its share of this month's AI budget, so it's resting until next month (00:00 UTC on the 1st)."
TOO_BIG = ("That request is too big for what's left of today's trial allowance or this month's trial budget. "
           "Try a shorter question, or one without a pull request.")
STOPPED = {trial_budget.PAUSED: "The trial agent is paused: the AI service it uses said it has reached its limit for now. "
                                "It should be back in about {mins} min.",
           trial_budget.SHARE: "The trial agent has used its share of the AI plan for this period, so it's resting until the "
                               "plan's next period.",
           trial_budget.WINDOW: "The trial agent has used its share of the AI service for now; it comes back when the "
                                "service's usage window resets (in hours, or at its weekly reset).",
           trial_budget.DAMAGED: "The trial agent is paused while the server's operator checks its settings. Please try again later.",
           trial_budget.TOKENS: RESTING}


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
        if self.stopping.is_set() or not self.for_me(room, event):
            return                             # @pm is restarting: nothing new starts (it would be cut off)
        if event.sender in self.answering:     # one at a time per visitor: never a queue of theirs (GPT review)
            self.say_once(room.room_id, event.sender, STILL)
            return
        if len(self.waiting) >= MAX_WAITING:
            self.say_once(room.room_id, event.sender, BUSY)
            return
        self.answering.add(event.sender)
        task = asyncio.create_task(self.handle(room.room_id, event))
        for tasks in (self.waiting, self.busy):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        task.add_done_callback(lambda _: self.answered(event.sender))

    def say_once(self, room_id: str, visitor: str, text: str) -> None:
        """STILL or BUSY, at most once a minute per visitor (independent review: a flood of them got the shared
        account rate limited), posted in the background so sync never waits on it."""
        if time.time() - self.told_busy.get(visitor, 0) < NOTICE_EVERY_S:
            return
        self.told_busy[visitor] = time.time()
        task = asyncio.create_task(self.post_safely(room_id, text))
        self.busy.add(task)
        task.add_done_callback(self.busy.discard)

    async def drain(self) -> None:
        """Wait for the answers underway (tests; a restart lets them finish)."""
        while self.waiting:
            await asyncio.gather(*list(self.waiting), return_exceptions=True)

    async def handle(self, room_id: str, event: RoomMessageText) -> None:
        try:
            await self.answer(room_id, event)
        except (RuntimeError, OSError, ValueError, TypeError) as e:   # Matrix refused, a count couldn't be saved: said
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
            await asyncio.wait_for(self.slot.acquire(), max(1.0, trial_budget.setting_or("TRIAL_QUEUE_WAIT_S", QUEUE_WAIT_S)))
        except asyncio.TimeoutError:
            self.say_once(room_id, visitor, BUSY)   # not counted: nothing was asked of the model
            return
        try:
            reply, read_repo, turn = await self.in_slot(room_id, visitor, text, now)
        finally:
            self.slot.release()
        if reply and await self.may_post(room_id, visitor, read_repo):
            await self.post(room_id, reply)
            if turn:                                  # remembered only once it was really said there
                turns = self.history.setdefault((room_id, visitor), [])
                turns += turn
                del turns[:-HISTORY_TURNS]

    async def may_post(self, room_id: str, visitor: str, read_repo: str | None) -> bool:
        """The repo first (an await), then membership, so a leave during the lookup still counts."""
        if read_repo and await self.repo(room_id) != read_repo:
            log.warning("not posting a PR answer in %s: its repo changed meanwhile", room_id)
            return False
        return self.still_here(room_id, visitor)    # they left meanwhile: nothing goes to that room

    def stop_words(self, stop: str) -> str:
        return f"{STOPPED[stop].format(mins=max(1, math.ceil(self.budget.paused_for() / 60)))} {BRING}"

    def answered(self, visitor: str) -> None:
        self.answering.discard(visitor)

    def notice(self, visitor: str, stop: str, words: str) -> tuple[str | None, None, None]:
        return (words if self.usage.first_notice(visitor, stop) else None), None, None

    async def in_slot(self, room_id: str, visitor: str, text: str, now: float) -> tuple:
        """Holding the one model slot: the budget, the caps, then the model;
        (what to post, the repo a PR was read from, the turn to remember once posted)."""
        if stop := await self.budget.stopped():
            return self.notice(visitor, stop, self.stop_words(stop))
        if cap := self.usage.take(visitor):
            return self.notice(visitor, cap, cap_words(cap, now))
        await self.typing(room_id, True)
        try:
            return await self.think(room_id, visitor, text)
        except trial_model.TrialLimited as e:
            self.budget.pause(e.retry_after or trial_budget.setting("TRIAL_PAUSE_S"), f"Ollama answered {e.status}")
            return self.stop_words(trial_budget.PAUSED), None, None
        except trial_model.TrialModelError as e:
            return FAILED.format(why=e), None, None
        finally:
            await self.typing(room_id, False)

    async def think(self, room_id: str, visitor: str, text: str) -> tuple:
        """The model's answer to `text`, with this visitor's recent turns here and a PR's diff if asked."""
        repo = await self.repo(room_id)
        n = trial_reads.pr_number(text, repo)
        diff = await trial_reads.pr_diff(repo, n) if n and repo and self.still_here(room_id, visitor) else None
        if diff and not await self.may_post(room_id, visitor, repo):   # left, or rebound, while GitHub answered
            self.usage.give_back(visitor)
            return None, None, None
        if not (self.eligible(visitor) and self.still_here(room_id, visitor)):   # GPT review: left, or connected an
            self.usage.give_back(visitor)          # agent of their own, while it waited for the slot: never sent
            return None, None, None
        chat = trial_model.messages(self.history.get((room_id, visitor), []), text, diff)
        model = trial_models.chosen((self.visitors.get(visitor) or {}).get("model"))
        weight = trial_models.weight(model)       # a bigger model's tokens count more against the month
        reserved = trial_model.high_estimate(chat, trial_limits.number("TRIAL_MAX_TOKENS")) * weight
        if (refused := self.reserve(visitor, reserved)) is not None:   # "": refused, already said today
            self.usage.give_back(visitor)         # never asked: not counted against their day either
            return refused or None, None, None
        reply = await (self.ask_model or trial_model.ask)(chat, count=lambda used: self.settle(visitor, reserved, used * weight),
                                                          model=model)
        turn = [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        return f"{reply}\n\n— Trial agent · {model}", (repo if diff else None), turn   # every reply names its model

    def reserve(self, visitor: str, n: float) -> str | None:
        """Reserve `n` of the visitor's day and of the month; None when both fit, else what to say ("": refused,
        and the used-up allowance was said already today). A request too big for what's left says so each time:
        a request too big for what's left says so each time (independent review), a used-up allowance once a day."""
        remote, now = identity.split(visitor)[1] != SERVER_NAME, time.time()
        self.reserved_at = (trial_limits.day(now), trial_budget.month(now))   # one call at a time: one reservation
        if not self.usage.reserve_tokens(visitor, n, remote):
            if self.usage.tokens_used(visitor) + self.smallest(visitor) <= self.usage.allowance(visitor, remote):
                return TOO_BIG                    # a shorter one still fits: say so, every time
            return cap_words(trial_limits.TOKENS, time.time()) if self.usage.first_notice(visitor, trial_limits.TOKENS) else ""
        if not self.budget.reserve(n):            # this answer could pass the month's token cap: not asked
            self.usage.settle_tokens(visitor, n, 0, self.reserved_at[0])
            return TOO_BIG
        return None

    def settle(self, visitor: str, reserved: float, used: float) -> None:
        on_day, in_month = getattr(self, "reserved_at", (None, None))
        self.budget.settle(reserved, used, in_month)
        self.usage.settle_tokens(visitor, reserved, used, on_day)

    def smallest(self, visitor: str) -> float:
        """What the shortest possible request reserves on their model: below it, the day is used up."""
        weight = trial_models.weight(trial_models.chosen((self.visitors.get(visitor) or {}).get("model")))
        return trial_model.high_estimate(trial_model.messages([], "?"), trial_limits.number("TRIAL_MAX_TOKENS")) * weight

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
