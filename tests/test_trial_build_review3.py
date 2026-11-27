"""Regression tests for the independent review of PR #79 (trial builds, ADR 0027), one per finding.
Run: .venv/bin/python -m unittest tests.test_trial_build_review3"""
import asyncio
import time
import unittest
from unittest import mock

from server import pm_build
from server import trial_build
from server import trial_build_config
from server import trial_github as tg
from server import trial_jobs
from server import trial_patch
from server import trial_pool
from server import trial_worker
from tests import trial_build_kit as kit
from tests.test_trial_build import BuildBase
from tests.test_trial_build_flow import BuildFlowBase


def run(coro):
    return asyncio.run(coro)


class PytestConfig(unittest.TestCase):
    """HIGH: no file pytest reads its settings or plugins from may come in a patch; none is read anyway."""
    def test_every_pytest_config_file_is_refused(self):
        pristine = {"src/a.py": ("100644", b"x\n")}
        for path in (".pytest.ini", "pytest.ini", "pytest.toml", ".pytest.toml", "pyproject.toml", "setup.cfg", "tox.ini",
                     "src/conftest.py", "sub/.pytest.ini"):
            with self.assertRaises(trial_patch.Refused, msg=path):
                trial_patch.validate(pristine, {"src/a.py": b"x\n", path: b"[pytest]\ntestpaths = src\n"})

    def test_the_command_reads_no_repo_config_and_no_plugins(self):
        cmd = trial_build_config.DEFAULT_TEST_CMD
        for part in ("-c /dev/null", "--rootdir=.", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "-p no:cacheprovider", "python -P", " tests"):
            self.assertIn(part, cmd)


class PausedWorker(BuildBase):
    """HIGH + MEDIUM: while the budget is stopped or the day's sandbox time is used, nothing is claimed, the
    heartbeat says so, what waits is failed as "paused", and @pm refuses before counting."""
    def test_a_stopped_budget_claims_nothing(self):
        d = kit.deps(self.folder)

        async def stopped():
            return "paused"
        d.budget.stopped = stopped
        boxes = d.sandboxes
        _, job_id, _ = d.jobs.enqueue("@a:s", 1, "!a", 5)
        self.assertIsNone(run(trial_worker.tick(d)))
        self.assertEqual(d.jobs.all()[job_id]["outcome"]["why"], "paused")
        self.assertEqual(boxes.boxes, [])                        # no sandbox, no lease
        self.assertEqual(d.pool.entry(kit.REPO), {})
        self.assertEqual(d.jobs.alive()["paused"], "budget")
        self.assertIn("paused", trial_build.REFUNDED)

    def test_pm_refuses_a_paused_or_full_pool_before_counting(self):
        self.assertEqual(pm_build.BuildOffer.unavailable({"paused": "budget"}, {"id": 1}), pm_build.PAUSED)
        self.assertEqual(pm_build.BuildOffer.unavailable({"free_repos": 0, "lease_holders": [9]}, {"id": 1}), pm_build.NO_REPO)
        self.assertIsNone(pm_build.BuildOffer.unavailable({"free_repos": 0, "lease_holders": [1]}, {"id": 1}))   # theirs
        self.assertIsNone(pm_build.BuildOffer.unavailable({"free_repos": 2}, {"id": 1}))


class PausedFlow(BuildFlowBase):
    def test_a_paused_server_says_so_and_counts_nothing(self):
        self.worker.jobs.beat({**kit.CAPS, "paused": "budget"})
        self.assertEqual(self.click(), pm_build.PAUSED)
        self.assertEqual(self.lp._build_usage.counts(777), (0, 0, 0))
        self.assertEqual(self.lp.trial_view(self.lp_person())["build"]["state"], "paused for now")

    def test_every_repo_held_by_others_counts_nothing(self):
        self.worker.jobs.beat({**kit.CAPS, "free_repos": 0, "lease_holders": [5, 6, 7]})
        self.assertEqual(self.click(), pm_build.NO_REPO)
        self.assertEqual(self.lp._build_usage.counts(777), (0, 0, 0))

    def test_a_server_side_failure_gives_the_build_back(self):
        self.click()
        self.assertEqual(self.lp._build_usage.counts(777)[0], 1)
        (job_id,) = self.worker.jobs.all()
        self.worker.jobs.claim()
        self.worker.jobs.finish(job_id, trial_jobs.FAILED, {"why": "no_repo"})
        self.run_(self.lp.relay_builds_once())
        self.assertEqual(self.lp._build_usage.counts(777)[:2], (0, 0))
        self.assertIn("3 builds left today", self.dm()[-1])
        self.run_(self.lp.relay_builds_once())                  # once only
        self.assertEqual(self.lp._build_usage.counts(777)[:2], (0, 0))

    def test_a_failed_build_of_its_own_is_not_given_back(self):
        self.click()
        (job_id,) = self.worker.jobs.all()
        self.worker.jobs.claim()
        self.worker.jobs.finish(job_id, trial_jobs.FAILED, {"why": "tests_failed"})
        self.run_(self.lp.relay_builds_once())
        self.assertEqual(self.lp._build_usage.counts(777)[:2], (1, 1))

    def test_clicks_in_a_row_make_one_request(self):
        self.run_(self.lp.trial_action(self.lp_person(), "trial_build"))
        again = self.run_(self.lp.trial_action(self.lp_person(), "trial_build"))
        self.assertEqual(again, pm_build.TOO_SOON)

    def lp_person(self):
        from tests.test_welcome import NEW
        return NEW


