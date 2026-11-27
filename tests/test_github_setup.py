"""Unit tests for github_setup's readiness check and readiness_identity. Run: .venv/bin/python -m unittest tests.test_github_setup"""
import asyncio
import unittest
from unittest import mock

from common.github import GitHubError
from server import github_setup
from server import readiness_identity
from server.github_setup import evaluate

GATED = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
ONE_OWNER = mock.patch.object(github_setup.registry, "owner_of", lambda a: "@one:s")   # one person's agents
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
        self.assertIn("Settings → Collaborators → Add people", p[0])

    def test_write_access_unknown_is_never_ready_nor_signed_out(self):
        p = evaluate({**OK, "betty-agent": {"login": "b", "push": None}}, GATED)
        self.assertEqual(len(p), 1)
        self.assertIn("couldn't check its Write access", p[0])
        self.assertNotIn("not signed in", p[0])

    def test_an_agents_own_report_says_its_own_fix(self):
        self.assertEqual(evaluate({"betty-agent": {"problem": "betty-agent: do this"}}, GATED), ["betty-agent: do this"])

    @ONE_OWNER
    def test_shared_account(self):
        shared = {"anna-agent": {"login": "a", "push": True}, "betty-agent": {"login": "a", "push": True}}
        p = evaluate(shared, GATED)                       # with the rule: it can't be met, said with the switch
        self.assertEqual(len(p), 1)
        self.assertIn("anna-agent and betty-agent share the GitHub account @a", p[0])
        self.assertIn(github_setup.SOLO_FIX, p[0])
        self.assertEqual(evaluate(shared, []), [])        # without it: one account works (ADR 0016)
        mixed = {**shared, "betty-agent": {"login": "A", "push": True}}   # GitHub's names ignore case
        self.assertEqual(len(evaluate(mixed, GATED)), 1)

    @ONE_OWNER
    def test_solo_review_needs_an_admin_and_the_rules_bypass(self):
        """GPT review: Solo on alone isn't enough; GitHub lets only a repo admin past, once the rule allows it."""
        admin = {"login": "a", "push": True, "admin": True}
        shared = {"anna-agent": admin, "betty-agent": admin}
        self.assertEqual(evaluate(shared, GATED, solo=True, bypass=True), [])
        self.assertIn("Bypass list", evaluate(shared, GATED, solo=True, bypass=False)[0])
        self.assertNotIn("**", evaluate(shared, GATED, solo=True, bypass=False)[0])     # plain text in the panel
        writer = {a: {**admin, "admin": False} for a in shared}
        self.assertIn("@a isn't an admin of", evaluate(writer, GATED, solo=True, bypass=True)[0])
        unknown = {"anna-agent": {**admin, "admin": None}}
        self.assertIn("couldn't be checked", evaluate(unknown, GATED, solo=True, bypass=True)[0])

    def test_enough_other_accounts_for_the_approvals(self):
        """GPT review: one agent alone, or two accounts for a rule needing two approvals, can't meet the rule."""
        alone = {"anna-agent": {"login": "a", "push": True}}
        self.assertIn("only 1 GitHub account is here", evaluate(alone, GATED)[0])
        two = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}]
        three = {**OK, "cx-agent": {"login": "c", "push": True}}
        self.assertIn("2 approving reviews) can't be met (the loop has one checker", evaluate(three, two)[0])
        self.assertEqual(github_setup.approvals(two + GATED), 2)
        self.assertEqual(github_setup.approvals([{"type": "deletion"}]), 0)

    def test_verified_lists_what_was_checked(self):
        admin = {"login": "a", "push": True, "admin": True}
        seen = github_setup.verified({"anna-agent": admin, "betty-agent": admin}, GATED, "o/r", "main")
        self.assertEqual(seen[0], "anna-agent and betty-agent: signed in to GitHub as @a (as each reports), "
                                  "and GitHub says @a can write to o/r")
        self.assertIn("'agent-ok' label is on o/r", seen[1])
        self.assertIn("Solo review is on", seen[2])
        self.assertIn("2 different GitHub accounts", github_setup.verified(OK, GATED, "o/r", "main")[-1])
        self.assertIn("no review rule", github_setup.verified(OK, [], "o/r", "main")[-1])


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
                mock.patch.object(github_setup, "identity", lambda a, r: asyncio.sleep(0, {"login": a, "push": True})):
            _, problems, seen = asyncio.run(github_setup.readiness("o/r", ["anna-agent", "betty-agent"]))
        self.assertEqual(problems, [])
        self.assertIn("the 'agent-ok' label is on o/r", seen)
        self.assertIn("repos/o/r/rules/branches/trunk", calls)

    def test_a_server_agent_signed_in_but_repo_unreadable_is_unknown(self):
        """GPT review: a confirmed login whose repo read failed was called "not signed in"."""
        async def run_json(args, agent=None):
            if args == ["api", "user"]:
                return {"login": "a"}
            raise GitHubError("gh api failed: 502")
        with mock.patch.object(github_setup, "run_json", run_json):
            ident = asyncio.run(github_setup.identity("anna-agent", "o/r"))
        self.assertEqual(ident, {"login": "a", "push": None, "admin": None})
        self.assertIn("couldn't check its Write access", evaluate({"anna-agent": ident}, [])[0])

    def test_a_missing_label_says_githubs_own_steps(self):
        async def run_json(args, agent=None):
            return {"default_branch": "main"} if args[1] == "repos/o/r" else []    # no labels, no review rule
        with mock.patch.object(github_setup, "run_json", run_json), \
                mock.patch.object(github_setup, "identity", mock.AsyncMock(return_value={"login": "a", "push": True})):
            _, problems, seen = asyncio.run(github_setup.readiness("o/r", ["anna-agent"]))
        self.assertEqual(problems, [github_setup.LABEL_STEPS.format(label="agent-ok", repo="o/r")])
        self.assertIn("Issues → Labels → New label", problems[0])
        self.assertEqual(seen, [])

    def test_no_raw_errors_without_detail(self):
        p = evaluate({"betty-agent": {"error": "token at /secret/path expired"}}, GATED, detail=False)
        self.assertEqual(p, ["betty-agent: not signed in to GitHub"])


