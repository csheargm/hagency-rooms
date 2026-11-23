"""Words from the live GitHub test (2026-11-23): a new agent's PR, alone in a room."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from common.redact import public_safe
from server import pm_paused


class PullRequestText(unittest.TestCase):
    def test_no_local_paths_or_preamble_reach_github(self):
        said = ("I’ll add `GH_LIVE_TEST.md` at the repository root and verify its exact contents without running Git.\n\n"
                "Added [GH_LIVE_TEST.md](/Users/someone/claudeworkfolder/rooms/x/work/tree/GH_LIVE_TEST.md) at the "
                "repository root; see also /home/someone/notes.txt. This was the only file edited.")
        out = public_safe(said)
        self.assertTrue(out.startswith("Added GH_LIVE_TEST.md at the repository root"), out)
        for leak in ("/Users/", "/home/", "someone", "I’ll"):
            self.assertNotIn(leak, out)
        self.assertIn("This was the only file edited.", out)

    def test_a_summary_without_paths_is_kept(self):
        self.assertEqual(public_safe("Added index.html with a heading."), "Added index.html with a heading.")


class AloneInARoom(unittest.TestCase):
    def test_a_finished_pr_with_no_reviewer_says_to_review_and_merge_it_with_a_link(self):
        from server import loop
        lp = loop.Loop.__new__(loop.Loop)
        lp.state = {}
        lp.save = lambda: None
        lp.say = mock.AsyncMock()
        lp.agents_in = lambda room: ["@g-agent:x"]
        lp.agent_online = lambda a: True
        item = {"repo": "csheargm/hagency-demo-sandbox", "pr": 167, "builder": "@g-agent:x", "agent": "@g-agent:x"}
        with mock.patch("server.ownership.owner_id", lambda a: a.replace("-agent", "")):
            picked = asyncio.run(lp.pick_reviewer(SimpleNamespace(room_id="!r"), item, "csheargm/hagency-demo-sandbox#166",
                                                  "review PR #167"))
        self.assertFalse(picked)
        said = lp.say.call_args.args[1]
        self.assertIn("[Review and merge PR #167 on GitHub](https://github.com/csheargm/hagency-demo-sandbox/pull/167)", said)
        self.assertNotIn("Make the issue clearer", said)
        self.assertNotIn(".log", said)
        self.assertEqual(item["stage"], "stopped")

    def test_other_pauses_keep_the_general_steps(self):
        self.assertIn("Make the issue clearer", pm_paused.next_steps(5, "@a-agent:x"))


if __name__ == "__main__":
    unittest.main()
