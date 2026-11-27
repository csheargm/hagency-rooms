"""Regression tests for the second GPT review of the trial agent (ADR 0025): month rollover, the budget
file, one answer per visitor, give-backs, hello retries, background start. Run:
.venv/bin/python -m unittest tests.test_trial_review2"""
import asyncio
import datetime
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from server import trial_agent
from server import trial_budget
from server import trial_model
from server import trial_reads
from server import trial_turn
from tests.test_trial_agent import NEW, Base, hear, message, room


class SecondPass(Base):
    """The second GPT review's points."""
    def test_a_settle_after_midnight_on_the_first_never_carries_the_old_month(self):
        now = [datetime.datetime(2026, 11, 30, 23, 59, tzinfo=datetime.timezone.utc).timestamp()]
        b = trial_budget.Budget(self.dir / "rollover.json", clock=lambda: now[0], fetch=self.fetch_balance)
        b.add_tokens(800)
        self.assertTrue(b.reserve(1500))
        now[0] += 120                                             # December now
        b.settle(1500, 400)
        self.assertEqual((b.data["month"], b.data["tokens"]), ("2026-12", 0))

    def test_a_budget_file_with_bad_fields_stops_trials(self):
        for bad in ('{"month": "2026-11"}', '{"month": "2026-11", "tokens": "x"}',
                    '{"month": "2026-11", "tokens": 1, "paused_until": "soon"}', '[]'):
            path = self.dir / "bad.json"
            path.write_text(bad)
            with self.assertLogs("trial", "ERROR"):
                self.assertEqual(asyncio.run(trial_budget.Budget(path, fetch=self.fetch_balance).stopped()), trial_budget.DAMAGED)

    def test_more_damaged_budget_files(self):
        for bad in ('{"month": "2026-13", "tokens": 5}', '{"month": "2026-11", "tokens": 1, "paused_until": 1e309}',
                    '{"month": "2026-11", "tokens": 1, "paused_until": ' + "9" * 400 + '}',
                    '{"month": "2026-11", "tokens": 1, "share": 7}'):
            path = self.dir / "bad2.json"
            path.write_text(bad)
            with self.assertLogs("trial", "ERROR"):
                self.assertTrue(trial_budget.Budget(path, fetch=self.fetch_balance).damaged, bad)

    def test_nothing_new_starts_while_pm_restarts(self):
        self.enrol()
        self.agent.stopping.set()
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.asked, [])

    def test_one_visitor_never_queues_more_than_one(self):
        self.enrol()
        release = None

        async def slow(chat, count=None, model=None):
            await release.wait()
            return "first"
        self.agent.ask_model = slow

        async def go():
            nonlocal release
            release = asyncio.Event()
            dm = self.client.rooms["!trialdm"]
            await self.agent.on_message(dm, message(NEW, "one"))
            await self.agent.on_message(dm, message(NEW, "two"))
            self.assertEqual(len(self.agent.waiting), 1)
            release.set()
            await self.agent.drain()
        self.run_(go())
        self.assertIn(trial_turn.STILL, self.said())

    def test_a_budget_refusal_gives_the_daily_message_back(self):
        self.enrol()
        with mock.patch.dict(os.environ, {"TRIAL_MONTHLY_TOKENS": "100"}):
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.agent.usage.used(NEW), 0)

    def test_a_failed_hello_is_sent_on_the_next_try(self):
        sends = []

        async def flaky(room_id, kind, content):
            sends.append(content["body"])
            return SimpleNamespace() if len(sends) == 1 else SimpleNamespace(event_id="$h")
        self.client.room_send = flaky
        self.client.rooms["!trialdm"] = room("!trialdm", NEW)
        with self.assertRaises(RuntimeError):
            self.run_(self.agent.enroll(NEW))
        self.run_(self.agent.enroll(NEW))
        self.assertEqual(len(sends), 2)
        self.assertTrue(sends[1].startswith("Hi! I'm the Server agent"))
        self.run_(self.agent.enroll(NEW))
        self.assertEqual(len(sends), 2)                           # greeted once it worked

    def test_estimates_and_counts(self):
        chat = [{"content": "x" * 400}]
        self.assertGreaterEqual(trial_model.high_estimate(chat, 100), 400 // 2 + 100)
        self.assertEqual(trial_model.tokens_of({"eval_count": 50}), 0)          # one count only: not trusted
        self.assertNotIn("/Users/someone", trial_reads.shaped("a/b", 1, {"title": "see /Users/someone/x"}, []))


class Starting(unittest.TestCase):
    def test_a_slow_sign_in_never_holds_up_pm(self):
        async def go():
            gate = asyncio.Event()

            async def slow_start(eligible, busy=None, stopping=None):
                await gate.wait()
                return "agent"
            loop = SimpleNamespace(trial=None, trial_eligible=lambda who: True, busy=set(),
                                   sync_point=SimpleNamespace(stop=asyncio.Event()))
            with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20}), mock.patch.object(trial_agent, "start", slow_start):
                trial_agent.start_later(loop)                     # returns at once
                self.assertIsNone(loop.trial)
                gate.set()
                await loop.trial_starting
            self.assertEqual(loop.trial, "agent")
        asyncio.run(go())

    def test_no_key_starts_nothing(self):
        loop = SimpleNamespace(trial=None)
        with mock.patch.dict(os.environ, {}, clear=True):
            trial_agent.start_later(loop)
        self.assertFalse(hasattr(loop, "trial_starting"))

    def test_a_failed_first_sync_never_reconciles_against_nothing(self):
        agent = SimpleNamespace(client=mock.Mock(sync=mock.AsyncMock(return_value="error"), close=mock.AsyncMock()),
                                waiting=set(), reconcile=mock.AsyncMock())
        with self.assertRaises(RuntimeError):
            asyncio.run(trial_agent.run(agent))
        agent.reconcile.assert_not_awaited()
        agent.client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
