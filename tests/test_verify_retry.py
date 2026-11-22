"""A paused issue a person can act on: Hermes can verify/close (it used to fail "unsupported"),
every pause says what to do, and "@pm retry #N" puts the issue back in the queue.
Run: .venv/bin/python -m unittest tests.test_verify_retry"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("HAGENCY_AGENT", "anna-hermes-agent")
os.environ.setdefault("HAGENCY_ROOM", "!r:bridge.localhost")
from common import github  # noqa: E402
from mcp_tools import hagency_mcp as m  # noqa: E402
from common import intents  # noqa: E402
from server import loop  # noqa: E402
from mcp_tools import mcp_core as core  # noqa: E402
from mcp_tools import mcp_verify  # noqa: E402
from adapters.agents import room_context  # noqa: E402
from common import room_state  # noqa: E402
from adapters.agents import tasks  # noqa: E402
from tests.test_hagency_mcp import task_event  # noqa: E402

REPO = "your-name/hagency-hello"


class HermesVerifies(unittest.TestCase):
    """Seen live: "verify failed for #9: the Hagency verify workflow is unsupported here"."""
    def setUp(self):
        core._open.clear()
        self.posted = []
        for target, name, fn in ((core, "owner_in", lambda room: True), (core, "token", lambda: "t"),
                                 (room_context, "room_repo", lambda token, room: github.REPO),
                                 (core, "post", lambda line, result=None, room=None: self.posted.append(result))):
            p = mock.patch.object(target, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def events(self, evs):
        p = mock.patch.object(core, "recent_events", lambda: evs)
        p.start()
        self.addCleanup(p.stop)

    def test_verify_and_close_are_supported(self):
        for kind, step in (("verify", "get_verify"), ("close", "close_issue")):
            core._open.clear()
            self.events([task_event("T", kind)])
            self.assertEqual(m.next_task()["next"], f"Call {step}(task_id).")

    def test_verify_flow_reports_the_verdict(self):
        tree = Path(tempfile.mkdtemp())
        (tree / "index.html").write_text("<h1>Habari!</h1>")
        (tree / ".git").mkdir()
        (tree / ".git" / "config").write_text("secret-ish")
        outside = Path(tempfile.mkdtemp()) / "id_rsa"
        outside.write_text("PRIVATE-KEY")
        (tree / "key").symlink_to(outside)                   # a planted link out of the clone
        (tree / "dir").symlink_to(outside.parent, target_is_directory=True)

        async def material(agent, base, task):
            return 7, tree, "Check issue #7."
        self.events([task_event("V", "verify")])
        self.assertIn("get_verify", asyncio.run(m.submit_verify("V", True, "x"))["next"])   # out of order: a hint
        with mock.patch.object(tasks, "verify_material", material):
            got = asyncio.run(m.get_verify("V"))
        self.assertIn("Habari!", got["instructions"])
        self.assertNotIn("secret-ish", got["instructions"])                                # .git is never shown
        self.assertNotIn("PRIVATE-KEY", got["instructions"])                               # links aren't followed
        asyncio.run(m.submit_verify("V", True, "every criterion met"))
        self.assertEqual((self.posted[-1]["kind"], self.posted[-1]["verdict"]), ("verify", "approve"))

    def test_a_repo_too_large_to_read_is_never_confirmed(self):
        tree = Path(tempfile.mkdtemp())
        (tree / "big.txt").write_text("x" * (tasks.MAX_DIFF_CHARS + 1))
        with self.assertRaises(tasks.TaskError):
            mcp_verify.repo_text(tree)

    def test_close_refused_unless_autonomous(self):
        self.events([task_event("C", "close", verifier="@b:x")])
        with mock.patch.object(core, "api", lambda *a, **k: {"level": "assist", "repo": github.REPO}):
            asyncio.run(m.close_issue("C"))
        self.assertIn("not autonomous", self.posted[-1]["error"])


class Retry(unittest.TestCase):
    def setUp(self):
        owners = mock.patch("server.ownership.owner_id", lambda a: a.replace("-agent", ""))   # each agent: its own person
        owners.start()
        self.addCleanup(owners.stop)
        no_prs = mock.patch("server.pm_paused.agent_prs", mock.AsyncMock(return_value={}))   # no unrecorded PRs
        no_prs.start()
        self.addCleanup(no_prs.stop)
        self.lp = loop.Loop.__new__(loop.Loop)
        self.lp.lock = asyncio.Lock()
        self.lp.room_repos = {"!r": REPO}
        self.lp.level = mock.AsyncMock(return_value="autonomous")
        self.lp.state = {f"{REPO}#9": {"room": "!r", "repo": REPO, "stage": "verifying", "builder": "@b-agent:x",
                                       "reviewer": "@r-agent:x", "task_id": "T"}}
        self.lp.save = lambda: None
        self.lp.say = mock.AsyncMock()
        self.lp.client = SimpleNamespace()
        self.room = SimpleNamespace(room_id="!r")
        self.mod = mock.patch.object(room_state, "is_moderator", mock.AsyncMock(return_value=True))
        self.mod.start()
        self.addCleanup(self.mod.stop)

    def said(self):
        return self.lp.say.call_args.args[1]

    def fail_verify(self):
        asyncio.run(self.lp.on_result(self.room, "@r-agent:x", {"kind": "verify", "issue": 9, "task_id": "T",
                                                               "error": "unsupported"}))

    def test_a_pause_says_what_to_do(self):
        """the owner: "it says handed off to manual review and I have no idea what I need to do"."""
        self.fail_verify()
        self.assertEqual(self.lp.state[f"{REPO}#9"]["stage"], "stopped")
        self.assertIn("@pm retry #9", self.said())

    def test_retry_puts_it_back_in_the_queue(self):
        self.fail_verify()
        self.assertEqual((intents.classify("retry #9"), intents.issue_named("retry #9")), (intents.RETRY, 9))
        asyncio.run(self.lp.retry(self.room, "@anna:x", 9))
        self.assertNotIn(f"{REPO}#9", self.lp.state)          # the next poll picks the open issue up again
        self.assertIn("back in the queue", self.said())

    def test_retry_without_a_number_takes_the_only_paused_one(self):
        self.fail_verify()
        asyncio.run(self.lp.retry(self.room, "@anna:x", None))
        self.assertNotIn(f"{REPO}#9", self.lp.state)

    def test_retry_of_something_not_paused_changes_nothing(self):
        asyncio.run(self.lp.retry(self.room, "@anna:x", 9))    # still verifying: not paused
        self.assertIn(f"{REPO}#9", self.lp.state)
        self.assertIn("Nothing is paused", self.said())

    def test_retry_never_starts_a_second_pr(self):
        self.fail_verify()
        self.lp.state[f"{REPO}#9"]["pr"] = 14
        self.lp.pr_open = mock.AsyncMock(return_value=True)
        asyncio.run(self.lp.retry(self.room, "@anna:x", 9))
        self.assertIn(f"{REPO}#9", self.lp.state)
        self.assertIn("PR #14 for #9 is still open", self.said())

    def test_only_a_moderator_can_retry(self):
        self.fail_verify()
        with mock.patch.object(room_state, "is_moderator", mock.AsyncMock(return_value=False)):
            asyncio.run(self.lp.retry(self.room, "@betty:x", 9))
        self.assertIn(f"{REPO}#9", self.lp.state)


class Review13(unittest.TestCase):
    """GPT review 13: approvals that weren't, negated modes, retry guesses, unbounded verify."""
    def test_talk_is_not_consent(self):
        for t in ("good for you", "let us approve", "yes?", "ok 👎", "ok?"):
            self.assertFalse(intents.is_approval(t) or intents.is_yes(t), t)

    def test_a_negated_mode_is_not_a_request(self):
        self.assertEqual(intents.classify("do not go autonomous"), intents.UNKNOWN)
        self.assertEqual(intents.classify("take it all the way"), intents.AUTONOMOUS)

    def test_retry_numbers(self):
        self.assertEqual((intents.issue_named("retry 9"), intents.issue_named("retry #12345678")), (9, None))

    def test_verify_size_counts_every_file(self):
        tree = Path(tempfile.mkdtemp())
        for i in range(mcp_verify.MAX_FILES + 1):
            (tree / f"f{i}").write_text("")
        with self.assertRaises(tasks.TaskError):
            mcp_verify.repo_text(tree)


class OneClick(unittest.TestCase):
    """the owner: "make a clickable action right on the message" — @pm puts the 👍 under its question."""
    def test_pm_adds_the_thumb_and_its_own_never_counts(self):
        lp = loop.Loop.__new__(loop.Loop)
        lp.user_id, lp.client = "@pm:x", SimpleNamespace(room_send=mock.AsyncMock())
        asyncio.run(lp.offer_thumb("!r", "$plan"))
        room_id, kind, content = lp.client.room_send.call_args.args
        self.assertEqual((kind, content["m.relates_to"]["event_id"], content["m.relates_to"]["key"]),
                         ("m.reaction", "$plan", "👍"))
        room = SimpleNamespace(users={"@pm:x": None, "@anna:x": None}, invited_users={})
        self.assertEqual((lp.is_person(room, "@pm:x"), lp.is_person(room, "@anna:x")), (False, True))


if __name__ == "__main__":
    unittest.main()
