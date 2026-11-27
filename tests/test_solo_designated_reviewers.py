"""Solo review never waives a code owner's or a designated team's approval (GPT whole-batch review): the admin
bypass is not added to a ruleset that requires one, and an admin merge refuses a branch whose rules (or classic
protection) require one. Run: .venv/bin/python -m unittest tests.test_solo_designated_reviewers"""
import unittest

from tests import test_solo_github as sg
from tests.test_solo_github import FakeGitHub

CODE_OWNERS = {"type": "pull_request", "parameters": {"require_code_owner_review": True}}
TEAM = {"type": "pull_request", "parameters": {"required_reviewers": [
    {"minimum_approvals": 1, "file_patterns": ["deploy/**"], "reviewer": {"id": 3, "type": "Team"}}]}}
WORDS = ((CODE_OWNERS, "code owner"), (TEAM, "designated reviewers"))


class NoBypassAdded(unittest.TestCase):
    go = sg.Ruleset.go

    def test_a_ruleset_that_names_its_reviewers_is_left_alone(self):
        for rule, word in WORDS:
            gh = FakeGitHub()
            gh.rulesets[42]["rules"] = [rule]
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, word):
                self.go(gh, {"on": True})
            self.assertEqual(gh.puts, [])

    def test_a_plain_review_rule_still_gets_the_bypass(self):
        gh = FakeGitHub()
        gh.rulesets[42]["rules"] = [{"type": "pull_request", "parameters": {
            "require_code_owner_review": False, "required_reviewers": []}}]
        self.go(gh, {"on": True})
        self.assertTrue(gh.puts)


class NoAdminMerge(unittest.TestCase):
    PR, merge = sg.AdminMerge.PR, sg.AdminMerge.merge

    def test_never_past_a_code_owner_or_team_requirement(self):
        for rule, word in WORDS:
            gh = FakeGitHub(extra_rules=[{**rule, "ruleset_id": 42}])
            with self.subTest(word=word), self.assertRaisesRegex(ValueError, word):
                self.merge(gh)
            self.assertEqual(gh.ran, [])

    def test_classic_code_owner_reviews_are_a_persons_merge(self):
        gh = FakeGitHub()
        gh.protection = {"required_pull_request_reviews": {"required_approving_review_count": 1,
                                                           "require_code_owner_reviews": True}}
        with self.assertRaisesRegex(ValueError, "code owner"):
            self.merge(gh)
        self.assertEqual(gh.ran, [])


if __name__ == "__main__":
    unittest.main()
