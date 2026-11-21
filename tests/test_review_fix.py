"""Tests: the review → fix → review loop. Run: .venv/bin/python -m unittest tests.test_review_fix

Rounds (unit): "changes requested" sends the builder a fix task with the reasons; its fix goes back
to the same reviewer; after MAX_FIX_ROUNDS a person takes over.
Fix step (integration, real git through tests/fake_gh.py): the builder's revision is pushed to its
own PR branch as a new commit; a "fix" that changes nothing is refused.
"""
import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.agents import agents
from common import github
from server import loop_progress
from adapters.agents import tasks
from tests import test_loop_e2e
from server.loop import Loop

B, R = "@b-agent:x", "@r-agent:x"


class Rounds(unittest.TestCase):
    def setUp(self):
        owners = mock.patch("server.ownership.owner_id", lambda a: a.replace("-agent", ""))   # each agent: its own person
        owners.start()
        self.addCleanup(owners.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos = {"!r": "your-name/hagency-hello"}
        self.lp.state, self.lp.save = {}, lambda: None
        self.lp.say = mock.AsyncMock()
        self.lp.level = mock.AsyncMock(return_value="autonomous")
        self.item = self.lp.state["7"] = {"room": "!r", "repo": "your-name/hagency-hello", "stage": "reviewing", "builder": B, "reviewer": R, "pr": 9,
                                          "task_id": "T", "task_ids": ["T"]}

    def result(self, sender, **r):
        r.setdefault("task_id", self.item["task_id"])
        asyncio.run(self.lp.on_result(SimpleNamespace(room_id="!r"), sender, {"issue": 7, "pr": 9, **r}))

    def last_task(self):
        return [c for c in self.lp.say.call_args_list if len(c.args) > 3][-1].args[3]

    def last_to(self):
        return [c for c in self.lp.say.call_args_list if len(c.args) > 3][-1].args[2]

    def test_changes_requested_go_to_the_builder_with_the_reasons(self):
        self.result(R, kind="review", verdict="request_changes", sha="s1", reasons="README lacks the live link")
        self.assertEqual((self.item["stage"], self.item["fix_rounds"]), ("fixing", 1))
        self.assertEqual(self.last_to(), B)
        self.assertEqual((self.last_task()["kind"], self.last_task()["feedback"]), ("fix", "README lacks the live link"))

    def test_the_fix_goes_back_to_the_same_reviewer(self):
        self.result(R, kind="review", verdict="request_changes", sha="s1", reasons="x")
        self.result(B, kind="fix")
        self.assertEqual(self.item["stage"], "reviewing")
        self.assertEqual((self.last_to(), self.last_task()["kind"]), (R, "review"))
        self.result(R, kind="review", verdict="approve", sha="s2")
        self.assertEqual((self.item["stage"], self.item["sha"]), ("approved", "s2"))     # pinned to the new commit

    def test_after_the_last_round_a_person_takes_over(self):
        for _ in range(loop_progress.MAX_FIX_ROUNDS):
            self.result(R, kind="review", verdict="request_changes", sha="s", reasons="x")
            self.result(B, kind="fix")
        self.result(R, kind="review", verdict="request_changes", sha="s", reasons="x")
        self.assertEqual(self.item["stage"], "stopped")
        self.assertIn("@pm retry #7", self.lp.say.call_args.args[1])

    def test_only_the_builder_reports_a_fix(self):
        self.result(R, kind="review", verdict="request_changes", sha="s1", reasons="x")
        self.result(R, kind="fix")                            # the reviewer can't fix-and-approve itself
        self.assertEqual(self.item["stage"], "fixing")


class FixStep(unittest.TestCase):
    """The real git path: build a PR, then revise it, through the fake GitHub."""
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        test_loop_e2e.ISSUES = {"5": {"title": "docs: add NOTE", "state": "OPEN", "labels": ["agent-ok"],
                                      "body": "Acceptance criteria:\n- docs/NOTE.md says hello"}}
        env = test_loop_e2e.make_fake_github(tmp)
        self.remote = Path(json.loads(Path(env["FAKE_GH_STATE"]).read_text())["remote"])
        self.base = tmp / "work"
        for patch in (mock.patch.dict(os.environ, {"FAKE_GH_STATE": env["FAKE_GH_STATE"]}),
                      mock.patch.object(github, "GH_BIN", env["GH_BIN"]),
                      mock.patch.object(github, "IDENTITY_ROOT", Path(env["GH_IDENTITY_ROOT"])),
                      mock.patch.object(github, "REPO", "test/sandbox")):
            patch.start()
            self.addCleanup(patch.stop)
        self.task = {"issue": 5, "repo": "test/sandbox"}

    def agent_writes(self, text):
        async def run_turn(backend, workdir, prompt, write=False, fresh=False):
            if text is not None:
                (workdir / "docs").mkdir(exist_ok=True)
                (workdir / "docs" / "NOTE.md").write_text(text)
            return "done"
        return mock.patch.object(agents, "run_turn", run_turn)

    def head(self, branch):
        return subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", branch],
                              capture_output=True, text=True, check=True).stdout.strip()

    def show(self, branch):
        return subprocess.run(["git", "--git-dir", str(self.remote), "show", f"{branch}:docs/NOTE.md"],
                              capture_output=True, text=True, check=True).stdout

    def test_the_revision_is_a_new_commit_on_the_same_pr_branch(self):
        branch = "agent/anna-agent/issue-5"
        with self.agent_writes("hi\n"):
            built = asyncio.run(tasks.build("anna-agent", "codex", self.base, self.task))
        first = self.head(branch)
        with self.agent_writes("hello\n"):
            fixed = asyncio.run(tasks.fix("anna-agent", "codex", self.base,
                                          {**self.task, "pr": built["pr"], "feedback": "it must say hello"}))
        self.assertEqual(fixed["line"], f"updated PR #{built['pr']} after the review")
        self.assertNotEqual(self.head(branch), first)
        self.assertEqual(self.show(branch), "hello\n")

    def test_a_fix_that_changes_nothing_is_refused(self):
        with self.agent_writes("hi\n"):
            built = asyncio.run(tasks.build("anna-agent", "codex", self.base, self.task))
        with self.agent_writes(None):
            result = asyncio.run(tasks.handle("anna-agent", "codex", self.base,
                                              {**self.task, "kind": "fix", "pr": built["pr"], "feedback": "x"}))
        self.assertIn("no changes were made", result["error"])


if __name__ == "__main__":
    unittest.main()
