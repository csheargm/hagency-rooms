"""Regression tests for the GPT review of trial builds (ADR 0027), one per verified finding.
Run: .venv/bin/python -m unittest tests.test_trial_build_review"""
import asyncio
import unittest
from pathlib import Path
from unittest import mock

from common import model_slot
from common import sandbox
from common import sandbox_fake
from server import pm_build
from server import trial_build
from server import trial_github as tg
from server import trial_jobs
from server import trial_patch
from server import trial_worker
from tests import trial_build_kit as kit
from tests.test_trial_build import BuildBase

DOCS = Path(__file__).resolve().parent.parent / "docs"


def run(coro):
    return asyncio.run(coro)


class VacuousPasses(BuildBase):
    """Finding 2: a run that tests nothing, or a runner from the repo, is never a pass."""
    def verdict(self, report: str | None, code: int = 0):
        def on_run(box, cmd):
            if box.kind == sandbox.TEST and "--junitxml" in cmd:
                if report is not None:
                    box.outside[trial_build.REPORT] = report.encode()
                return sandbox.Result(code)
            return kit.on_run(box, cmd)
        job = self.queue_and_run(kit.deps(self.folder, sandboxes=sandbox_fake.FakeSandboxes(on_run)))
        return job["outcome"].get("why", job["state"])

    def test_all_skipped_or_no_report_or_failures_despite_exit_0(self):
        self.assertEqual(self.verdict(kit.junit(passed=0, skipped=3)), "tests_unrun")
        self.assertEqual(self.verdict(None), "tests_unrun")
        self.assertEqual(self.verdict("<html>not a report</html>"), "tests_unrun")
        self.assertEqual(self.verdict(kit.junit(passed=2, failed=1)), "tests_failed")
        self.assertEqual(self.verdict(kit.junit(passed=1), code=1), "tests_failed")
        self.assertEqual(self.verdict(kit.junit(passed=1)), trial_jobs.DONE)

    def test_a_stand_in_for_the_runner_is_refused(self):
        pristine = {"src/a.py": ("100644", b"x\n")}
        for path in ("pytest.py", "pytest/__init__.py", "_pytest/x.py", "src/evil.pth", "pluggy/__init__.py"):
            with self.assertRaises(trial_patch.Refused, msg=path) as e:
                trial_patch.validate(pristine, {"src/a.py": b"x\n", path: b"x\n"})
            self.assertEqual(e.exception.rule, trial_patch.TESTS)

    def test_the_test_command_keeps_the_repo_off_the_front_of_sys_path(self):
        self.assertIn("python -P -m pytest", trial_build.config.DEFAULT_TEST_CMD)

    def test_finish_after_the_deadline_is_a_timeout(self):
        clock = iter([0, 0, 10_000, 10_000, 10_000])
        loop = trial_build.trial_tools.ToolLoop(None, model_slot.Slot(model_slot.lock_path(self.folder), model_slot.BUILD),
                                                kit.deps(self.folder).budget, kit.scripted(kit.tool("finish", summary="x")),
                                                clock=lambda: next(clock), seconds=100)
        self.assertEqual(run(loop.run([{"role": "user", "content": "go"}])).reason, "time")


class Bounded(BuildBase):
    """Finding 3: nothing big is pulled into the worker."""
    def test_changed_files_are_refused_by_listed_size_before_any_read(self):
        def big(box, cmd):
            if "make-big" in cmd:
                box.files["src/big.py"] = b"x" * (trial_patch.MAX_BYTES + 1)
            return kit.on_run(box, cmd)
        boxes = sandbox_fake.FakeSandboxes(big)
        reads = []
        original = sandbox_fake.FakeBox.read

        async def counted(self, path):
            reads.append(path)
            return await original(self, path)
        with mock.patch.object(sandbox_fake.FakeBox, "read", counted):
            job = self.queue_and_run(kit.deps(self.folder, sandboxes=boxes, ask_build=kit.scripted(kit.tool("run", command="make-big"))))
        self.assertEqual(job["outcome"]["rule"], trial_patch.TOO_BIG)
        self.assertNotIn("src/big.py", reads)

    def test_a_huge_tree_is_refused_while_unpacking(self):
        with mock.patch.object(tg, "MAX_EXPANDED", 10):
            with self.assertRaises(tg.TrialGitHubError):
                tg.unpack(kit.tarball_of(kit.TEMPLATE))

    def test_commands_are_capped_inside_the_sandbox(self):
        wrapped = sandbox.capped("yes", 8192)
        self.assertIn("head -c 8192", wrapped)
        self.assertIn("> /tmp/.hagency-out", wrapped)
        self.assertEqual(sandbox.parse_capped(f"out\nmore\n{sandbox.EXIT_MARK} 3"), sandbox.Result(3, "out\nmore"))
        with self.assertRaises(sandbox.SandboxError):
            sandbox.parse_capped("no exit line")


