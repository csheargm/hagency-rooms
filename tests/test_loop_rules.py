"""Unit tests for the loop's trust rules. Run: .venv/bin/python -m unittest tests.test_loop_rules"""
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import github
from adapters.agents import tasks
from adapters.agents.bridge import Bridge

ANNA, BETTY = "@anna:bridge.localhost", "@betty:bridge.localhost"
AGENT, BOT = "@anna-agent:bridge.localhost", f"@{github.PM_BOT}:bridge.localhost"


def room(members, invited=()):
    users = {m: None for m in [*members, *invited]}
    return SimpleNamespace(room_id="!r", users=users, invited_users={m: None for m in invited})


class BridgeTaskTrust(unittest.TestCase):
    def setUp(self):
        self.b = Bridge("anna", "codex")
        self.b.run_task = mock.Mock(return_value=None)
        self.started = []
        patcher = mock.patch("asyncio.create_task", side_effect=lambda c: self.started.append(c) or mock.Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def task_from(self, sender, task, r=None):
        ev = SimpleNamespace(sender=sender, event_id=f"$e{len(self.started)}{sender}{task.get('agent')}")
        self.b.on_task(r or room([ANNA, AGENT, BOT]), ev, task)

    def test_loop_bot_task_for_me_runs(self):
        self.task_from(BOT, {"kind": "build", "agent": AGENT})
        self.assertEqual(len(self.started), 1)

    def test_no_work_while_my_owner_is_away(self):
        """GPT review 7: an agent must never work in a room without its owner."""
        self.task_from(BOT, {"kind": "build", "agent": AGENT}, room([AGENT, BOT, BETTY]))
        self.assertEqual(self.started, [])

    def test_only_my_owner_or_pm_can_bring_me_into_a_room(self):
        joined = []

        async def join(room_id):
            joined.append(room_id)
        self.b.client.join = join
        for sender in (BETTY, ANNA, BOT):
            ev = SimpleNamespace(state_key=AGENT, membership="invite", sender=sender)
            asyncio.run(self.b.on_invite(SimpleNamespace(room_id=f"!{sender}"), ev))
        self.assertEqual(joined, [f"!{ANNA}", f"!{BOT}"])

    def test_redelivered_task_runs_once(self):
        ev = SimpleNamespace(sender=BOT, event_id="$same")
        for _ in range(2):
            self.b.on_task(room([ANNA, AGENT, BOT]), ev, {"kind": "build", "agent": AGENT})
        self.assertEqual(len(self.started), 1)

    def test_resend_of_a_running_step_is_not_run_twice(self):
        self.task_from(BOT, {"kind": "build", "agent": AGENT, "issue": 7})
        self.task_from(BOT, {"kind": "build", "agent": AGENT, "issue": 7})   # new event id, same step
        self.assertEqual(len(self.started), 1)

    def test_task_from_a_human_is_ignored(self):
        self.task_from(ANNA, {"kind": "build", "agent": AGENT})
        self.assertEqual(self.started, [])

    def test_task_for_another_agent_is_ignored(self):
        self.task_from(BOT, {"kind": "build", "agent": "@betty-agent:bridge.localhost"})
        self.assertEqual(self.started, [])

    def test_task_from_invited_not_joined_bot_is_ignored(self):
        self.task_from(BOT, {"kind": "build", "agent": AGENT}, room([ANNA, AGENT], invited=[BOT]))
        self.assertEqual(self.started, [])


class TaskRules(unittest.TestCase):
    def test_wrong_repo_refused(self):
        r = asyncio.run(tasks.handle("anna-agent", "codex", Path("/tmp"), {"kind": "merge", "repo": "evil/../x",
                                                                          "issue": 1, "pr": 2}))
        self.assertIn("no valid repo", r["error"])

    def test_unknown_kind_refused(self):
        r = asyncio.run(tasks.handle("anna-agent", "codex", Path("/tmp"), {"kind": "rm", "repo": github.REPO}))
        self.assertIn("unknown task kind", r["error"])

    def test_no_identity_means_no_github_writes(self):
        with mock.patch.object(github, "IDENTITY_ROOT", Path("/nonexistent")):
            with self.assertRaises(github.GitHubError):
                asyncio.run(github.run(["api", "user"], "anna-agent"))

    def test_verdict_must_be_exact_last_line(self):
        self.assertEqual(tasks.parse_verdict("looks fine\nVERDICT: APPROVE\n"), "APPROVE")
        self.assertEqual(tasks.parse_verdict("VERDICT: APPROVE\nbut actually no"), "REQUEST_CHANGES")
        self.assertEqual(tasks.parse_verdict("I would say VERDICT: APPROVE"), "REQUEST_CHANGES")
        self.assertEqual(tasks.parse_verdict(""), "REQUEST_CHANGES")

    def test_git_runs_without_user_config_or_hooks(self):
        seen = {}

        async def fake_run(args, agent=None, cwd=None, program=None, extra_env=None):
            seen.update(args=args, env=extra_env)
            return ""
        with mock.patch.object(github, "run", fake_run):
            asyncio.run(github.git(["status"], "anna-agent", Path("/tmp")))
        self.assertEqual(seen["env"]["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertIn("core.fsmonitor=false", seen["args"])
        self.assertTrue(any(a.startswith("core.hooksPath=") for a in seen["args"]))

    def test_ambient_token_overrides_scrubbed(self):
        self.assertTrue({"GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST"} <= set(github.SCRUBBED_ENV))

    def test_merge_ready_needs_github_approval(self):
        async def fake(args, agent=None):
            return {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": ""}
        with mock.patch.object(github, "run_json", fake):
            ok, _ = asyncio.run(github.merge_ready(5))
        self.assertFalse(ok, "no required-review protection → never merge-ready")


class LoopResults(unittest.TestCase):
    def setUp(self):
        owners = mock.patch("server.ownership.owner_id", lambda a: a.replace("-agent", ""))   # each agent: its own person
        owners.start()
        self.addCleanup(owners.stop)
        from server import loop
        self.lp = loop.Loop.__new__(loop.Loop)
        self.lp.room_repos = {"!r": "your-name/hagency-hello"}
        self.lp.level = mock.AsyncMock(return_value="autonomous")   # dispatch checks the mode
        self.lp.state = {"7": {"room": "!r", "repo": "your-name/hagency-hello", "stage": "reviewing", "builder": "@b-agent:x",
                               "reviewer": "@r-agent:x", "pr": 9, "task_id": "T"}}
        self.lp.save = lambda: None
        self.lp.say = mock.AsyncMock()

    def deliver(self, sender, result):
        asyncio.run(self.lp.on_result(SimpleNamespace(room_id="!r"), sender, result))
        return self.lp.state["7"]["stage"]

    def approval(self, **kw):
        return {"kind": "review", "issue": 7, "pr": 9, "sha": "abc", "verdict": "approve", "task_id": "T", **kw}

    def test_reviewer_approval_advances(self):
        self.assertEqual(self.deliver("@r-agent:x", self.approval()), "approved")
        self.assertEqual(self.lp.state["7"]["sha"], "abc")

    def test_approval_from_builder_is_ignored(self):
        self.assertEqual(self.deliver("@b-agent:x", self.approval()), "reviewing")

    def test_approval_for_old_task_id_is_ignored(self):
        self.assertEqual(self.deliver("@r-agent:x", self.approval(task_id="OLD")), "reviewing")

    def test_approval_for_another_pr_is_ignored(self):
        self.assertEqual(self.deliver("@r-agent:x", self.approval(pr=10)), "reviewing")

    def test_build_result_must_be_builders_own_branch(self):
        self.lp.state["7"].update(stage="building")
        async def fake(args, agent=None):
            return {"headRefName": "agent/x-agent/issue-7", "isCrossRepository": False, "state": "OPEN"}
        with mock.patch.object(github, "run_json", fake):
            stage = self.deliver("@b-agent:x", {"kind": "build", "issue": 7, "pr": 9, "task_id": "T"})
        self.assertEqual(stage, "stopped")

    def test_late_build_result_is_ignored(self):
        self.assertEqual(self.deliver("@b-agent:x", {"kind": "build", "issue": 7, "error": "x"}), "reviewing")

    def test_stale_events_dropped(self):
        from common.lifecycle import is_stale
        self.assertTrue(is_stale(SimpleNamespace(server_timestamp=1_000), 100_000))
        self.assertFalse(is_stale(SimpleNamespace(server_timestamp=99_000), 100_000))


class AlreadyDone(unittest.TestCase):
    """An issue the builder found nothing to change for: a second agent must confirm before closing."""
    def setUp(self):
        from server import loop
        self.lp = loop.Loop.__new__(loop.Loop)
        self.lp.room_repos = {"!r": "your-name/hagency-hello"}
        self.lp.level = mock.AsyncMock(return_value="autonomous")   # dispatch checks the mode
        self.lp.state = {"7": {"room": "!r", "repo": "your-name/hagency-hello", "stage": "building", "builder": "@b-agent:x", "task_id": "T"}}
        self.lp.save = lambda: None
        self.lp.say = mock.AsyncMock()
        self.lp.agents_in = lambda room: ["@b-agent:x", "@r-agent:x"]
        self.item = self.lp.state["7"]
        owners = mock.patch("server.ownership.owner_id", lambda a: a.replace("-agent", ""))   # each agent: its own person
        owners.start()
        self.addCleanup(owners.stop)
        self.lp._seen = {}                            # nobody known offline

    def deliver(self, sender, result):
        asyncio.run(self.lp.on_result(SimpleNamespace(room_id="!r"), sender, {"issue": 7, **result}))
        return self.item["stage"]

    def test_no_change_build_asks_the_other_agent_to_verify(self):
        self.assertEqual(self.deliver("@b-agent:x", {"kind": "build", "already_done": True, "task_id": "T"}), "verifying")
        self.assertEqual(self.item["reviewer"], "@r-agent:x")
        _, _, agent, task = self.lp.say.call_args.args
        self.assertEqual((task["kind"], agent), ("verify", "@r-agent:x"))

    def verifying(self):
        self.item.update(stage="verifying", reviewer="@r-agent:x")

    def test_builder_cannot_confirm_its_own_no_change(self):
        self.verifying()
        self.assertEqual(self.deliver("@b-agent:x", {"kind": "verify", "verdict": "approve", "task_id": "T"}), "verifying")

    def test_reviewer_confirms(self):
        self.verifying()
        self.assertEqual(self.deliver("@r-agent:x", {"kind": "verify", "verdict": "approve", "task_id": "T"}), "confirmed_done")

    def test_reviewer_disagrees_stops(self):
        self.verifying()
        self.assertEqual(self.deliver("@r-agent:x", {"kind": "verify", "verdict": "request_changes", "task_id": "T"}), "stopped")

    def advance(self, lvl, gh_state="OPEN", stage="confirmed_done"):
        self.item.update(stage=stage, reviewer="@r-agent:x")
        async def fake(args, agent=None):
            return {"state": gh_state}
        with mock.patch.object(github, "run_json", fake):
            asyncio.run(self.lp.advance("!r", "7", self.item, lvl))
        return self.item["stage"]

    def test_autonomous_sends_close_to_builder(self):
        self.assertEqual(self.advance("autonomous"), "closing")
        _, _, agent, task = self.lp.say.call_args.args
        self.assertEqual((task["kind"], agent, task["verifier"]), ("close", "@b-agent:x", "@r-agent:x"))

    def test_assist_asks_a_person(self):
        self.assertEqual(self.advance("assist"), "close_ready")
        self.assertIn("a person closes", self.lp.say.call_args.args[1])

    def test_done_only_when_github_says_closed(self):
        self.assertEqual(self.advance("autonomous", stage="close_sent"), "close_sent")
        self.assertEqual(self.advance("autonomous", "CLOSED", stage="close_sent"), "done")

    def test_bridges_recheck_level_before_close(self):
        self.assertIn("close", tasks.AUTONOMOUS_ONLY)


class Links(unittest.TestCase):
    def test_pr_and_issue_refs_link_to_the_repo(self):
        self.assertEqual(github.linkify("merged PR #153"), "merged PR #153")   # no repo known: no links
        md = github.linkify("merged PR #153 (closes #113)", github.REPO)
        self.assertIn(f"[PR #153](https://github.com/{github.REPO}/pull/153)", md)
        self.assertIn(f"[#113](https://github.com/{github.REPO}/issues/113)", md)

    def test_urls_and_words_untouched(self):
        for text in ("https://x/pull/5#12", "abc#12", "matrix.to/#/@a:b"):
            self.assertEqual(github.linkify(text), text)


if __name__ == "__main__":
    unittest.main()
