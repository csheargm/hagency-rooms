"""Unit tests for github_setup.evaluate. Run: .venv/bin/python -m unittest tests.test_github_setup"""
import asyncio
import unittest
from unittest import mock

from server import github_setup
from server.github_setup import evaluate

GATED = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
OK = {"anna-agent": {"login": "a", "push": True}, "betty-agent": {"login": "b", "push": True}}


class Evaluate(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(evaluate(OK, GATED), [])

    def test_no_review_rule_is_fine(self):
        """ADR 0016: the rule is optional; without it the loop's own reviewer decides (final review 2026-11-24)."""
        self.assertEqual(evaluate(OK, []), [])
        rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 0}}]
        self.assertEqual(evaluate(OK, rules), [])

    def test_not_signed_in(self):
        p = evaluate({**OK, "betty-agent": {"error": "no identity"}}, GATED)
        self.assertIn("betty-agent: not signed in", p[0])

    def test_no_write(self):
        p = evaluate({**OK, "betty-agent": {"login": "b", "push": False}}, GATED)
        self.assertIn("collaborator", p[0])

    def test_shared_account(self):
        shared = {"anna-agent": {"login": "a", "push": True}, "betty-agent": {"login": "a", "push": True}}
        p = evaluate(shared, GATED)                       # with the rule: it can't be met, said with both ways out
        self.assertIn("share a GitHub account", p[0])
        self.assertIn("remove the rule", p[0])
        self.assertEqual(evaluate(shared, []), [])        # without it: one account works (ADR 0016)


class Readiness(unittest.TestCase):
    """GPT review 10: the check read `main` even where the default branch differs, and the console
    showed raw GitHub CLI errors to room moderators."""
    def test_checks_the_default_branch(self):
        calls = []

        async def run_json(args, agent=None):
            calls.append(args[1] if args[0] == "api" else args[0])
            if args[1] == "repos/o/r":
                return {"default_branch": "trunk"}
            return [{"name": "agent-ok"}] if args[0] == "label" else GATED
        with mock.patch.object(github_setup, "run_json", run_json), \
                mock.patch.object(github_setup, "identity", mock.AsyncMock(return_value={"login": "a", "push": True})):
            _, problems = asyncio.run(github_setup.readiness("o/r", ["anna-agent"]))
        self.assertEqual(problems, [])
        self.assertIn("repos/o/r/rules/branches/trunk", calls)

    def test_no_raw_errors_without_detail(self):
        p = evaluate({"betty-agent": {"error": "token at /secret/path expired"}}, GATED, detail=False)
        self.assertEqual(p, ["betty-agent: not signed in to GitHub"])


if __name__ == "__main__":
    unittest.main()
