"""Solo review on GitHub, as the agent runs it (adapters/agents/solo_github.py, tasks.submit_review; ADR 0023):
the review rule's bypass for repo admins is added without touching anything else, only Hagency's own entry is
taken out, a solo verdict is a comment, and an admin merge never skips a red check or a rule beyond the review.
Run: .venv/bin/python -m unittest tests.test_solo_github"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from adapters.agents import solo_github, tasks
from common import github, solo_review

REPO = "o/r"
OTHER = {"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"}
GREEN = [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}]


class FakeGitHub:
    def __init__(self, bypass=None, source="Repository", can_bypass="pull_requests_only", readable=True, extra_rules=()):
        self.rulesets = {i: {"id": i, "current_user_can_bypass": can_bypass} for i in (42, 43)}
        for rs in self.rulesets.values():
            if readable:
                rs["bypass_actors"] = list(bypass or [])
        self.rules = [{"type": "pull_request", "ruleset_id": 42, "ruleset_source_type": source},
                      {"type": "deletion", "ruleset_id": 42}, *extra_rules]
        self.puts, self.ran, self.pr, self.branch = [], [], {}, {"protected": False}

    async def run_json(self, args, agent=None):
        if args[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "main"}}
        if args[1] == f"repos/{REPO}/rules/branches/main?per_page=100":
            return self.rules
        if args[1] == f"repos/{REPO}/branches/main":
            return self.branch
        if args[1].startswith(f"repos/{REPO}/rulesets/"):
            return self.rulesets[int(args[1].rsplit("/", 1)[1])]
        if args[:2] == ["pr", "view"]:
            return self.pr
        raise AssertionError(args)

    async def run(self, args, agent=None, **kw):
        self.ran.append(args)
        if args[:3] == ["api", "-X", "PUT"]:
            with open(args[args.index("--input") + 1]) as f:
                self.puts.append((args[3], json.load(f)))
        return ""

    def patch(self):
        return mock.patch.multiple(solo_github, run_json=self.run_json, run=self.run)


class Ruleset(unittest.TestCase):
    def go(self, gh, task):
        with gh.patch():
            return asyncio.run(solo_github.ruleset("cx-agent", "codex", None, {"repo": REPO, **task}))

    def test_on_adds_the_admin_bypass_and_keeps_everything_else(self):
        gh = FakeGitHub(bypass=[OTHER])
        out = self.go(gh, {"on": True})
        self.assertEqual(out["changed"], [42])
        self.assertEqual(gh.puts, [(f"repos/{REPO}/rulesets/42", {"bypass_actors": [OTHER, solo_review.ADMIN_BYPASS]})])

    def test_an_equal_entry_already_there_is_done(self):
        gh = FakeGitHub(bypass=[solo_review.ADMIN_BYPASS])
        out = self.go(gh, {"on": True})
        self.assertEqual((out["changed"], out["already"], gh.puts), ([], [42], []))

    def test_off_takes_out_only_hagencys_entry_and_only_where_it_added_it(self):
        gh = FakeGitHub(bypass=[OTHER, solo_review.ADMIN_BYPASS])
        self.assertEqual(self.go(gh, {"on": False, "rulesets": []})["changed"], [])   # not ours: kept
        out = self.go(gh, {"on": False, "rulesets": [43]})       # recorded, though no longer on the branch
        self.assertEqual((out["changed"], gh.puts), ([43], [(f"repos/{REPO}/rulesets/43", {"bypass_actors": [OTHER]})]))

    def test_not_an_admin_or_an_org_rule_says_so(self):
        with self.assertRaisesRegex(ValueError, "must be the repo's admin"):
            self.go(FakeGitHub(readable=False), {"on": True})
        with self.assertRaisesRegex(ValueError, "organization owner"):
            self.go(FakeGitHub(source="Organization"), {"on": True})

    def test_a_refused_write_says_what_is_wrong(self):
        gh = FakeGitHub()

        async def refused(args, agent=None, **kw):
            raise solo_github.GitHubError("HTTP 403: Resource not accessible")
        with mock.patch.multiple(solo_github, run_json=gh.run_json, run=refused):
            with self.assertRaisesRegex(ValueError, "repo's admin.*403"):
                asyncio.run(solo_github.ruleset("cx-agent", "codex", None, {"repo": REPO, "on": True}))

    def test_no_bypass_on_a_ruleset_that_holds_more_than_the_review(self):
        """GPT review: a bypass covers the whole ruleset, so one with a merge queue or required threads is left alone."""
        for rules in ([{"type": "pull_request"}, {"type": "merge_queue"}],
                      [{"type": "pull_request"}, {"type": "required_status_checks"}],
                      [{"type": "pull_request", "parameters": {"required_review_thread_resolution": True}}]):
            gh = FakeGitHub()
            gh.rulesets[42]["rules"] = rules
            with self.subTest(rules=rules), self.assertRaisesRegex(ValueError, "ruleset of its own"):
                self.go(gh, {"on": True})
            self.assertEqual(gh.puts, [])

    def test_a_failure_part_way_still_reports_what_changed(self):
        gh = FakeGitHub(bypass=[solo_review.ADMIN_BYPASS])
        del gh.rulesets[43]["bypass_actors"]                    # 43 isn't readable: after 42 was changed
        out = self.go(gh, {"on": False, "rulesets": [42, 43]})
        self.assertEqual(out["changed"], [42])
        self.assertIn("repo's admin", out["error"])


class AdminMerge(unittest.TestCase):
    PR = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "headRefOid": "abc",
          "baseRefName": "main", "reviewDecision": "REVIEW_REQUIRED", "statusCheckRollup": GREEN}

    def merge(self, gh, **pr):
        gh.pr = {**self.PR, **pr}
        with gh.patch():
            return asyncio.run(tasks.merge("cx-agent", "codex", None, {"repo": REPO, "issue": 7, "pr": 9, "sha": "abc",
                                                                       "admin": True}))

    def test_merges_past_the_rule_as_admin_at_the_reviewed_commit(self):
        gh = FakeGitHub()
        self.merge(gh)
        self.assertEqual(gh.ran[-1], ["pr", "merge", "9", "-R", REPO, "--squash", "--admin", "--match-head-commit", "abc"])

    def test_never_past_a_red_check_a_moved_pr_or_a_rule_beyond_the_review(self):
        needs_ci = {"type": "required_status_checks", "ruleset_id": 42,
                    "parameters": {"required_status_checks": [{"context": "deploy"}]}}
        for gh, pr, why in ((FakeGitHub(), {"statusCheckRollup": [{"conclusion": "FAILURE"}]}, "check"),
                            (FakeGitHub(), {"headRefOid": "zzz"}, "reviewed commit"),
                            (FakeGitHub(), {"baseRefName": "other"}, "reviewed commit"),
                            (FakeGitHub(), {"mergeStateStatus": "BEHIND"}, "reviewed commit"),
                            (FakeGitHub(), {"reviewDecision": "CHANGES_REQUESTED"}, "changes requested"),
                            (FakeGitHub(extra_rules=[{"type": "merge_queue", "ruleset_id": 42}]), {}, "merge_queue"),
                            (FakeGitHub(extra_rules=[needs_ci]), {}, "require hasn't passed"),
                            (FakeGitHub(extra_rules=[{"type": "pull_request", "parameters": {
                                "allowed_merge_methods": ["merge"]}}]), {}, "other than squash"),
                            (FakeGitHub(extra_rules=[{"type": "required_status_checks", "parameters": {
                                "required_status_checks": [{"context": "ci", "integration_id": 7}]}}]), {}, "one particular app"),
                            (FakeGitHub(can_bypass="never"), {}, solo_review.NO_BYPASS)):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                self.merge(gh, **pr)
            self.assertEqual(gh.ran, [])

    def test_a_check_classic_protection_requires_must_be_there(self):
        gh = FakeGitHub()
        gh.branch = {"protected": True, "protection": {"required_status_checks": {"contexts": ["deploy"]}}}
        with self.assertRaisesRegex(ValueError, "require hasn't passed"):
            self.merge(gh)
        gh.branch["protection"]["required_status_checks"] = {"checks": [{"context": "ci", "app_id": 15368}]}
        with self.assertRaisesRegex(ValueError, "one particular app"):
            self.merge(gh)

    def test_switched_off_while_the_checks_ran_it_doesnt_merge(self):
        gh = FakeGitHub()
        gh.pr = dict(self.PR)
        with gh.patch():
            with self.assertRaisesRegex(ValueError, "switched off"):
                asyncio.run(tasks.merge("cx-agent", "codex", None, {"repo": REPO, "issue": 7, "pr": 9, "sha": "abc",
                                                                    "admin": True, "_still": mock.AsyncMock(return_value=False)}))
        self.assertEqual(gh.ran, [])

    def test_unfinished_checks_are_waited_for(self):
        gh = FakeGitHub()
        states = iter([[{"status": "IN_PROGRESS"}], GREEN])
        pr = dict(self.PR)

        async def run_json(args, agent=None):
            if args[:2] == ["pr", "view"]:
                return {**pr, "statusCheckRollup": next(states)}
            return await FakeGitHub.run_json(gh, args, agent)
        with mock.patch.multiple(solo_github, run_json=run_json, run=gh.run), \
                mock.patch.object(solo_github.asyncio, "sleep", mock.AsyncMock()):
            asyncio.run(solo_github.admin_merge("cx-agent", REPO, 7, 9, "abc"))
        self.assertIn("--admin", gh.ran[-1])


class BridgeRechecks(unittest.TestCase):
    """The agent adapter re-reads the room's switch itself: a bypass or an admin merge only while @pm's solo
    review is on there (a stale or replayed task does nothing)."""

    def run_task(self, task, on):
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

    def test_an_add_and_a_removal_are_different_steps(self):
        from adapters.agents import bridge
        on, off = ({"kind": solo_review.TASK_KIND, "task_id": t, "repo": REPO} for t in ("a", "b"))
        self.assertNotEqual(bridge.step_key(on), bridge.step_key(off))   # GPT review: one never drops the other


class SoloVerdict(unittest.TestCase):
    def submit(self, *args, **kw):
        ran = []

        async def run(a, agent=None, **k):
            ran.append(a)
            return ""
        with mock.patch.object(tasks, "run", run):
            return asyncio.run(tasks.submit_review(*args, **kw)), ran

    def test_a_solo_review_is_a_comment_never_an_approval(self):
        out, ran = self.submit("cx-codex-agent", 7, 9, "abc", "APPROVE", "fine", REPO, solo=True)
        self.assertIn("event=COMMENT", ran[0])
        self.assertTrue(any(a.startswith("body=Verdict: APPROVE (solo review") for a in ran[0]))
        self.assertEqual((out["verdict"], len(ran)), ("approve", 1))   # the verdict still goes to @pm

    def test_an_oversized_solo_diff_is_a_comment_too(self):
        ran = []

        async def run(a, agent=None, **k):
            ran.append(a)
            return ""
        material = mock.AsyncMock(return_value=(7, 9, {}, "abc", "x" * (tasks.MAX_DIFF_CHARS + 1)))
        with mock.patch.object(tasks, "run", run), mock.patch.object(tasks, "review_material", material):
            asyncio.run(tasks.review("cx-codex-agent", "codex", None, {"repo": REPO, "solo": True}))
        self.assertIn("event=COMMENT", ran[0])


if __name__ == "__main__":
    unittest.main()
