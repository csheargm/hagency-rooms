"""Unit tests: the hagency MCP server's trust rules. Run: .venv/bin/python -m unittest tests.test_hagency_mcp"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("HAGENCY_AGENT", "anna-hermes-agent")
os.environ.setdefault("HAGENCY_ROOM", "!r:bridge.localhost")
from common import github  # noqa: E402
from mcp_tools import hagency_mcp as m  # noqa: E402
from mcp_tools import mcp_core as core  # noqa: E402
from adapters.agents import room_context  # noqa: E402

ME, BOT = core.USER_ID, core.PM_ID
_patches = [mock.patch.object(core, "owner_in", lambda room: True),   # owner present unless a test says not
            mock.patch.object(core, "token", lambda: "t"),                 # never a real Matrix login (GPT review 10)
            mock.patch.object(room_context, "room_repo", lambda token, room: github.REPO)]   # the room's repo


def setUpModule():
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()


def task_event(task_id, kind="build", sender=BOT, agent=ME, **kw):
    return {"sender": sender, "room_id": "!r", "content": {github.TASK_KEY: {"task_id": task_id, "kind": kind, "agent": agent,
                                                            "issue": 7, "repo": github.REPO, **kw}}}


def result_event(task_id):
    return {"sender": ME, "room_id": "!r", "content": {github.RESULT_KEY: {"task_id": task_id}}}


class Trust(unittest.TestCase):
    def setUp(self):
        core._open.clear()
        self.posted = []
        for name, fn in (("post", lambda line, result=None, room=None: self.posted.append((line, result))),):
            p = mock.patch.object(core, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def events(self, evs):
        p = mock.patch.object(core, "recent_events", lambda: evs)
        p.start()
        self.addCleanup(p.stop)

    def test_newest_open_task_is_returned(self):
        self.events([task_event("T2", "merge", pr=9), task_event("T1")])
        self.assertEqual((m.next_task()["task_id"], m.next_task()["next"]), ("T2", "Call merge_pr(task_id)."))

    def test_answered_task_is_not_returned_again(self):
        self.events([result_event("T1"), task_event("T1")])
        self.assertIsNone(m.next_task()["task"])

    def test_tasks_from_anyone_but_loop_bot_are_ignored(self):
        self.events([task_event("T1", sender="@mallory:bridge.localhost")])
        self.assertIsNone(m.next_task()["task"])
        with self.assertRaises(ValueError):
            core.task("T1", "build")

    def test_tasks_for_another_agent_are_ignored(self):
        self.events([task_event("T1", agent="@betty-agent:bridge.localhost")])
        self.assertIsNone(m.next_task()["task"])

    def test_wrong_task_id_for_the_tool_does_not_stop_the_loop(self):
        """Seen live: Hermes reused the build task id when asked to merge."""
        self.events([task_event("BUILD")])
        out = asyncio.run(m.merge_pr("BUILD"))
        self.assertIn("next_task", out["next"])
        self.assertEqual(self.posted, [])          # nothing reported, the item keeps going

    def test_merge_refused_unless_autonomous(self):
        self.events([task_event("M", "merge", pr=9, sha="abc")])
        with mock.patch.object(core, "api", lambda *a, **k: {"level": "assist", "repo": github.REPO}), \
                mock.patch.object(core, "token", lambda: "t"):
            out = asyncio.run(m.merge_pr("M"))
        self.assertIn("failed", out)
        line, result = self.posted[-1]
        self.assertIn("not autonomous", result["error"])

    def test_steps_out_of_order_get_a_hint_not_a_stop(self):
        self.events([task_event("B")])
        self.assertIn("start_build", asyncio.run(m.finish_build("B", "x"))["next"])
        core._open.clear()
        self.events([task_event("R", "review", pr=9)])
        self.assertIn("get_review", asyncio.run(m.submit_review("R", True, "ok"))["next"])
        self.assertEqual(self.posted, [])

    def test_a_task_for_another_repo_is_refused(self):
        """Seen live: the MCP server ran on the default repo and opened issues there."""
        self.events([task_event("M", "merge", pr=9, sha="a", repo="someone/else")])
        out = asyncio.run(m.merge_pr("M"))
        self.assertIn("the room works on", out["error"])
        self.assertEqual(self.posted, [])

    def test_nothing_happens_once_the_owner_has_left(self):
        """GPT review 7: work could continue after the owner left the room."""
        self.events([task_event("M", "merge", pr=9, sha="a")])
        with mock.patch.object(core, "owner_in", lambda room: False):
            out = asyncio.run(m.merge_pr("M"))
        self.assertIn("owner isn't in that room", out["error"])
        self.assertEqual(self.posted, [])

    def test_review_steps_keep_their_state(self):
        """Seen live: every tool re-checks the current task, and that used to drop the PR head
        get_review had stored, so submit_review kept answering "call get_review first"."""
        self.events([task_event("R", "review", pr=9)])

        async def material(agent, t):
            return 7, 9, {"title": "t"}, "abc", "diff"

        async def submit(agent, n, pr, head, verdict, text, repo):
            self.assertEqual(repo, github.REPO)       # GPT review 10: the call lost its repo argument
            return {"kind": "review", "issue": n, "pr": pr, "sha": head, "verdict": verdict.lower(), "line": "approved PR #9"}
        with mock.patch.object(core.tasks, "review_material", material), mock.patch.object(core.tasks, "submit_review", submit):
            asyncio.run(m.get_review("R"))
            out = asyncio.run(m.submit_review("R", True, "ok"))
        self.assertEqual(out["done"], "approved PR #9")
        self.assertEqual(self.posted[-1][1]["sha"], "abc")

    def test_approved_issues_are_filed_in_the_rooms_repo(self):
        """GPT review 10: open_issues called planning.file_issues without the repo (TypeError)."""
        self.events([task_event("F", "file_issues", items=[{"title": "x"}], approved_by="anna")])
        filed = mock.AsyncMock(return_value=[11])
        with mock.patch.object(m.planning if hasattr(m, "planning") else __import__("adapters.agents.planning", fromlist=["planning"]), "file_issues", filed):
            out = asyncio.run(m.open_issues("F"))
        self.assertEqual(out["done"], "opened #11")
        self.assertEqual(filed.await_args.args[-1], github.REPO)

    def test_an_older_or_answered_task_is_never_acted_on(self):
        """GPT review: tools used to accept any task id still visible in the room."""
        self.events([task_event("NEW", "review", pr=9), task_event("OLD", "merge", pr=8, sha="a")])
        self.assertIn("isn't your current task", asyncio.run(m.merge_pr("OLD"))["error"])
        self.events([result_event("M"), task_event("M", "merge", pr=9, sha="a")])
        self.assertIn("error", asyncio.run(m.merge_pr("M")))
        self.assertEqual(self.posted, [])


class RoomSafe(unittest.TestCase):
    def test_local_paths_never_reach_the_room(self):
        from pathlib import Path
        from common.redact import room_safe
        text = room_safe(f"git failed: {Path.home()}/work/x and /private/tmp/abc/y: denied")
        self.assertNotIn(str(Path.home()), text)
        self.assertNotIn("/private/tmp", text)
        self.assertIn("denied", text)


class CheckoutOnDisk(unittest.TestCase):
    """Seen live: Hermes called start_build three times and the in-memory checkout was lost."""
    def setUp(self):
        import tempfile
        from pathlib import Path
        core._open.clear()
        p = mock.patch.object(core, "BASE", Path(tempfile.mkdtemp()))
        p.start()
        self.addCleanup(p.stop)
        self.clones = 0

        async def prepare(agent, base, t):
            self.clones += 1
            root = base / f"issue-{t['issue']}"
            (root / "git").mkdir(parents=True, exist_ok=True)
            return core.tasks.Checkout(int(t["issue"]), {"title": "x"}, root, "b", t["repo"])
        for name, fn in (("recent_events", lambda: [task_event("B")]), ("post", lambda *a, **k: None)):
            q = mock.patch.object(core, name, fn)
            q.start()
            self.addCleanup(q.stop)
        q = mock.patch.object(core.tasks, "prepare_build", prepare)
        q.start()
        self.addCleanup(q.stop)

    def test_repeat_start_reuses_the_checkout(self):
        asyncio.run(m.start_build("B"))
        asyncio.run(m.start_build("B"))
        self.assertEqual(self.clones, 1)

    def test_finish_finds_the_checkout_after_a_server_restart(self):
        asyncio.run(m.start_build("B"))
        core._open.clear()                                   # as if the MCP server restarted
        finished = []

        async def fin(agent, who, co, summary):
            finished.append(co.root)
            return {"kind": "build", "issue": 7, "pr": 9, "line": "PR #9 opened for #7"}
        with mock.patch.object(core.tasks, "finish_build", fin):
            out = asyncio.run(m.finish_build("B", "done"))
        self.assertEqual(out["done"], "PR #9 opened for #7")
        self.assertEqual(len(finished), 1)


class FixOverMcp(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        core._open.clear()
        self.posted = []
        base = Path(tempfile.mkdtemp())

        async def prepare_fix(agent, base_, t):
            root = base / f"issue-{t['issue']}"
            (root / "git").mkdir(parents=True, exist_ok=True)
            (root / "tree").mkdir(parents=True, exist_ok=True)
            return core.tasks.Checkout(int(t["issue"]), {"title": "x"}, root, "b", t["repo"])

        async def finish_fix(agent, co, summary, pr):
            return {"kind": "fix", "issue": 7, "pr": pr, "line": f"updated PR #{pr} after the review"}
        for target, name, fn in ((core, "BASE", base), (core, "recent_events", lambda: [task_event("F", "fix", pr=9, feedback="add hello")]),
                                 (core, "post", lambda line, result=None, room=None: self.posted.append((line, result))),
                                 (core.tasks, "prepare_fix", prepare_fix), (core.tasks, "finish_fix", finish_fix)):
            p = mock.patch.object(target, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def test_fix_flow_over_mcp(self):
        self.assertEqual(m.next_task()["next"], "Call start_fix(task_id).")
        out = asyncio.run(m.start_fix("F"))
        self.assertIn("add hello", out["instructions"])
        self.assertEqual(m.write_files("F", {"docs/HELLO.md": "hello"})["written"], ["docs/HELLO.md"])
        done = asyncio.run(m.finish_fix("F", "added hello"))
        self.assertEqual(done["done"], "updated PR #9 after the review")


class ContainedWrites(CheckoutOnDisk):
    def setUp(self):
        super().setUp()
        asyncio.run(m.start_build("B"))
        self.tree = core.BASE / room_context.repo_dir(github.REPO) / "issue-7" / "tree"

    def test_writes_land_in_the_checkout(self):
        out = m.write_files("B", {"docs/HELLO.md": "# Hello\n"})
        self.assertEqual(out["written"], ["docs/HELLO.md"])
        self.assertEqual((self.tree / "docs/HELLO.md").read_text(), "# Hello\n")
        self.assertEqual(m.read_file("B", "docs/HELLO.md")["content"], "# Hello\n")

    def test_paths_outside_the_repo_are_refused_and_nothing_is_written(self):
        for bad in ("/etc/passwd", "../escape.md", "docs/../../x", ".git/hooks/pre-commit", ""):
            out = m.write_files("B", {"ok.md": "x", bad: "evil"})
            self.assertIn("error", out, bad)
        self.assertFalse((self.tree / "ok.md").exists())      # all-or-nothing
        self.assertIn("error", m.read_file("B", "../../../etc/passwd"))

    def test_symlink_out_of_the_repo_is_refused(self):
        self.tree.mkdir(parents=True, exist_ok=True)
        (self.tree / "link").symlink_to("/tmp")
        self.assertIn("error", m.write_files("B", {"link/x.md": "evil"}))


if __name__ == "__main__":
    unittest.main()