class GitHubLimits(unittest.TestCase):
    """Findings 4 and 5: resets only to a template, only our own PRs closed, revocation checked."""
    def test_a_branch_update_is_only_a_forced_reset_to_a_commit(self):
        tg.check_write("PATCH", "/git/refs/heads/main", {"sha": "a" * 40, "force": True})
        for body in ({"sha": "a" * 40}, {"sha": "nope", "force": True}, {"sha": "a" * 40, "force": True, "x": 1}):
            with self.assertRaises(tg.TrialGitHubError):
                tg.check_write("PATCH", "/git/refs/heads/main", body)
        with self.assertRaises(tg.TrialGitHubError):
            tg.check_write("PATCH", "/git/refs/heads/trial/1-x", {"sha": "a" * 40, "force": True})

    def test_recycling_never_closes_a_forks_pr(self):
        fake = kit.FakeTrialGitHub({kit.REPO: kit.TEMPLATE})
        gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=fake.call, fetch=fake.fetch)
        fake.pulls[7] = {"repo": kit.REPO.lower(), "head": "trial/1-x", "head_repo": "someone/fork", "state": "open",
                         "base": "main", "title": "", "body": ""}
        token = run(gh.token(kit.REPO, write=True))
        run(gh.recycle(token, kit.REPO, fake.head[kit.REPO.lower()]))
        self.assertEqual(fake.pulls[7]["state"], "open")

    def test_a_revocation_github_doesnt_confirm_is_retried_and_warned(self):
        calls = []

        async def refuse(method, path, auth, body=None):
            calls.append(path)
            return 500, {}
        gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=refuse)
        with self.assertLogs("trial-build", "WARNING"):
            self.assertFalse(run(gh.revoke("ghs_x")))
        self.assertEqual(calls, ["/installation/token"] * 2)


class PartialPublish(BuildBase):
    """Finding 6: an open PR is never reported as "nothing was opened"."""
    def test_a_refused_label_still_reports_the_pr(self):
        d = kit.deps(self.folder)

        async def no_label(*a):
            raise tg.TrialGitHubError("label refused")
        d.gh.label = no_label
        job = self.queue_and_run(d)
        self.assertEqual(job["state"], trial_jobs.DONE)
        self.assertEqual(job["outcome"]["review"], trial_build.NO_REVIEW)
        self.assertEqual(d.pool.entry(kit.REPO)["prs"], [job["outcome"]["pr"]])


class Shutdown(BuildBase):
    """Findings 7 and 8: nothing queues after the switch-off; one worker at a time."""
    def test_switch_off_and_enqueue_share_the_lock(self):
        jobs = trial_jobs.Jobs(self.folder)
        jobs.beat(kit.CAPS)
        jobs.enqueue("@a:s", 1, "!a", 5)
        self.assertEqual(len(jobs.shut("off")), 1)
        self.assertEqual(jobs.enqueue("@b:s", 2, "!b", 5)[0], trial_jobs.OFF)

    def test_pm_says_off_when_the_worker_went_during_the_gate(self):
        lp = mock.Mock(spec=pm_build.BuildOffer)
        lp.build_jobs.return_value = trial_jobs.Jobs(self.folder)
        said = pm_build.BuildOffer.queue_build(lp, "!dm", "@a:s", {"id": 1}, kit.CAPS)
        self.assertEqual(said, pm_build.OFF)
        lp.build_usage.return_value.take.assert_not_called()

    def test_a_second_worker_exits_and_leaves_the_first_ones_build(self):
        d = kit.deps(self.folder)
        d.jobs.enqueue("@a:s", 1, "!a", 5)
        d.jobs.claim()
        with d.jobs.one_worker() as first:
            self.assertTrue(first)
            self.assertFalse(run(trial_worker.serve(d, asyncio.Event())))
        self.assertEqual([j["state"] for j in d.jobs.all().values()], [trial_jobs.RUNNING])

    def test_the_docs_recreate_the_worker_for_the_kill_switch(self):
        self.assertIn("--force-recreate", (DOCS / "TRIAL_BUILDER_SETUP.md").read_text())


class Probes(BuildBase):
    """Findings 9 and 10: talk wins a race; an inconclusive probe or environment check refuses."""
    def test_an_inconclusive_probe_or_unreadable_environment_refuses(self):
        for boxes in (sandbox_fake.FakeSandboxes(kit.on_run, probe_exit=127), sandbox_fake.FakeSandboxes(kit.on_run, probe_exit=0),
                      sandbox_fake.FakeSandboxes(kit.on_run, environ="")):
            job = self.queue_and_run(kit.deps(self.folder, sandboxes=boxes), visitor=f"@v{len(boxes.boxes)}{id(boxes)}:s")
            self.assertEqual(job["outcome"]["why"], "unsafe")

    def test_a_talk_answer_arriving_as_a_build_locks_still_goes_first(self):
        build = model_slot.Slot(model_slot.lock_path(self.folder), model_slot.BUILD, poll_s=0.01)
        answers = iter([False, True, False, False])
        with mock.patch.object(build, "talk_waiting", lambda: next(answers)):
            run(asyncio.wait_for(build.acquire(), 1))
        self.assertTrue(build.locked())
        build.release()


class Inert(BuildBase):
    """Finding 11: model words on GitHub and in Matrix carry no links, mentions or markup."""
    def test_pr_body_review_and_news(self):
        ask = kit.scripted(*kit.FIX[:-1], kit.tool("finish", summary="Fixed! See https://evil.example ![x](y) @everyone <b>"))
        d = kit.deps(self.folder, ask_build=ask)

        async def review(chat):
            return "- see [here](https://evil.example)\n- ping @someone"
        d.ask_review = review
        job = self.queue_and_run(d)
        pr = d.fake.pulls[job["outcome"]["pr"]]
        body = pr["body"] + d.fake.reviews[0][1]["body"]
        for bad in ("https://", "![x](y)", "@everyone", "@someone", "<b>", "](https"):
            self.assertNotIn(bad, body)
        words = pm_build.news_words({"repo": kit.REPO, "issue": 1, "title": "x https://evil.example @room"}, "started", 1)
        self.assertNotIn("https://", words)
        self.assertNotIn("@room", words)


if __name__ == "__main__":
    unittest.main()
