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

    def test_gpt_review_cases(self):
        self.assertEqual(public_safe(r"Saved C:\Users\bob\notes.txt and file:///Users/x/a.md"),
                         "Saved (a local file) and (a local file)")                 # Windows and file:// too
        web = "See [guide](https://example.com/opt/guide) and https://example.com/tmp/x"
        self.assertEqual(public_safe(web), web)                                      # web links untouched
        work = "I'll document the caveat. Fixed the regression.\n\nTests pass."
        self.assertEqual(public_safe(work), work)                                    # more than a plan: kept
        self.assertEqual(public_safe("I'll change it."), "I'll change it.")          # nothing else: kept
        self.assertEqual(public_safe("token ghp_abcdefghijklmnop123 here"), "token <redacted> here")


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
        self.assertIn("[Review and merge it on GitHub](https://github.com/csheargm/hagency-demo-sandbox/pull/167)", said)
        self.assertNotIn("retry", said)                       # GPT review: retry can't move a PR that's open
        self.assertNotIn("Make the issue clearer", said)
        self.assertNotIn(".log", said)
        self.assertEqual(item["stage"], "stopped")

    def test_a_paused_pr_a_person_merged_is_done(self):
        """GPT review: paused items weren't checked, so "once it's merged, #N is done" wasn't true."""
        lp = SimpleNamespace(save=mock.Mock(), say=mock.AsyncMock(), state={
            "o/r#166": {"room": "!r", "repo": "o/r", "stage": "stopped", "pr": 167},
            "o/r#5": {"room": "!r", "repo": "o/r", "stage": "stopped"}})                  # no PR: never asked
        with mock.patch.object(pm_paused.github, "run_json", mock.AsyncMock(return_value={"state": "MERGED"})) as asked:
            asyncio.run(pm_paused.finish_merged(lp, "!r", "o/r"))
            asyncio.run(pm_paused.finish_merged(lp, "!r", "o/r"))                         # done: not asked again
        self.assertEqual(asked.await_count, 1)
        self.assertEqual(lp.state["o/r#166"]["stage"], "done")
        self.assertEqual(lp.state["o/r#5"]["stage"], "stopped")
        self.assertIn("#166 done", lp.say.call_args.args[1])

    def test_a_paused_pr_still_open_is_asked_about_again_only_later(self):
        lp = SimpleNamespace(save=mock.Mock(), say=mock.AsyncMock(),
                             state={"o/r#166": {"room": "!r", "repo": "o/r", "stage": "stopped", "pr": 167}})
        with mock.patch.object(pm_paused.github, "run_json", mock.AsyncMock(return_value={"state": "OPEN"})) as asked:
            for _ in range(3):
                asyncio.run(pm_paused.finish_merged(lp, "!r", "o/r"))
        self.assertEqual((asked.await_count, lp.state["o/r#166"]["stage"]), (1, "stopped"))
        lp.say.assert_not_called()

    def test_other_pauses_keep_the_general_steps(self):
        self.assertIn("Make the issue clearer", pm_paused.next_steps(5, "@a-agent:x"))


if __name__ == "__main__":
    unittest.main()