class ReportedIdentity(unittest.TestCase):
    """readiness_identity: an agent on a person's computer counts by its own report, never by gh on the server."""
    REMOTE = {"localpart": "cx-agent", "agent": "remote"}

    def reported(self, said, configured=True, level="write"):
        found = {**self.REMOTE, **({"github": said} if said is not None else {})}
        perm = mock.AsyncMock(side_effect=level) if isinstance(level, Exception) else mock.AsyncMock(return_value=level)
        with mock.patch.object(readiness_identity.github_app, "configured", lambda: configured), \
                mock.patch.object(readiness_identity.github_app, "permission", perm):
            return asyncio.run(readiness_identity.reported("cx-agent", found, "o/r")), perm

    def test_each_state(self):
        def say(text):
            return {"problem": text.format(agent="cx-agent")}
        self.assertEqual(self.reported(None)[0], say(readiness_identity.NO_REPORT))
        self.assertEqual(self.reported({"state": "none", "login": None})[0], say(readiness_identity.SIGNED_OUT))
        self.assertEqual(self.reported({"state": "no_gh", "login": None})[0], say(readiness_identity.NO_GH))
        self.assertEqual(self.reported({"state": "signed_in", "login": None})[0], say(readiness_identity.NO_LOGIN))
        ident, perm = self.reported({"state": "signed_in", "login": "csheargm"})
        self.assertEqual(ident, {"login": "csheargm", "push": True, "admin": False, "reported": True})
        perm.assert_awaited_once_with("o/r", "csheargm")       # read as the server's App

    def test_hermes_without_a_report_is_unknown(self):
        found = {**self.REMOTE, "agent_kind": "hermes"}
        self.assertIn("couldn't check", asyncio.run(readiness_identity.reported("cx-agent", found, "o/r"))["problem"])

    def test_write_access(self):
        for level, push in (("admin", True), ("maintain", True), ("write", True), ("read", False), ("none", False)):
            ident = self.reported({"state": "signed_in", "login": "a"}, level=level)[0]
            self.assertEqual((ident["push"], ident["admin"]), (push, level == "admin"), level)
        unknown = self.reported({"state": "signed_in", "login": "a"}, level=GitHubError("502"))[0]
        self.assertEqual((unknown["push"], unknown["admin"]), (None, None))

    def test_without_the_app_the_hosts_gh_reads_it(self):
        read = mock.AsyncMock(return_value={"permission": "write"})
        with mock.patch.object(readiness_identity.github, "run_json", read):
            ident, perm = self.reported({"state": "signed_in", "login": "a"}, configured=False)
        self.assertTrue(ident["push"])
        read.assert_awaited_once_with(["api", "repos/o/r/collaborators/a/permission"])   # @pm's own read, no agent
        perm.assert_not_awaited()

    def test_only_a_server_run_agent_is_asked_with_gh(self):
        gh = mock.AsyncMock(return_value={"login": "a", "push": True})
        with mock.patch.object(github_setup, "identity", gh), \
                mock.patch.object(readiness_identity, "entry_of", lambda a: dict(self.REMOTE) if a == "cx-agent" else None), \
                mock.patch.object(readiness_identity, "reported", mock.AsyncMock(return_value={"problem": "p"})):
            self.assertEqual(asyncio.run(github_setup.agent_identity("cx-agent", "o/r")), {"problem": "p"})
            asyncio.run(github_setup.agent_identity("anna-agent", "o/r"))
        gh.assert_awaited_once_with("anna-agent", "o/r")


if __name__ == "__main__":
    unittest.main()
