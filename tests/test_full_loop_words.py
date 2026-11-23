"""The full-loop live test (2026-11-23): two people, two agents, one GitHub account."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from adapters.agents import tasks
from common import github
from server import pm_plan

PM = "@pm:x"


def msg(sender, body, formatted=False):
    content = {"body": body, **({"format": "org.matrix.custom.html"} if formatted else {})}
    return {"type": "m.room.message", "sender": sender, "content": content}


class Discussion(unittest.TestCase):
    def test_a_new_plan_reads_only_what_was_said_after_the_last_filed_one(self):
        """Live: "add LOOP_TEST.md" was planned together with the older, already filed GH_LIVE_TEST.md."""
        events = [msg("@g:x", "add GH_LIVE_TEST.md"), msg(PM, pm_plan.OPENED + "#166. The agents start on them now.", formatted=True),
                  msg("@g:x", "add LOOP_TEST.md")]
        lp = SimpleNamespace(user_id=PM, client=SimpleNamespace(access_token="t"))
        with mock.patch.object(pm_plan, "api", return_value={"chunk": list(reversed(events))}):
            said = pm_plan.Planner.discussion(lp, "!r")
        self.assertEqual(said, "g: add LOOP_TEST.md")

    def test_a_plain_model_reply_that_looks_like_the_filing_line_resets_nothing(self):
        events = [msg("@g:x", "add A"), msg(PM, "Opened #1. The agents start on them now."), msg("@g:x", "add B")]
        lp = SimpleNamespace(user_id=PM, client=SimpleNamespace(access_token="t"))
        with mock.patch.object(pm_plan, "api", return_value={"chunk": list(reversed(events))}):
            self.assertEqual(pm_plan.Planner.discussion(lp, "!r"), "g: add A\ng: add B")


class OwnPullRequest(unittest.TestCase):
    def run_review(self, author):
        async def run(args, agent):
            if "event=APPROVE" in args:
                raise github.GitHubError("gh api -X failed: gh: Unprocessable Entity (HTTP 422)")
            return ""

        async def run_json(args, agent):
            return {"login": "csheargm"} if args[:2] == ["api", "user"] else {"author": {"login": author}}
        with mock.patch.object(tasks, "run", run), mock.patch.object(tasks.own_account, "run_json", run_json):
            return asyncio.run(tasks.submit_review("h-agent", 168, 170, "abc", "APPROVE", "Looks right.", "o/r"))

    def test_one_account_for_both_agents_posts_the_verdict_as_a_comment(self):
        """ADR 0016: one GitHub account for both agents must not block the loop."""
        calls = []

        async def run(args, agent):
            calls.append(args)
            if "event=APPROVE" in args:
                raise github.GitHubError("gh api -X failed: gh: Unprocessable Entity (HTTP 422)")
            return ""

        async def run_json(args, agent):
            return {"login": "csheargm"} if args[:2] == ["api", "user"] else {"author": {"login": "CSHEARGM"}}
        with mock.patch.object(tasks, "run", run), mock.patch.object(tasks.own_account, "run_json", run_json):
            out = asyncio.run(tasks.submit_review("h-agent", 168, 170, "abc", "APPROVE", "Looks right.", "o/r"))
        self.assertEqual(out["verdict"], "approve")
        self.assertIn("as a comment", out["line"])                          # says what GitHub recorded
        self.assertIn("event=COMMENT", calls[-1])
        self.assertTrue(any(a.startswith("body=Verdict: APPROVE") for a in calls[-1]))

    def test_a_422_for_another_reason_is_raised_as_it_was(self):
        with self.assertRaises(github.GitHubError):
            self.run_review("someone-else")

    def test_a_repo_that_requires_an_approval_pauses_with_a_link(self):
        from server import pm_paused
        said = pm_paused.approve_steps("o/r", 170)
        self.assertIn("[Approve and merge it on GitHub](https://github.com/o/r/pull/170)", said)

    def test_planning_marker_is_only_the_real_filing_line(self):
        self.assertTrue(pm_plan.FILED_RE.match("Opened #168, #169. The agents start on them now."))
        self.assertFalse(pm_plan.FILED_RE.match("Opened the door for you."))


if __name__ == "__main__":
    unittest.main()
