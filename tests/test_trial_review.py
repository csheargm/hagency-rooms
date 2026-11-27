"""Regression tests for the GPT review of the trial agent (ADR 0025): quotes, stale rooms, the budget
reservation, enrolment races, restarts, sync never blocked. Run: .venv/bin/python -m unittest tests.test_trial_review"""
import asyncio
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import trial_agent
from server import trial_budget
from server import trial_model
from server import trial_models
from server import trial_reads
from server import trial_turn
from tests.test_trial_agent import ANNA, ME, NEW, Base, hear, message, room
from tests.test_trial_flow import TrialBase, said_to
from tests.test_welcome import DM, reaction

NOON = datetime.datetime(2026, 11, 27, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


class Quotes(Base):
    def test_a_replys_quote_never_reaches_the_model_nor_picks_a_pr_nor_mentions(self):
        self.enrol()
        quoted = "> <@anna:bridge.localhost> my secret plan, see PR #9\n\nwhat do you think?"
        with mock.patch.object(trial_reads, "pr_diff", mock.AsyncMock()) as read:
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, quoted)))
        read.assert_not_awaited()
        self.assertNotIn("secret plan", str(self.asked[0]))
        self.assertEqual(self.asked[0][-1]["content"], "what do you think?")
        project = room("!proj", NEW)
        self.agent.rooms["!proj"] = [NEW]
        self.run_(hear(self.agent, project, message(NEW, "> <@x:y> Trial agent, hi\n\nnice")))
        self.assertEqual(len(self.asked), 1)                     # "Trial agent" only in the quote: not for it


class StaleRooms(Base):
    def test_nothing_is_posted_after_the_visitor_left_meanwhile(self):
        self.enrol()
        dm = self.client.rooms["!trialdm"]

        async def leaves(chat, count=None, model=None):
            dm.users.pop(NEW)
            return "late"
        self.agent.ask_model = leaves
        self.run_(hear(self.agent, dm, message(NEW, "hi")))
        self.assertNotIn("late", " ".join(self.said()))

    def test_a_pr_answer_isnt_posted_once_the_rooms_repo_changed(self):
        self.enrol()
        for repos, asked in ((["acme/app", "other/repo"], 0),                  # rebound while GitHub answered
                             (["acme/app", "acme/app", "other/repo"], 1)):     # rebound while the model answered
            seq = iter(repos)
            self.asked.clear()
            with mock.patch.object(trial_turn.room_state, "repo_of", mock.AsyncMock(side_effect=lambda *a: next(seq))), \
                    mock.patch.object(trial_reads, "pr_diff", mock.AsyncMock(return_value="diff")):
                self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "PR #3?")))
            self.assertEqual(len(self.asked), asked)
            self.assertFalse(any(s.startswith("An answer") for s in self.said()))
            self.assertEqual(self.agent.history.get(("!trialdm", NEW)), None)  # never remembered either
        self.assertEqual(self.agent.usage.used(NEW), 1)                        # the first never reached the model

    def test_at_start_rooms_its_visitors_left_are_left(self):
        self.enrol()
        self.agent.rooms["!old"] = [NEW]
        self.client.rooms["!old"] = room("!old", ANNA)            # NEW left while @pm was down
        self.agent.rooms["!gone"] = [NEW]                         # a room it is no longer in at all
        self.run_(self.agent.reconcile())
        self.client.room_leave.assert_awaited_once_with("!old")
        self.assertEqual(set(self.agent.rooms), {"!trialdm"})


