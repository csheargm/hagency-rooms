"""The trial-worker's side of one build (ADR 0027), with a fake sandbox, a fake GitHub and a scripted
model: what reaches GitHub, which sandboxes are made and killed, and every way a build fails.
Run: .venv/bin/python -m unittest tests.test_trial_build"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from common import sandbox
from server import trial_build
from server import trial_github
from server import trial_jobs
from server import trial_patch
from server import trial_worker
from tests import trial_build_kit as kit


class BuildBase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        env = kit.configured_env(self.folder)
        env.start()
        self.addCleanup(env.stop)

    def queue_and_run(self, d, visitor="@anna:bridge.localhost"):
        state, job_id, _ = d.jobs.enqueue(visitor, 777, "!dm", 5)
        self.assertEqual(state, trial_jobs.QUEUED)
        asyncio.run(trial_worker.tick(d))
        return d.jobs.all()[job_id]


class HappyPath(BuildBase):
    def test_a_build_opens_a_labelled_pr_with_a_comment_review_and_never_merges(self):
        d = kit.deps(self.folder)
        job = self.queue_and_run(d)
        self.assertEqual(job["state"], trial_jobs.DONE, job.get("outcome"))
        out, gh = job["outcome"], d.fake
        self.assertEqual((out["repo"], out["files"], out["tests"]), (kit.REPO, ["src/calc.py"], "passed"))
        pr = gh.pulls[out["pr"]]
        self.assertTrue(pr["head"].startswith("trial/1-"))
        self.assertIn("Written by AI in a sandbox", pr["body"])
        self.assertNotIn("@anna", pr["body"])                     # an opaque job id, never the Matrix ID
        self.assertEqual(gh.labels, [(out["pr"], [trial_github.LABEL])])
        (number, review), = gh.reviews
        self.assertEqual((number, review["event"]), (out["pr"], "COMMENT"))
        self.assertIn("AI self-check", review["body"])
        self.assertFalse([r for r in gh.requests if "/merge" in r[1]])   # a person merges
        tree = next(iter(gh.trees.values()))["tree"]
        self.assertEqual(tree, [{"path": "src/calc.py", "mode": "100644", "type": "blob", "content": kit.FIXED}])

    def test_tokens_are_scoped_per_step_and_all_revoked(self):
        d = kit.deps(self.folder)
        self.queue_and_run(d)
        perms = [t["permissions"]["contents"] for t in d.fake.tokens.values()]
        self.assertIn("read", perms)
        self.assertIn("write", perms)
        self.assertTrue(all(t["repo"] == kit.REPO for t in d.fake.tokens.values()))   # one repo each
        self.assertTrue(all(t["revoked"] for t in d.fake.tokens.values()))
        for t in d.fake.tokens.values():
            self.assertFalse({"workflows", "administration", "actions", "secrets"} & set(t["permissions"]))

    def test_two_fresh_sandboxes_build_then_test_without_network_and_both_are_killed(self):
        boxes = sandbox.FakeSandboxes(kit.on_run)
        self.queue_and_run(kit.deps(self.folder, sandboxes=boxes))
        self.assertEqual([b.kind for b in boxes.boxes], [sandbox.BUILD, sandbox.TEST])
        self.assertTrue(all(b.killed for b in boxes.boxes))
        test_box = boxes.boxes[1]
        self.assertEqual(test_box.files["src/calc.py"], kit.FIXED.encode())   # the pristine copy plus the patch
        self.assertIn("python -m pytest -q", test_box.ran)                    # the template's fixed command
        self.assertTrue(all(sandbox.PROBE_CMD in b.ran for b in boxes.boxes))   # egress probed first, both times

    def test_the_model_never_sees_a_credential_and_tool_output_is_capped(self):
        ask = kit.scripted(kit.tool("run", command="yes | head -c 50000"), *kit.FIX)
        boxes = sandbox.FakeSandboxes(lambda box, cmd: sandbox.Result(0, "y" * 50000) if "yes" in cmd else kit.on_run(box, cmd))
        self.queue_and_run(kit.deps(self.folder, sandboxes=boxes, ask_build=ask))
        tool_out = [m for m in ask.asked[1] if m["role"] == "tool"][0]["content"]
        self.assertLess(len(tool_out), 9000)


class Refusals(BuildBase):
    def refused(self, *steps, boxes=None):
        d = kit.deps(self.folder, sandboxes=boxes, ask_build=kit.scripted(*steps))
        job = self.queue_and_run(d)
        self.assertEqual(job["state"], trial_jobs.FAILED)
        self.assertFalse(d.fake.pulls)                         # nothing is opened
        return job["outcome"]

    def test_editing_a_test_is_refused(self):
        out = self.refused(kit.tool("write_file", path="tests/test_calc.py", content="def test_add():\n    pass\n"),
                           kit.tool("write_file", path="src/calc.py", content=kit.FIXED))
        self.assertEqual((out["why"], out["rule"]), ("patch", trial_patch.TESTS))

    def test_a_workflow_is_refused(self):
        out = self.refused(kit.tool("write_file", path=".github/workflows/x.yml", content="on: push\n"))
        self.assertEqual(out["rule"], trial_patch.PROTECTED)

    def test_a_symlink_is_refused(self):
        def link(box, cmd):
            if "ln -s" in cmd:
                box.links.append("src/evil")
            return kit.on_run(box, cmd)
        out = self.refused(kit.tool("run", command="ln -s /etc/passwd src/evil"),
                           kit.tool("write_file", path="src/calc.py", content=kit.FIXED), boxes=sandbox.FakeSandboxes(link))
        self.assertEqual(out["rule"], trial_patch.LINK)

    def test_an_empty_diff_is_a_failure_not_a_pass(self):
        out = self.refused(kit.tool("read_file", path="src/calc.py"))
        self.assertEqual(out["rule"], trial_patch.EMPTY)

    def test_failing_tests_fail_the_build(self):
        out = self.refused(kit.tool("write_file", path="src/calc.py", content="def add(a, b):\n    return 0\n"))
        self.assertEqual(out["why"], "tests_failed")

    def test_a_forged_passing_log_from_the_build_sandbox_is_ignored(self):
        out = self.refused(kit.tool("write_file", path="src/calc.py", content="print('1 passed')\n"),
                           kit.tool("run", command="echo '1 passed'"))
        self.assertEqual(out["why"], "tests_failed")           # only the separate test sandbox's exit code counts

    def test_an_open_egress_stops_the_build_before_anything_is_uploaded(self):
        boxes = sandbox.FakeSandboxes(kit.on_run, egress_open=True)
        out = self.refused(*kit.FIX, boxes=boxes)
        self.assertEqual(out["why"], "unsafe")
        self.assertEqual(boxes.boxes[0].files, {})
        self.assertTrue(boxes.boxes[0].killed)

    def test_a_sandbox_holding_a_credential_is_refused(self):
        boxes = sandbox.FakeSandboxes(kit.on_run, environ="PATH=/bin\nGITHUB_TOKEN=ghs_x\n")
        self.assertEqual(self.refused(*kit.FIX, boxes=boxes)["why"], "unsafe")

    def test_running_out_of_steps_is_a_failure(self):
        with kit.configured_env(self.folder, TRIAL_BUILD_STEPS="2"):
            out = self.refused(*[kit.tool("read_file", path="README.md")] * 5)
        self.assertEqual(out["why"], "steps")

    def test_running_out_of_time_is_a_failure(self):
        with kit.configured_env(self.folder, TRIAL_BUILD_S="0"):
            out = self.refused(*kit.FIX)
        self.assertEqual(out["why"], "time")

    def test_no_seeded_issue_is_a_failure(self):
        d = kit.deps(self.folder)
        d.fake.issues[kit.REPO] = []
        self.assertEqual(self.queue_and_run(d)["outcome"]["why"], "no_issue")


class Worker(BuildBase):
    def test_one_build_at_a_time_and_a_restart_fails_what_was_underway(self):
        d = kit.deps(self.folder)
        d.jobs.enqueue("@a:bridge.localhost", 1, "!a", 5)
        d.jobs.enqueue("@b:bridge.localhost", 2, "!b", 5)
        self.assertIsNotNone(d.jobs.claim())
        self.assertIsNone(d.jobs.claim())                      # one runs: nobody else starts
        stop = asyncio.Event()
        stop.set()
        asyncio.run(trial_worker.serve(d, stop))
        states = sorted(j["state"] for j in d.jobs.all().values())
        self.assertEqual(states, [trial_jobs.FAILED, trial_jobs.QUEUED])
        self.assertEqual(next(j for j in d.jobs.all().values() if j["state"] == "failed")["outcome"]["why"], "restart")
        self.assertIsNone(d.jobs.alive())                       # it stopped beating when it stopped

    def test_the_days_sandbox_time_cap_holds_new_builds(self):
        d = kit.deps(self.folder)
        with kit.configured_env(self.folder, TRIAL_SANDBOX_S_PER_DAY="0"):
            d.jobs.enqueue("@a:bridge.localhost", 1, "!a", 5)
            self.assertIsNone(asyncio.run(trial_worker.tick(d)))
        self.assertEqual([j["state"] for j in d.jobs.all().values()], [trial_jobs.QUEUED])

    def test_a_crash_inside_a_build_fails_it_plainly(self):
        d = kit.deps(self.folder)

        async def boom(*a):
            raise KeyError("bug")
        d.pool.lease = boom
        self.assertEqual(self.queue_and_run(d)["outcome"], {"why": "error"})
        self.assertIn("error", trial_build.WHY)


if __name__ == "__main__":
    unittest.main()
