"""Flow tests for trial builds (ADR 0027), as a visitor lives them: "Try a build" on the Server agent card
(or "try a build" to @pm) → the gate → queued → the worker builds in a (fake) sandbox → the patch is
checked → a no-network test sandbox → a PR → the AI self-check comment → @pm: "a person merges".
Also: refusals said plainly, the caps, the gate, and with no keys nothing changes.
Run: .venv/bin/python -m unittest tests.test_trial_build_flow"""
import asyncio
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common.saved_dict import SavedDict
from server import github_link
from server import pm_build
from server import trial_gate
from server import trial_jobs
from server import trial_tiers
from server import trial_worker
from tests import trial_build_kit as kit
from tests.test_trial_flow import TrialBase, said_to
from tests.test_welcome import NEW, S, text

OLD_ACCOUNT = time.time() - 400 * 86400


class BuildFlowBase(TrialBase):
    def setUp(self):
        super().setUp()
        self.folder = Path(tempfile.mkdtemp())
        env = kit.configured_env(self.folder)
        env.start()
        self.addCleanup(env.stop)
        links = SavedDict(self.folder / "links.json")
        links[NEW] = {"login": "anna-gh", "id": 777}
        for p in (mock.patch.object(github_link, "_links", links), mock.patch.object(trial_gate, "_ages", {}),
                  mock.patch.object(trial_gate, "_created_at", lambda gid: OLD_ACCOUNT)):
            p.start()
            self.addCleanup(p.stop)
        self.lp._build_jobs = trial_jobs.Jobs(self.folder)
        self.lp._build_usage = trial_gate.Usage(self.folder / "usage.json")
        self.worker = kit.deps(self.folder)
        self.worker.jobs.beat(kit.CAPS)                       # the trial-worker is up

    def click(self, who=NEW):
        """What the panel shows after "Try a build": its words (an ok result carries them in "says")."""
        self.lp._build_asks = {}              # each click as if a while after the last (pm_build.too_soon)
        said = self.run_(self.lp.trial_action(who, "trial_build"))
        return said["says"] if isinstance(said, dict) else said

    def work_and_relay(self):
        asyncio.run(trial_worker.tick(self.worker))
        self.run_(self.lp.relay_builds_once())

    def dm(self):
        return said_to(self, "!dm")


class TheFlow(BuildFlowBase):
    def test_try_a_build_from_the_card_through_to_a_person_merges(self):
        card = self.lp.trial_view(NEW)["build"]
        self.assertEqual((card["left"], card["per_day"], card["github"], card["state"]), (3, 3, True, None))
        said = self.run_(self.lp.trial_action(NEW, "trial_build"))
        self.assertEqual(said["result"], "ok")
        self.assertIn("queued: you're #1", said["says"])
        self.assertIn("E2B", self.dm()[-1])                    # told plainly where the code goes
        self.assertEqual(self.lp.trial_view(NEW)["build"]["state"], "queued #1")
        self.work_and_relay()
        started, done = self.dm()[-2:]
        self.assertIn("has started: issue #1 in hagency-trials/demo-1", started)
        self.assertIn("pull request #100", done)
        self.assertIn("Ready: a person merges", done)
        self.assertIn("AI self-check", done)
        self.assertIn("Check negative numbers too", done)      # the reviewer's words, as an excerpt
        gh = self.worker.fake
        self.assertEqual(gh.reviews[0][1]["event"], "COMMENT")
        self.assertFalse([r for r in gh.requests if r[0] == "PUT"])   # no merge, ever
        self.assertEqual(self.lp.trial_view(NEW)["build"]["left"], 2)
        self.run_(self.lp.relay_builds_once())
        self.assertEqual(len(self.dm()), 3)                    # each piece of news once

    def test_typing_try_a_build_to_pm_works_too(self):
        self.join_welcome()                                    # our private chat with them: "!dm"
        room = SimpleNamespace(room_id="!dm", users={NEW: None})
        self.assertTrue(self.run_(self.lp.on_trial_text(room, text(NEW, "can I try a build?"))))
        self.assertIn("queued", self.dm()[-1])

    def test_a_refusal_or_other_talk_of_builds_never_queues_one(self):
        self.join_welcome()
        room = SimpleNamespace(room_id="!dm", users={NEW: None})
        for words in ("don't start a server agent build", "I want to build a house", "bring my own agent to try a build"):
            self.run_(self.lp.on_build_text(room, text(NEW, words)))
        self.assertFalse(self.worker.jobs.all())               # GPT review of the rebase: nothing queued

    def test_a_refused_patch_is_said_plainly_and_nothing_opens(self):
        self.worker.ask_build = kit.scripted(kit.tool("write_file", path="tests/test_calc.py", content="def test_add():\n    pass\n"))
        self.click()
        self.work_and_relay()
        self.assertIn("didn't finish: the change was refused, because it changed the tests", self.dm()[-1])
        self.assertIn("Nothing was opened", self.dm()[-1])
        self.assertIn("2 builds left today", self.dm()[-1])
        self.assertFalse(self.worker.fake.pulls)

    def test_model_words_reach_the_room_without_links_mentions_or_markup(self):
        out = pm_build.excerpt("See https://evil.example/x @room <b>hi</b> [x](y)")
        self.assertNotIn("https://", out)
        self.assertNotIn("@room", out)
        self.assertNotIn("<b>", out)
        self.assertIn("\\[x\\]", out)


