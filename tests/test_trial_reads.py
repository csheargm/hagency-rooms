"""Unit tests: the trial agent reads a PR only of the room's repo, read-only and scrubbed (ADR 0025).
Run: .venv/bin/python -m unittest tests.test_trial_reads"""
import unittest
from unittest import mock

from common import github
from server import trial_reads
from tests.test_trial_agent import NEW, Base, hear, message


class PullRequests(Base):
    def test_a_pr_of_this_rooms_repo_is_read_and_sent_with_the_question(self):
        self.enrol()
        with mock.patch.object(trial_reads, "pr_diff", mock.AsyncMock(return_value="acme/app PR #3: diff text")) as read:
            self.run_(hear(self.agent, self.client.rooms["!trialdm"], message(NEW, "what does PR #3 change?")))
        read.assert_awaited_once_with("acme/app", 3)
        self.assertIn("diff text", self.asked[0][-1]["content"])

    def test_only_this_rooms_repo(self):
        self.assertEqual(trial_reads.pr_number("see https://github.com/acme/app/pull/7", "acme/app"), 7)
        self.assertIsNone(trial_reads.pr_number("see https://github.com/other/secret/pull/7", "acme/app"))
        self.assertIsNone(trial_reads.pr_number("PR #7", None))
        self.assertEqual(trial_reads.pr_number("review pull request 12 please", "acme/app"), 12)

    def test_the_diff_is_read_only_through_the_app_and_scrubbed(self):
        token = "ghp_" + "B" * 30
        calls = []

        async def get(repo, path):
            calls.append((repo, path))
            return {"title": "Add x", "state": "open"} if path.endswith("/3") else [
                {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": f"+KEY={token}"}]
        with mock.patch.object(trial_reads.github_app, "configured", lambda: True), \
                mock.patch.object(trial_reads.github_app, "get", get):
            text = self.run_(trial_reads.pr_diff("acme/app", 3))
        self.assertEqual([p for _, p in calls], ["/repos/acme/app/pulls/3", "/repos/acme/app/pulls/3/files?per_page=100"])
        self.assertIn("a.py", text)
        self.assertNotIn(token, text)

    def test_no_app_or_an_unreadable_pr_is_said(self):
        with mock.patch.object(trial_reads.github_app, "configured", lambda: False):
            self.assertEqual(self.run_(trial_reads.pr_diff("acme/app", 3)), trial_reads.NO_APP)
        with mock.patch.object(trial_reads.github_app, "configured", lambda: True), \
                mock.patch.object(trial_reads.github_app, "get", mock.AsyncMock(side_effect=github.GitHubError("404"))):
            self.assertIn("couldn't read PR #3", self.run_(trial_reads.pr_diff("acme/app", 3)))


if __name__ == "__main__":
    unittest.main()