class Sync(Base):
    def test_sync_never_waits_on_the_model(self):
        self.enrol()
        release = None

        async def slow(chat, count=None, model=None):
            await release.wait()
            return "slow"
        self.agent.ask_model = slow

        async def go():
            nonlocal release
            release = asyncio.Event()
            await asyncio.wait_for(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, "hi")), 1)
            self.assertEqual(len(self.agent.waiting), 1)          # the callback returned; the answer runs on
            release.set()
            await self.agent.drain()
        self.run_(go())
        self.assertTrue(self.said()[-1].startswith("slow"))

    def test_too_many_waiting_answers_get_busy_at_once(self):
        self.enrol()
        self.agent.waiting = {object() for _ in range(trial_turn.MAX_WAITING)}
        self.run_(self.agent.on_message(self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.said()[-1], trial_turn.BUSY)

    def test_a_disk_error_is_said_and_never_stops_the_agent(self):
        self.enrol()
        with mock.patch.object(self.agent.usage, "take", side_effect=OSError("disk full")):
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertIn("went wrong on the server", self.said()[-1])


class Reservation(Base):
    def test_a_call_that_could_pass_the_token_cap_isnt_made(self):
        self.enrol()
        with mock.patch.dict(os.environ, {"TRIAL_MONTHLY_TOKENS": "1000", "TRIAL_MAX_TOKENS": "1500"}):
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.asked, [])
        self.assertIn(trial_turn.RESTING, self.said()[-1])

    def test_the_estimate_is_reserved_then_replaced_and_kept_on_failure(self):
        self.enrol()

        async def counted(chat, count=None, model=None):
            self.assertGreater(self.budget.data["tokens"], 1500)  # reserved before the call
            count(40)
            return "ok"
        self.agent.ask_model = counted
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "hi")))
        self.assertEqual(self.budget.data["tokens"], 40)

        async def fails(chat, count=None, model=None):
            raise trial_model.TrialModelError("the AI service didn't answer in time")
        self.agent.ask_model = fails
        self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "again")))
        self.assertGreater(self.budget.data["tokens"], 1500)     # a timed-out call may have cost: it stays counted


class BudgetFile(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "trial-budget.json"
        self.now, self.balance = NOON, None

    async def fetch(self):
        return self.balance

    def budget(self):
        return trial_budget.Budget(self.path, clock=lambda: self.now, fetch=self.fetch)

    def test_a_damaged_file_stops_trials(self):
        self.path.write_text("{broken")
        with self.assertLogs("trial", "ERROR"):
            b = self.budget()
        self.assertEqual(asyncio.run(b.stopped()), trial_budget.TOKENS)

    def test_an_over_reading_survives_a_restart(self):
        self.balance = {"included": {"balance_usd": 10, "allowance_usd": 100}}
        self.assertEqual(asyncio.run(self.budget().stopped()), trial_budget.SHARE)
        self.balance = None                                        # Ollama unreachable after the restart
        self.assertEqual(asyncio.run(self.budget().stopped()), trial_budget.SHARE)

    def test_near_the_limit_the_balance_is_read_every_minute(self):
        calls = []

        async def fetch():
            calls.append(1)
            return {"included": {"balance_usd": 55, "allowance_usd": 100}}   # 45%: near 50%
        b = trial_budget.Budget(self.path, clock=lambda: self.now, fetch=fetch)
        asyncio.run(b.stopped())
        self.now += 61
        asyncio.run(b.stopped())
        self.assertEqual(len(calls), 2)

    def test_pauses_are_kept_within_bounds(self):
        b = self.budget()
        with self.assertLogs("trial", "ERROR"):
            b.pause(float("inf"), "429")
            self.assertEqual(b.paused_for(), 900)                 # not finite: the default
            b.pause(5, "429")
            self.assertEqual(b.paused_for(), 60)
            b.pause(10**9, "402")
            self.assertEqual(b.paused_for(), 24 * 3600)


class RetryAfter(unittest.TestCase):
    def test_seconds_and_http_dates(self):
        self.assertEqual(trial_model.retry_after({"Retry-After": "120"}), 120.0)
        when = "Fri, 27 Nov 2026 12:10:00 GMT"
        self.assertEqual(trial_model.retry_after({"Retry-After": when}, now=NOON), 600.0)
        for bad in ("", "soon", "inf", "-5", "nan"):
            self.assertIsNone(trial_model.retry_after({"Retry-After": bad}))


class Enrolment(Base):
    def test_two_requests_at_once_make_one_chat_and_keep_the_model(self):
        async def slow_create(**kw):
            await asyncio.sleep(0.05)
            return SimpleNamespace(room_id="!trialdm")
        self.client.room_create = mock.AsyncMock(side_effect=slow_create)

        async def both():
            return await asyncio.gather(self.agent.enroll(NEW), self.agent.enroll(NEW))
        self.assertEqual(self.run_(both()), ["!trialdm", "!trialdm"])
        self.assertEqual(self.client.room_create.await_count, 1)  # sync hasn't shown it yet: still one
        self.agent.set_model(NEW, "gpt-oss:120b")
        self.agent.visitors[NEW] = {**self.agent.visitors[NEW], "since": 0}   # an old chat they left
        self.run_(self.agent.enroll(NEW))
        self.assertEqual(self.agent.model_of(NEW), "gpt-oss:120b")


class Secrets(unittest.TestCase):
    def test_the_exact_ollama_key_is_cut_even_if_it_looks_harmless(self):
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "plainlookingkey0123"}):
            self.assertNotIn("plainlookingkey0123", trial_model.scrub("my key plainlookingkey0123", 100))
            self.assertNotIn("plainlookingkey0123", trial_reads.shaped("a/b", 1, {}, [{"patch": "+plainlookingkey0123"}]))


