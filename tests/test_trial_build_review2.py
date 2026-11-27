"""Regression tests for the second GPT review of trial builds (ADR 0027), one per verified finding.
Run: .venv/bin/python -m unittest tests.test_trial_build_review2"""
import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import model_slot
from common import sandbox
from server import pm_build
from server import trial_build
from server import trial_github as tg
from server import trial_jobs
from server import trial_pool
from server import trial_review
from server import trial_tools
from server import trial_worker
from tests import trial_build_kit as kit
from tests.test_trial_build import BuildBase


def run(coro):
    return asyncio.run(coro)


class FakeSbx:
    """The e2b AsyncSandbox surface E2BBox uses: streamed output and a streamed file."""
    def __init__(self, chunks, file_chunks=()):
        self.sandbox_id, self.chunks, self.file_chunks, self.kw = "s", chunks, list(file_chunks), None
        self.commands = SimpleNamespace(run=self.run_cmd)
        self.files = SimpleNamespace(read=self.read)

    async def run_cmd(self, cmd, **kw):
        self.kw = kw
        for c in self.chunks:
            kw["on_stdout"](c)
        return SimpleNamespace(exit_code=0)

    async def read(self, path, format):
        chunks = self.file_chunks

        class Stream:
            closed = False

            def __aiter__(self):
                async def gen():
                    for c in chunks:
                        yield c
                return gen()

            async def aclose(self):
                Stream.closed = True
        self.stream = Stream
        return Stream()


class Output(unittest.TestCase):
    def setUp(self):
        e2b = types.ModuleType("e2b")               # the pinned SDK's exceptions (e2b==2.66.0), for E2BBox.run
        e2b.CommandExitException = type("CommandExitException", (Exception,), {})
        e2b.TimeoutException = type("TimeoutException", (Exception,), {})
        p = mock.patch.dict(sys.modules, {"e2b": e2b})
        p.start()
        self.addCleanup(p.stop)

    def test_the_command_is_data_and_cant_leave_the_wrapper(self):
        wrapped = sandbox.capped("x ) ; yes ; ( y", 100)
        self.assertNotIn("yes", wrapped)                       # base64: no shell syntax of its own survives
        self.assertIn("bash /tmp/.hagency-cmd > /tmp/.hagency-out", wrapped)

    def test_the_sdk_never_holds_more_than_we_allow(self):
        sbx = FakeSbx(["x" * 1000] * 5)
        box = sandbox.E2BBox(sbx)
        with self.assertRaises(sandbox.SandboxError):
            run(box.run("ls", limit=2000))
        self.assertIs(sbx.kw["retain_output"], False)
        self.assertEqual(run(sandbox.E2BBox(FakeSbx(["ab", "c"])).run("ls")).stdout, "abc")

    def test_a_file_that_grew_is_cut_off_while_streaming(self):
        sbx = FakeSbx([], [b"x" * 600, b"x" * 600])
        with self.assertRaises(sandbox.SandboxError):
            run(sandbox.E2BBox(sbx).read("src/a.py", 1000))
        self.assertTrue(sbx.stream.closed)
        self.assertEqual(run(sandbox.E2BBox(FakeSbx([], [b"ab"])).read("src/a.py", 10)), b"ab")


class Verdict(unittest.TestCase):
    def test_a_report_must_name_failures_and_errors(self):
        self.assertIsNone(trial_build.report_counts('<testsuite tests="3">'))
        self.assertEqual(trial_build.report_counts(kit.junit(passed=2)), (2, 0))


class Recycle(unittest.TestCase):
    def setUp(self):
        self.fake = kit.FakeTrialGitHub({kit.REPO: kit.TEMPLATE})
        self.gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=self.fake.call, fetch=self.fake.fetch)
        self.token = run(self.gh.token(kit.REPO, write=True))

    def test_no_reset_when_the_branch_is_the_template_even_with_prs_open(self):
        self.fake.pulls[5] = {"repo": kit.REPO.lower(), "head": "trial/1-a", "state": "open", "base": "main", "title": "", "body": ""}
        run(self.gh.recycle(self.token, kit.REPO, self.fake.head[kit.REPO.lower()]))
        self.assertFalse([r for r in self.fake.requests if r[0] == "PATCH" and "/git/refs/" in r[1]])
        self.assertEqual(self.fake.pulls[5]["state"], "closed")

    def test_a_refused_reset_touches_nothing_else(self):
        template = self.fake.head[kit.REPO.lower()]
        self.fake.head[kit.REPO.lower()] = "e" * 40
        self.fake.pulls[5] = {"repo": kit.REPO.lower(), "head": "trial/1-a", "state": "open", "base": "main", "title": "", "body": ""}
        real = self.fake._write

        def refuse(repo, method, sub, body):
            return (422, {}) if method == "PATCH" and "/git/refs/" in sub else real(repo, method, sub, body)
        self.fake._write = refuse
        with self.assertRaises(tg.TrialGitHubError):
            run(self.gh.recycle(self.token, kit.REPO, template))
        self.assertEqual(self.fake.pulls[5]["state"], "open")

    def test_a_branch_reset_outside_recycling_is_refused(self):
        with self.assertRaises(tg.TrialGitHubError):
            run(self.gh.write(self.token, kit.REPO, "PATCH", "/git/refs/heads/main", {"sha": "a" * 40, "force": True}))