class Gate(BuildFlowBase):
    def test_no_github_link(self):
        github_link._links.pop(NEW)
        self.assertIn("Connect GitHub", self.click())
        self.assertFalse(self.worker.jobs.all())

    def test_a_young_github_account(self):
        with mock.patch.object(trial_gate, "_created_at", lambda gid: time.time() - 5.5 * 86400):
            self.assertIn("at least 30 days old; yours is 5 days old", self.click())

    def test_an_unreadable_age_never_lets_it_through(self):
        with mock.patch.object(trial_gate, "_created_at", lambda gid: None):
            self.assertEqual(self.click(), trial_gate.NO_AGE)

    def test_people_from_other_servers(self):
        far = "@bob:elsewhere.example"
        said = self.run_(trial_gate.refusal(far, {"login": "b", "id": 1}, kit.CAPS, kit.FREE, self.lp._build_usage, time.time()))
        self.assertEqual(said, trial_gate.REMOTE)

    def test_owning_an_agent_no_longer_excludes(self):
        with mock.patch.object(type(self.lp), "has_connected_agent", lambda self, p: True):   # NEW's own agent
            self.assertIsNotNone(self.lp.build_view(NEW))
            self.assertIn("queued", self.click())

    def test_someone_who_may_not_use_the_server_agent(self):
        with mock.patch.object(type(self.lp), "trial_eligible", lambda self, p: False):
            self.assertIsNone(self.lp.build_view(NEW))
            self.assertEqual(self.run_(self.lp.start_build("!dm", NEW)), pm_build.NOT_FOR_YOU)

    def test_the_allowance_is_the_tiers(self):
        self.assertEqual(self.lp.build_allowance(NEW), kit.FREE)
        paid = {**trial_tiers.TIERS[trial_tiers.FREE], trial_tiers.BUILDS_DAY: 7, trial_tiers.BUILDS_TOTAL: 50}
        with mock.patch.dict(trial_tiers.TIERS, {"paid": paid}):
            self.assertTrue(self.lp.trial.tiers.set_tier(NEW, "paid"))
            self.assertEqual(self.lp.build_allowance(NEW), (7, 50))
            self.assertEqual(self.lp.trial_view(NEW)["build"]["per_day"], 7)


class Caps(BuildFlowBase):
    def test_three_a_day_per_github_account(self):
        for _ in range(3):
            self.click()
            self.work_and_relay()
        self.assertEqual(self.click(), trial_gate.PER_DAY.format(n=3))

    def test_twenty_a_day_for_the_server_and_the_demo_allowlist(self):
        usage = self.lp._build_usage
        usage.data = {"day": trial_gate.day(time.time()), "server": 20, "today": {}, "total": {}}
        self.assertEqual(self.click(), trial_gate.SERVER)
        self.worker.jobs.beat({**kit.CAPS, "demo_users": [NEW]})
        self.assertIn("queued", self.click())

    def test_ten_ever_per_account(self):
        self.lp._build_usage.data = {"day": trial_gate.day(time.time()), "server": 0, "today": {}, "total": {"777": 10}}
        self.assertIn("all 10 of your server agent builds", self.click())

    def test_the_queue_holds_five(self):
        for n in range(5):
            self.worker.jobs.enqueue(f"@v{n}:{S}", n, "!x", 5)
        self.assertIn("busy: 5 are waiting", self.click())
        self.assertEqual(self.lp._build_usage.counts(777)[0], 0)   # not counted: it never queued

    def test_one_at_a_time_per_visitor(self):
        self.click()
        self.assertIn("queued (#1)", self.click())
        self.assertEqual(self.lp._build_usage.counts(777)[0], 1)


class Off(BuildFlowBase):
    def test_no_heartbeat_no_offer_and_the_words_fall_through(self):
        self.worker.jobs.stop_beating()
        self.assertNotIn("build", self.lp.trial_view(NEW))
        room = SimpleNamespace(room_id="!dm", users={NEW: None})
        self.assertFalse(self.run_(self.lp.on_build_text(room, text(NEW, "try a build"))))

    def test_without_the_keys_the_worker_exits_and_writes_no_heartbeat(self):
        self.worker.jobs.stop_beating()
        for gone in ("E2B_API_KEY", "TRIAL_APP_ID", "TRIAL_ORG", "OLLAMA_API_KEY"):
            with kit.configured_env(self.folder, **{gone: ""}):
                self.assertEqual(asyncio.run(trial_worker.main()), 0)
                self.assertIsNone(self.lp.build_caps())
        with kit.configured_env(self.folder, TRIAL_APP_PRIVATE_KEY_PATH=str(self.folder / "missing.pem")):
            self.assertEqual(asyncio.run(trial_worker.main()), 0)
        self.assertIsNone(self.worker.jobs.alive())

    def test_the_kill_switch_fails_waiting_builds_and_hides_the_offer(self):
        self.click()
        with kit.configured_env(self.folder, TRIAL_BUILD="off", E2B_API_KEY=""):
            self.assertEqual(asyncio.run(trial_worker.main()), 0)
        self.assertIsNone(self.lp.build_caps())
        self.run_(self.lp.relay_builds_once())
        self.assertIn("trial builds were switched off", self.dm()[-1])

    def test_without_ollama_the_talk_trial_is_off_and_so_are_builds(self):
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": ""}):
            self.lp.trial = None
            self.assertIsNone(self.lp.build_caps())
            self.assertIsNone(self.lp.trial_view(NEW))
