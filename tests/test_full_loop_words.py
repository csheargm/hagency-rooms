"""The full-loop live test (2026-11-23): two people, two agents, one GitHub account."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from adapters.agents import tasks
from common import github
from server import pm_plan

PM = "@pm:x"


def msg(sender, body):
    return {"type": "m.room.message", "sender": sender, "content": {"body": body}}


class Discussion(unittest.TestCase):
    def test_a_new_plan_reads_only_what_was_said_after_the_last_filed_one(self):
        """Live: "add LOOP_TEST.md" was planned together with the older, already filed GH_LIVE_TEST.md."""
        events = [msg("@g:x", "add GH_LIVE_TEST.md"), msg(PM, pm_plan.OPENED + "#166. The agents start on them now."),
                  msg("@g:x", "add LOOP_TEST.md")]
        lp = SimpleNamespace(user_id=PM, client=SimpleNamespace(access_token="t"))
        with mock.patch.object(pm_plan, "api", return_value={"chunk": list(reversed(events))}):
            said = pm_plan.Planner.discussion(lp, "!r")
        self.assertEqual(said, "g: add LOOP_TEST.md")


class OwnPullRequest(unittest.TestCase):
    def test_an_approval_github_refuses_is_posted_as_a_comment(self):
        """Live: both agents used one GitHub account; GitHub answered 422 to approving its own PR."""
        calls = []

        async def run(args, agent):
            calls.append(args)
            if "event=APPROVE" in args:
                raise github.GitHubError("gh api -X failed: gh: Unprocessable Entity (HTTP 422)")
            return ""
        with mock.patch.object(tasks, "run", run):
            out = asyncio.run(tasks.submit_review("h-agent", 168, 170, "abc", "APPROVE", "Looks right.", "o/r"))
        self.assertEqual(out["verdict"], "approve")
        self.assertIn("event=COMMENT", calls[-1])
        self.assertTrue(any(a.startswith("body=Verdict: APPROVE") for a in calls[-1]))

    def test_other_github_errors_still_fail(self):
        async def run(args, agent):
            raise github.GitHubError("HTTP 403")
        with mock.patch.object(tasks, "run", run), self.assertRaises(github.GitHubError):
            asyncio.run(tasks.submit_review("h-agent", 1, 2, "abc", "APPROVE", "ok", "o/r"))


if __name__ == "__main__":
    unittest.main()