class Leases(unittest.TestCase):
    def test_a_lease_that_opened_nothing_is_free_after_an_hour(self):
        now = [1000.0]
        fake = kit.FakeTrialGitHub({kit.REPO: kit.TEMPLATE})
        gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=fake.call, fetch=fake.fetch)
        pool = trial_pool.Pool(Path(tempfile.mkdtemp()) / "p.json", [kit.REPO], 7, clock=lambda: now[0])
        run(pool.lease(gh, 1))
        with self.assertRaises(trial_pool.NoRepoFree):
            run(pool.lease(gh, 2))
        now[0] += trial_pool.IDLE_S
        self.assertEqual(run(pool.lease(gh, 2)), kit.REPO)


class Deadline(BuildBase):
    def test_no_tool_and_no_finish_past_the_deadline_within_one_answer(self):
        two = {"content": "", "tool_calls": [{"function": {"name": "run", "arguments": {"command": "sleep"}}},
                                             {"function": {"name": "finish", "arguments": {"summary": "x"}}}]}
        clock = iter([0, 0, 0, 0, 10_000, 10_000, 10_000])
        box = SimpleNamespace(run_capped=lambda *a, **k: asyncio.sleep(0, sandbox.Result(0, "")))
        loop = trial_tools.ToolLoop(box, model_slot.Slot(model_slot.lock_path(self.folder), model_slot.BUILD),
                                    kit.deps(self.folder).budget, kit.scripted(two), clock=lambda: next(clock), seconds=100)
        self.assertEqual(run(loop.run([{"role": "user", "content": "go"}])).reason, trial_tools.TIME)


class Words(unittest.TestCase):
    def test_done_says_when_the_self_check_couldnt_be_posted(self):
        job = {"outcome": {"pr": 3, "repo": kit.REPO, "issue": 1, "files": ["src/a.py"], "review": trial_build.NO_REVIEW}}
        said = pm_build.news_words(job, trial_jobs.DONE, 2)
        self.assertNotIn(pm_build.POSTED, said)
        self.assertIn(pm_build.NOT_POSTED, said)
        self.assertIn("Ready: a person merges", said)

    def test_review_notes_have_no_headings_or_quotes(self):
        out = trial_review.notes("# Big\n> quoted\n- fine")
        self.assertEqual(out.splitlines()[0], "Big")
        self.assertEqual(out.splitlines()[1], "quoted")


class KillSwitch(BuildBase):
    def test_it_waits_for_the_running_worker(self):
        jobs = trial_jobs.Jobs(self.folder)
        jobs.beat(kit.CAPS)
        jobs.enqueue("@a:s", 1, "!a", 5)
        with jobs.one_worker():
            self.assertFalse(run(trial_worker.shut_down(jobs, None)))
        self.assertEqual([j["state"] for j in jobs.all().values()], [trial_jobs.QUEUED])
        self.assertTrue(run(trial_worker.shut_down(jobs, None)))


class ThirdPass(BuildBase):
    """The third GPT pass: an opened PR survives a restart in the news, Bounded stays bounded, a refused
    kill switch exits non-zero."""
    def test_a_restart_after_the_pr_was_opened_says_it_is_open(self):
        jobs = trial_jobs.Jobs(self.folder)
        jobs.beat(kit.CAPS)
        _, job_id, _ = jobs.enqueue("@a:s", 1, "!a", 5)
        jobs.claim()
        jobs.update(job_id, pr=100, repo=kit.REPO)
        jobs.fail_unfinished("restart")
        job = jobs.all()[job_id]
        said = pm_build.news_words(job, trial_jobs.FAILED, 2)
        self.assertIn("opened pull request #100", said)
        self.assertNotIn("Nothing was opened", said)

    def test_a_full_buffer_keeps_nothing_more(self):
        b = sandbox.Bounded(3)
        for _ in range(1000):
            b.add("xy")
        self.assertEqual((b.text(), len(b.parts), b.over), ("xyx", 2, True))

    def test_a_refused_kill_switch_exits_non_zero(self):
        jobs = trial_jobs.Jobs(self.folder)
        with kit.configured_env(self.folder, TRIAL_BUILD="off", E2B_API_KEY=""), jobs.one_worker():
            self.assertEqual(run(trial_worker.main()), 1)


if __name__ == "__main__":
    unittest.main()
