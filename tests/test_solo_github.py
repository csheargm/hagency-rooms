"""Solo review on GitHub, as the agent runs it (adapters/agents/solo_github.py, tasks.submit_review; ADR 0023):
the review rule's bypass for repo admins is added without touching anything else, only Hagency's own entry is
taken out, a solo verdict is a comment, and an admin merge never skips a red check or a rule it can't pass.
Run: .venv/bin/python -m unittest tests.test_solo_github"""
import asyncio
import json
import unittest
from unittest import mock

from adapters.agents import solo_github, tasks
from common import github, solo_review

REPO = "o/r"
OTHER = {"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}


class FakeGitHub:
    def __init__(self, bypass=None, source="Repository", can_bypass="pull_requests_only", readable=True):
        self.ruleset = {"id": 42, "rules": [{"type": "pull_request"}], "current_user_can_bypass": can_bypass}
        if readable:
            self.ruleset["bypass_actors"] = list(bypass or [])
        self.source, self.puts, self.ran = source, [], []

    async def run_json(self, args, agent=None):
        if args[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "main"}}
        if args[1] == f"repos/{REPO}/rules/branches/main":
            return [{"type": "pull_request", "ruleset_id": 42, "ruleset_source_type": self.source},
                    {"type": "deletion", "ruleset_id": 42}]
        if args[1] == f"repos/{REPO}/rulesets/42":
            return self.ruleset
        if args[:2] == ["pr", "view"]:
            return self.pr
        raise AssertionError(args)

    async def run(self, args, agent=None, **kw):
        self.ran.append(args)
        if args[:3] == ["api", "-X", "PUT"]:
            with open(args[args.index("--input") + 1]) as f:
                self.puts.append(json.load(f))
        return ""


class Ruleset(unittest.TestCase):
    def go(self, gh, task):
        with mock.patch.object(solo_github, "run_json", gh.run_json), mock.patch.object(solo_github, "run", gh.run):
            return asyncio.run(solo_github.ruleset("cx-agent", "codex", None, {"repo": REPO, **task}))

    def test_on_adds_the_admin_bypass_and_keeps_everything_else(self):
        gh = FakeGitHub(bypass=[OTHER])
        out = self.go(gh, {"on": True})
        self.assertEqual(out["changed"], [42])
        self.assertEqual(gh.puts, [{"bypass_actors": [OTHER, solo_review.ADMIN_BYPASS]}])   # only the bypass list

    def test_an_equal_entry_already_there_is_done(self):
        gh = FakeGitHub(bypass=[solo_review.ADMIN_BYPASS])
        out = self.go(gh, {"on": True})
        self.assertEqual((out["changed"], out["already"], gh.puts), ([], [42], []))

    def test_off_takes_out_only_hagencys_entry_and_only_where_it_added_it(self):
        gh = FakeGitHub(bypass=[OTHER, solo_review.ADMIN_BYPASS])
        self.assertEqual(self.go(gh, {"on": False, "rulesets": []})["changed"], [])   # not ours: kept
        out = self.go(gh, {"on": False, "rulesets": [42]})
        self.assertEqual((out["changed"], gh.puts), ([42], [{"bypass_actors": [OTHER]}]))

    def test_not_an_admin_or_an_org_rule_says_so(self):
        with self.assertRaisesRegex(ValueError, "must be the repo's admin"):
            self.go(FakeGitHub(readable=False), {"on": True})
        with self.assertRaisesRegex(ValueError, "organization owner"):
            self.go(FakeGitHub(source="Organization"), {"on": True})

    def test_a_refused_write_says_what_is_wrong(self):
        gh = FakeGitHub()

        async def refused(args, agent=None, **kw):
            raise solo_github.GitHubError("HTTP 403: Resource not accessible")
        with mock.patch.object(solo_github, "run_json", gh.run_json), mock.patch.object(solo_github, "run", refused):
            with self.assertRaisesRegex(ValueError, "repo's admin.*403"):
                asyncio.run(solo_github.ruleset("cx-agent", "codex", None, {"repo": REPO, "on": True}))


