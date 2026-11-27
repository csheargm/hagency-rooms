"""Readiness pairs reviewers the way routing does (GPT whole-batch review): one owner's two agents on two GitHub
accounts are solo review (a comment on GitHub), so a review rule they can't meet is never reported as met.
Run: .venv/bin/python -m unittest tests.test_readiness_owners"""
import unittest
from unittest import mock

from server import github_setup
from server.github_setup import evaluate

GATED = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
OWNERS = {"cx-agent": "@cx:s", "cx-codex-agent": "@cx:s", "betty-agent": "@betty:s"}
CX_TWO = {"cx-agent": {"login": "cx", "push": True}, "cx-codex-agent": {"login": "cx-bot", "push": True}}


class OwnerPairing(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(github_setup.registry, "owner_of", OWNERS.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_owners_two_accounts_cant_meet_the_rule_without_solo_review(self):
        problems = evaluate(CX_TWO, GATED)
        self.assertEqual(len(problems), 1)
        self.assertIn("one person's", problems[0])
        self.assertIn(github_setup.SOLO_FIX, problems[0])

    def test_with_solo_review_it_needs_the_admin_merge_like_one_account(self):
        writer = {a: {**i, "admin": False} for a, i in CX_TWO.items()}
        self.assertIn("isn't an admin of", evaluate(writer, GATED, solo=True, bypass=True)[0])
        admins = {a: {**i, "admin": True} for a, i in CX_TWO.items()}
        self.assertEqual(evaluate(admins, GATED, solo=True, bypass=True), [])

    def test_another_persons_agent_on_another_account_meets_it(self):
        self.assertEqual(evaluate({**CX_TWO, "betty-agent": {"login": "b", "push": True}}, GATED), [])


if __name__ == "__main__":
    unittest.main()