class Pool(unittest.TestCase):
    """MEDIUM: one broken repo never blocks the others; the status says what's free."""
    def setUp(self):
        repos = [kit.REPO, "hagency-trials/demo-2"]
        self.fake = kit.FakeTrialGitHub({r: kit.TEMPLATE for r in repos})
        self.gh = tg.TrialGitHub("1", str(kit.key_file()), repos, call=self.fake.call, fetch=self.fake.fetch)
        self.now = [1000.0]
        self.pool = trial_pool.Pool(kit.key_file().parent / f"pool-{time.time_ns()}.json", repos, 1, clock=lambda: self.now[0])

    def test_a_refused_repo_is_skipped(self):
        real = self.gh.token

        async def broken(repo, write=False):
            if repo == kit.REPO:
                raise tg.TrialGitHubError("not installed")
            return await real(repo, write)
        self.gh.token = broken
        with self.assertLogs("trial-build", "WARNING"):
            self.assertEqual(run(self.pool.lease(self.gh, 1)), "hagency-trials/demo-2")

    def test_status_counts_free_repos_and_holders(self):
        self.assertEqual(run(self.pool.status(self.gh)), (2, []))
        run(self.pool.lease(self.gh, 1))
        self.assertEqual(run(self.pool.status(self.gh)), (1, [1]))
        self.now[0] += trial_pool.IDLE_S                       # nothing opened in an hour: free again
        self.assertEqual(run(self.pool.status(self.gh)), (2, []))

    def test_leases_are_a_day_by_default(self):
        self.assertEqual(trial_build_config.DEFAULTS["TRIAL_LEASE_DAYS"], 1)


class Exits(BuildBase):
    """LOW: a worker that isn't configured leaves nothing waiting."""
    def test_not_configured_fails_what_waits(self):
        jobs = trial_jobs.Jobs(self.folder)
        jobs.beat(kit.CAPS)
        _, job_id, _ = jobs.enqueue("@a:s", 1, "!a", 5)
        with kit.configured_env(self.folder, TRIAL_ORG=""):
            self.assertEqual(run(trial_worker.main()), 0)
        self.assertEqual(jobs.all()[job_id]["outcome"]["why"], "off")


class Reviewer(BuildBase):
    """LOW: the reviewer respects a paused budget, and a 429 to it pauses builds."""
    def test_no_review_call_while_paused_and_a_429_pauses(self):
        d = kit.deps(self.folder)
        d.ask_review = None

        async def stopped():
            return "paused"
        with mock.patch.object(d.budget, "stopped", stopped), mock.patch.object(trial_build.trial_model, "ask") as ask:
            with self.assertRaises(trial_build.trial_model.TrialModelError):
                run(trial_build.review_ask(d)([{"role": "user", "content": "x"}]))
            ask.assert_not_called()

        async def limited(*a, **k):
            raise trial_build.trial_model.TrialLimited(429, 120)

        async def fine():
            return None
        with mock.patch.object(d.budget, "stopped", fine), mock.patch.object(trial_build.trial_model, "ask", limited), \
                mock.patch.object(d.budget, "pause") as pause:
            with self.assertRaises(trial_build.trial_model.TrialLimited):
                run(trial_build.review_ask(d)([{"role": "user", "content": "x"}]))
        pause.assert_called_once()


if __name__ == "__main__":
    unittest.main()