class Allowlist(unittest.TestCase):
    def test_a_wholly_malformed_list_never_enables_every_bundled_model(self):
        with mock.patch.dict(os.environ, {"TRIAL_MODELS": "glm-5.3*x"}):
            self.assertEqual(trial_models.names(), ["glm-5.3"])

    def test_deploy_refuses_what_the_server_would_drop(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("envfile", Path(__file__).resolve().parent.parent / "deploy" / "envfile.py")
        envfile = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(envfile)
        import re
        rule = envfile.RULES["TRIAL_MODELS"]
        self.assertTrue(re.fullmatch(rule, "glm-5.3,kimi-k3.1-pro*2,gpt-oss:120b"))
        for bad in ("glm-5.3*x", "glm-5.3,,x", "GLM", "a*1.234"):
            self.assertFalse(re.fullmatch(rule, bad), bad)
        self.assertTrue(re.fullmatch(trial_models.ENTRY, "kimi-k3.1-pro*2"))


class SignIn(unittest.TestCase):
    def test_a_saved_password_without_an_account_is_registered_again(self):
        calls = {}

        def ensure(env, user, secret, admin=False, verify=False):
            calls["verify"] = verify
            return "pw", True
        client = mock.Mock(user_id=ME, close=mock.AsyncMock(), set_displayname=mock.AsyncMock())
        with mock.patch.object(trial_agent.setup_accounts, "load_env", lambda: {}), \
                mock.patch.object(trial_agent.setup_accounts, "shared_secret", lambda: "s"), \
                mock.patch.object(trial_agent.setup_accounts, "api", lambda *a, **k: {"_status": 404}), \
                mock.patch.object(trial_agent.setup_accounts, "ensure_user", ensure), \
                mock.patch.object(trial_agent, "AsyncClient", lambda *a, **k: client), \
                mock.patch.object(trial_agent.lifecycle, "login", mock.AsyncMock(return_value=trial_agent.LoginResponse("u", "d", "t"))):
            asyncio.run(trial_agent.sign_in())
        self.assertTrue(calls["verify"])
        client.set_displayname.assert_awaited_once_with(trial_agent.DISPLAY_NAME)


class WelcomeOff(TrialBase):
    def test_a_hand_added_flask_while_off_isnt_used_up(self):
        self.join_welcome()
        self.lp.trial.task = SimpleNamespace(done=lambda: True)    # off (crashed): 🧪 means nothing
        before = len(said_to(self, "!dm"))
        self.run_(self.lp.on_welcome_reaction(DM, reaction(NEW, "🧪")))
        self.assertNotIn("🧪", self.lp._welcomes[NEW]["done"])  # still usable once it's back
        self.assertEqual(len(said_to(self, "!dm")), before)       # nothing said
        self.trial_client.room_create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