class AdminMerge(unittest.TestCase):
    PR = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc", "reviewDecision": "REVIEW_REQUIRED",
          "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}

    def merge(self, gh, **pr):
        gh.pr = {**self.PR, **pr}
        with mock.patch.object(solo_github, "run_json", gh.run_json), mock.patch.object(solo_github, "run", gh.run):
            return asyncio.run(tasks.merge("cx-agent", "codex", None, {"repo": REPO, "issue": 7, "pr": 9, "sha": "abc",
                                                                       "admin": True}))

    def test_merges_past_the_rule_as_admin_at_the_reviewed_commit(self):
        gh = FakeGitHub()
        self.merge(gh)
        self.assertEqual(gh.ran[-1], ["pr", "merge", "9", "-R", REPO, "--squash", "--admin", "--match-head-commit", "abc"])

    def test_never_past_a_red_check_a_moved_pr_or_a_rule_it_cant_pass(self):
        for gh, pr, why in ((FakeGitHub(), {"statusCheckRollup": [{"conclusion": "FAILURE"}]}, "check"),
                            (FakeGitHub(), {"headRefOid": "zzz"}, "reviewed commit"),
                            (FakeGitHub(), {"reviewDecision": "CHANGES_REQUESTED"}, "changes requested"),
                            (FakeGitHub(can_bypass="never"), {}, solo_review.NO_BYPASS)):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                self.merge(gh, **pr)
            self.assertEqual(gh.ran, [])


class BridgeRechecks(unittest.TestCase):
    """The agent adapter re-reads the room's switch itself: a bypass or an admin merge only while @pm's solo
    review is on there (a stale or replayed task does nothing)."""

    def run_task(self, task, on):
        from types import SimpleNamespace
        from adapters.agents import bridge
        b = bridge.Bridge("cx", "codex")
        b.send, b.keep_typing = mock.AsyncMock(), mock.AsyncMock()
        b.client = SimpleNamespace(room_typing=mock.AsyncMock())
        handle = mock.AsyncMock(return_value={"kind": task["kind"], "line": "done", "task_id": "t1"})
        with mock.patch.object(bridge.room_state, "repo_of", mock.AsyncMock(return_value=REPO)), \
                mock.patch.object(bridge.room_state, "level", mock.AsyncMock(return_value="autonomous")), \
                mock.patch.object(bridge.solo_review, "enabled", mock.AsyncMock(return_value=on)), \
                mock.patch.object(bridge.tasks, "handle", handle):
            asyncio.run(b.run_task("!r", {"task_id": "t1", "repo": REPO, **task}))
        return handle, b.send.call_args_list[-1][1]["extra"]

    def test_refused_while_solo_review_is_off(self):
        for task in ({"kind": solo_review.TASK_KIND, "on": True}, {"kind": "merge", "pr": 9, "sha": "a", "admin": True}):
            handle, extra = self.run_task(task, on=False)
            handle.assert_not_awaited()
            self.assertIn("solo review is off", extra[github.RESULT_KEY]["error"])

    def test_run_while_on_and_taking_the_bypass_out_is_always_allowed(self):
        self.run_task({"kind": solo_review.TASK_KIND, "on": True}, on=True)[0].assert_awaited_once()
        self.run_task({"kind": solo_review.TASK_KIND, "on": False}, on=False)[0].assert_awaited_once()


class SoloVerdict(unittest.TestCase):
    def test_a_solo_review_is_a_comment_never_an_approval(self):
        ran = []

        async def run(args, agent=None, **kw):
            ran.append(args)
            return ""
        with mock.patch.object(tasks, "run", run):
            out = asyncio.run(tasks.submit_review("cx-codex-agent", 7, 9, "abc", "APPROVE", "fine", REPO, solo=True))
        self.assertIn("event=COMMENT", ran[0])
        self.assertTrue(any(a.startswith("body=Verdict: APPROVE (solo review") for a in ran[0]))
        self.assertEqual(out["verdict"], "approve")         # the verdict still goes to @pm
        self.assertEqual(len(ran), 1)


if __name__ == "__main__":
    unittest.main()
