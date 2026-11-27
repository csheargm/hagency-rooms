"""Unit and flow tests: the trial agent's monthly budget guard, one model call at a time, and pausing on
Ollama's 429/402 (ADR 0025). Run: .venv/bin/python -m unittest tests.test_trial_budget"""
import asyncio
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import trial_budget
from server import trial_turn
from server.trial_model import TrialLimited
from tests.test_trial_agent import Base, NEW, OTHER, message, room

NOON = datetime.datetime(2026, 11, 27, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
PLAN = {"included": {"balance_usd": 72.5, "allowance_usd": 100, "period": {"from": "x", "until": "y"}},
        "purchased": {"balance_usd": 0}}


class Share(unittest.TestCase):
    def test_the_plans_used_share_from_ollamas_balance(self):
        self.assertAlmostEqual(trial_budget.used_share(PLAN), 0.275)
        self.assertEqual(trial_budget.used_share({"included": {"balance_usd": 0, "allowance_usd": 100}}), 1.0)
        legacy = {"included": {"session": {"remaining_percent": 80}, "weekly": {"remaining_percent": 30}}}
        self.assertAlmostEqual(trial_budget.used_share(legacy), 0.7)          # the tighter window counts
        for unreadable in (None, [], {}, {"included": "x"}, {"included": {"allowance_usd": 0, "balance_usd": 5}}):
            self.assertIsNone(trial_budget.used_share(unreadable))


class Guard(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "trial-budget.json"
        self.now, self.balance, self.fetches = NOON, PLAN, 0
        env = mock.patch.dict(os.environ, {"TRIAL_MAX_SHARE": "0.5", "TRIAL_MONTHLY_TOKENS": "1000"})
        env.start()
        self.addCleanup(env.stop)

    async def fetch(self):
        self.fetches += 1
        return self.balance

    def budget(self):
        return trial_budget.Budget(self.path, clock=lambda: self.now, fetch=self.fetch)

    def test_trials_stop_past_their_share_of_the_plan(self):
        b = self.budget()
        self.assertIsNone(asyncio.run(b.stopped()))
        self.balance = {"included": {"balance_usd": 45, "allowance_usd": 100}}   # 55% used, by anyone on the key
        self.now += 301
        self.assertEqual(asyncio.run(b.stopped()), trial_budget.SHARE)

    def test_the_balance_is_read_at_most_every_check_interval(self):
        b = self.budget()
        for _ in range(5):
            asyncio.run(b.stopped())
        self.assertEqual(self.fetches, 1)                                       # its rate limit is shared

    def test_an_unreadable_balance_keeps_the_last_reading(self):
        self.balance = {"included": {"balance_usd": 10, "allowance_usd": 100}}
        b = self.budget()
        self.assertEqual(asyncio.run(b.stopped()), trial_budget.SHARE)
        self.balance, self.now = None, self.now + 301
        self.assertEqual(asyncio.run(b.stopped()), trial_budget.SHARE)        # never back on by a failed read

    def test_the_token_cap_guards_without_a_balance(self):
        self.balance = None
        b = self.budget()
        b.add_tokens(999)
        self.assertIsNone(asyncio.run(b.stopped()))
        b.add_tokens(1)
        self.assertEqual(asyncio.run(self.budget().stopped()), trial_budget.TOKENS)   # on disk: a restart keeps it
        self.now = datetime.datetime(2026, 12, 1, 0, 1, tzinfo=datetime.timezone.utc).timestamp()
        self.assertIsNone(asyncio.run(self.budget().stopped()))                 # a new month

    def test_a_pause_survives_a_restart_and_ends(self):
        b = self.budget()
        with self.assertLogs("trial", "ERROR") as logged:
            b.pause(600, "Ollama answered 429")
        self.assertIn("operator", logged.output[0])                             # the operator sees it
        self.assertEqual(asyncio.run(self.budget().stopped()), trial_budget.PAUSED)
        self.now += 601
        self.assertIsNone(asyncio.run(self.budget().stopped()))


class InTheChat(Base):
    def test_over_budget_the_visitor_is_told_once_and_the_model_isnt_asked(self):
        self.balance = {"included": {"balance_usd": 20, "allowance_usd": 100}}
        self.enrol()
        for n in range(2):
            self.run_(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, f"q{n}")))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.said()[1:], [f"{trial_turn.RESTING} {trial_turn.BRING}"])
        self.assertEqual(self.agent.usage.used(NEW), 0)                         # not counted

    def test_a_429_pauses_every_trial_and_says_so(self):
        self.enrol()

        async def limited(chat, count=None, model=None):
            self.asked.append(chat)
            raise TrialLimited(429, 120)
        self.agent.ask_model = limited
        with self.assertLogs("trial", "ERROR"):
            self.run_(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertIn("paused", self.said()[-1])
        self.assertIn("about 2 min", self.said()[-1])
        self.assertIn("bring my own", self.said()[-1])
        self.run_(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, "again")))
        self.assertEqual(len(self.asked), 1)                                    # paused: not asked again

    def test_tokens_used_are_counted_for_the_month(self):
        self.enrol()

        async def counted(chat, count=None, model=None):
            count(321)
            return "ok"
        self.agent.ask_model = counted
        self.run_(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.budget.data["tokens"], 321)

    def test_one_model_call_at_a_time_and_a_busy_note_after_the_wait(self):
        self.enrol()
        self.client.rooms["!dm2"] = room("!dm2", OTHER)
        self.agent.visitors[OTHER], self.agent.rooms["!dm2"] = {"dm": "!dm2"}, [OTHER]
        running, most = [0], [0]

        async def slow(chat, count=None, model=None):
            running[0] += 1
            most[0] = max(most[0], running[0])
            await asyncio.sleep(0.3)
            running[0] -= 1
            return "done"
        self.agent.ask_model = slow

        async def both():
            self.agent.slot = asyncio.Semaphore(1)
            await asyncio.gather(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, "one")),
                                 self.agent.on_message(self.client.rooms["!dm2"], message(OTHER, "two")))
        with mock.patch.dict(os.environ, {"TRIAL_QUEUE_WAIT_S": "0.05"}):
            self.run_(both())
        self.assertEqual(most[0], 1)
        self.assertIn(trial_turn.BUSY, self.said())
        self.assertEqual(self.agent.usage.used(OTHER), 0)                       # a busy note isn't counted
        with mock.patch.dict(os.environ, {"TRIAL_QUEUE_WAIT_S": "5"}):
            self.posts.clear()
            self.run_(both())
        self.assertEqual(len([s for s in self.said() if s.startswith("done")]), 2)                          # a short wait: both answered, in turn


class ModelLimits(unittest.IsolatedAsyncioTestCase):
    async def test_429_and_402_raise_trial_limited_with_retry_after(self):
        from aiohttp import web
        from server import trial_model
        status = {"code": 429}

        async def chat(request):
            return web.json_response({"error": "limit"}, status=status["code"], headers={"Retry-After": "42"})
        app = web.Application()
        app.add_routes([web.post("/api/chat", chat)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/chat"
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20, "TRIAL_API_URL": url}):
            for code in (429, 402):
                status["code"] = code
                with self.assertRaises(TrialLimited) as e:
                    await trial_model.ask(trial_model.messages([], "hi"))
                self.assertEqual((e.exception.status, e.exception.retry_after), (code, 42.0))

    def test_tokens_come_from_ollamas_counts_or_an_estimate(self):
        from server import trial_model
        self.assertEqual(trial_model.tokens_of({"prompt_eval_count": 100, "eval_count": 50}), 150)
        self.assertEqual(trial_model.tokens_of({}), 0)
        self.assertEqual(trial_model.estimate([{"content": "x" * 40}], {"message": {"content": "y" * 40}}), 21)


if __name__ == "__main__":
    unittest.main()
