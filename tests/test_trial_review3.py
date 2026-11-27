"""Regression tests for the independent review of PR #73 (ADR 0025): a per-visitor token allowance,
"still"/"busy" said once and never blocking sync, "use <model> for the trial", the trial down while
its key is set, a failed sign-in logged, honest stop words. Run: .venv/bin/python -m unittest tests.test_trial_review3"""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from server import pm_trial
from server import trial_agent
from server import trial_budget
from server import trial_turn
from tests.test_trial_agent import NEW, Base, hear, message
from tests.test_trial_flow import TrialBase, said_to
from tests.test_welcome import DM, reaction, text

FAR = "@visitor:elsewhere.example"


class TokenAllowance(Base):
    def counted(self, used):
        async def model(chat, count=None, model=None):
            self.asked.append(chat)
            count(used)
            return "ok"
        self.agent.ask_model = model

    def test_a_visitor_has_a_daily_token_allowance_of_their_own(self):
        self.enrol()
        self.counted(9000)
        with mock.patch.dict(os.environ, {"TRIAL_VISITOR_TOKENS": "10000", "TRIAL_PER_VISITOR": "50"}):
            for n in range(3):
                self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, f"q{n}")))
        self.assertEqual(len(self.asked), 1)                          # 9000 used: the next estimate doesn't fit
        self.assertIn("today's trial allowance", self.said()[2])
        self.assertEqual(len(self.said()), 3)                          # said once, then quiet
        self.assertEqual(self.agent.usage.tokens_used(NEW), 9000)      # settled with Ollama's count
        self.assertEqual(self.agent.usage.used(NEW), 1)                # refused ones don't count as messages

    def test_people_from_other_servers_get_a_smaller_allowance(self):
        with mock.patch.dict(os.environ, {"TRIAL_VISITOR_TOKENS": "150000", "TRIAL_REMOTE_TOKENS": "1000"}):
            self.assertTrue(self.agent.usage.reserve_tokens(NEW, 5000, remote=False))
            self.assertFalse(self.agent.usage.reserve_tokens(FAR, 5000, remote=True))

    def test_a_request_too_big_for_whats_left_says_so_every_time(self):
        self.enrol()
        with mock.patch.dict(os.environ, {"TRIAL_VISITOR_TOKENS": "150000", "TRIAL_MONTHLY_TOKENS": "2000"}):
            for n in range(2):
                self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "x" * 4000)))
        self.assertEqual(self.said()[1:], [trial_turn.TOO_BIG, trial_turn.TOO_BIG])   # never an empty reply
        self.assertEqual(self.agent.usage.tokens_used(NEW), 0)        # given back to their day too


class Notices(Base):
    def test_still_and_busy_are_said_once_per_answer_and_never_block_sync(self):
        self.enrol()
        release, sends = None, []
        real_send = self.client.room_send

        async def slow_send(room_id, kind, content):
            sends.append(content["body"])
            await asyncio.sleep(0.2)                                    # Synapse slow: sync must not wait on it
            return await real_send(room_id, kind, content)

        async def slow(chat, count=None, model=None):
            await release.wait()
            return "done"
        self.agent.ask_model = slow

        async def go():
            nonlocal release
            release = asyncio.Event()
            dm = self.client.rooms["!trialdm"]
            await self.agent.on_message(dm, message(NEW, "one"))
            self.client.room_send = slow_send
            for n in range(20):                                        # a flood while it answers
                await asyncio.wait_for(self.agent.on_message(dm, message(NEW, f"more {n}")), 0.05)
            release.set()
            await self.agent.drain()
            await asyncio.gather(*list(self.agent.busy), return_exceptions=True)
        self.run_(go())
        self.assertEqual(sends.count(trial_turn.STILL), 1)
        self.assertEqual(self.agent.told_busy, set())                  # the next answer may say it again


class UseModel(TrialBase):
    def start(self):
        self.join_welcome()
        self.run_(self.lp.on_welcome_reaction(DM, reaction(NEW, "🧪")))

    def test_use_a_model_for_the_trial_changes_the_model(self):
        self.start()
        for words, name in (("use glm-5.3 for the trial", "glm-5.3"), ("use gpt-oss:120b in trial", "gpt-oss:120b"),
                            ("use kimi-k3.1-pro for trial", "kimi-k3.1-pro"), ("use glm-5.3 in my trial agent", "glm-5.3")):
            self.lp.trial.set_model(NEW, "deepseek-v4.1-flash")
            self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, words))))
            self.assertEqual(self.lp.trial.model_of(NEW), name, words)
            self.assertEqual(said_to(self, "!dm")[-1], pm_trial.MODEL_SET.format(name=name))

    def test_try_the_trial_still_starts_it(self):
        self.join_welcome()
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "I want to use the trial"))))
        self.assertTrue(said_to(self, "!dm")[-1].startswith("🧪 Your trial agent is ready"))


class Down(TrialBase):
    def test_while_down_a_click_and_the_words_both_say_unavailable(self):
        self.join_welcome()
        self.lp.trial.task = SimpleNamespace(done=lambda: True)
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "try the trial agent"))))
        self.assertEqual(said_to(self, "!dm")[-1], pm_trial.UNAVAILABLE)
        with mock.patch.dict(os.environ, {}, clear=True):           # no key at all: never mine, as before
            self.assertFalse(self.run_(self.lp.on_trial_text(DM, text(NEW, "try the trial agent"))))

    def test_a_failed_hello_is_said_plainly(self):
        self.join_welcome()
        self.lp.trial.enroll = mock.AsyncMock(side_effect=RuntimeError("couldn't post in !AbC:server: RoomSendError M_FORBIDDEN"))
        with self.assertLogs("loop", "ERROR"):
            said = self.run_(self.lp.start_trial("!dm", NEW))
        self.assertNotIn("M_FORBIDDEN", said)
        self.assertNotIn("!AbC", said)


class Words(unittest.TestCase):
    def test_each_stop_says_what_is_true(self):
        self.assertIn("operator", trial_turn.STOPPED[trial_budget.DAMAGED])
        self.assertIn("few hours", trial_turn.STOPPED[trial_budget.WINDOW])
        self.assertNotIn("month", trial_turn.STOPPED[trial_budget.WINDOW])
        legacy = {"included": {"session": {"remaining_percent": 10}}}
        self.assertTrue(trial_budget.is_window(legacy))
        self.assertFalse(trial_budget.is_window({"included": {"allowance_usd": 100, "balance_usd": 5}}))

    def test_an_older_plans_window_is_said_as_such(self):
        import tempfile
        from pathlib import Path

        async def fetch():
            return {"included": {"session": {"remaining_percent": 10}}}
        b = trial_budget.Budget(Path(tempfile.mkdtemp()) / "b.json", fetch=fetch)
        self.assertEqual(asyncio.run(b.stopped()), trial_budget.WINDOW)


class SignInFailure(unittest.TestCase):
    def test_any_sign_in_failure_is_logged_and_leaves_it_off(self):
        async def go():
            loop = SimpleNamespace(trial=None, trial_eligible=lambda who: True, busy=set(),
                                   sync_point=SimpleNamespace(stop=asyncio.Event()))
            with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20}), \
                    mock.patch.object(trial_agent, "start", mock.AsyncMock(side_effect=KeyError("nonce"))), \
                    self.assertLogs("trial", "ERROR") as logged:
                trial_agent.start_later(loop)
                await loop.trial_starting
            self.assertIsNone(loop.trial)
            self.assertIn("not started", logged.output[0])
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
