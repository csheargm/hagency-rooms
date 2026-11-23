"""Unit tests: turning a discussion into issues. Run: .venv/bin/python -m unittest tests.test_planning"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetStateResponse

from common import github
from common import room_state
from adapters.agents import planning
from server.loop import Loop

ANNA, BETTY = "@anna:bridge.localhost", "@betty:bridge.localhost"
ANNA_AGENT, BETTY_AGENT = "@anna-agent:bridge.localhost", "@betty-agent:bridge.localhost"
GOOD = '[{"title": "Add index.html", "criteria": ["index.html exists", "shows Hello, World!"]}]'


class Parse(unittest.TestCase):
    def test_json_inside_prose(self):
        items = planning.parse_items(f"Sure! Here is the plan:\n{GOOD}\nGood luck.")
        self.assertEqual(items, [{"title": "Add index.html", "criteria": ["index.html exists", "shows Hello, World!"]}])

    def test_malformed_is_refused_not_guessed(self):
        for bad in ("no json here", "[{oops}]", '[{"title": "x"}]', '[{"title": "", "criteria": ["a"]}]',
                    '[{"title": "x", "criteria": "not a list"}]', "[" + ",".join([GOOD[1:-1]] * 13) + "]"):
            with self.assertRaises(planning.PlanError, msg=bad):
                planning.parse_items(bad)

    def test_text_is_flattened_and_capped(self):
        item = planning.clean_item({"title": "A\n## heading\n" + "x" * 500, "criteria": ["line1\nline2"]})
        self.assertNotIn("\n", item["title"])
        self.assertLessEqual(len(item["title"]), planning.MAX_TITLE)
        self.assertEqual(item["criteria"], ["line1 line2"])

    def test_empty_plan_is_fine(self):
        self.assertEqual(planning.parse_items("[]"), [])

    def test_plan_renders_as_separate_issues(self):
        """Seen live: 3 issues with criteria rendered as one flat 13-item numbered list."""
        import markdown
        items = [{"title": "A", "criteria": ["a1", "a2"]}, {"title": "B", "criteria": ["b1"]}]
        html = markdown.markdown(planning.show(items))
        self.assertEqual(html.count("<strong>"), 2)
        self.assertNotIn("<ol>", html)
        self.assertEqual(html.count("<li>"), 3)

    def test_issue_body_is_what_the_loop_builds_from(self):
        body = planning.issue_body({"title": "t", "criteria": ["a", "b"]}, "anna")
        self.assertTrue(body.startswith("Acceptance criteria:\n- a\n- b"))


class Flow(unittest.TestCase):
    def setUp(self):
        for name, value in (("bound", True), ("repo_of", "your-name/hagency-hello")):   # a project room
            _p = mock.patch.object(room_state, name, mock.AsyncMock(return_value=value))
            _p.start()
            self.addCleanup(_p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos = {}
        self.lp.user_id, self.lp.pending, self.lp.plans, self.lp.boards = "@pm:bridge.localhost", {}, {}, {}
        self.lp.last_plans = {}
        self.sent = []

        async def say(room_id, body, mention=None, task=None):
            self.sent.append((body, mention, task))
            return f"$m{len(self.sent)}"
        self.lp.say = say
        self.lp.level = mock.AsyncMock(return_value="off")
        self.lp.discussion = lambda room_id, include_self=False: "anna: a hello page\nbetty: with a name box"
        create = RoomGetStateResponse([{"type": "m.room.create", "sender": ANNA, "content": {"room_version": "12"}}], "!r")
        self.lp.client = SimpleNamespace(room_get_state=mock.AsyncMock(return_value=create))
        self.room = SimpleNamespace(room_id="!r", users={ANNA: 0, BETTY: 0, ANNA_AGENT: 0, BETTY_AGENT: 0},
                                    invited_users={}, power_levels=SimpleNamespace(get_user_level=lambda u: 0))
        p = mock.patch.object(github, "run_json", mock.AsyncMock(return_value=[{"title": "already planned"}]))
        p.start()
        self.addCleanup(p.stop)

    def run_(self, coro):
        return asyncio.run(coro)

    def test_whole_flow(self):
        self.run_(self.lp.start_plan(self.room, ANNA))
        _, to, task = self.sent[-1]
        self.assertEqual((to, task["kind"]), (ANNA_AGENT, "plan"))          # the requester's own agent
        self.assertIn("with a name box", task["discussion"])
        self.assertEqual(task["existing"], ["already planned"])
        items = [{"title": "Add index.html", "criteria": ["exists"]}]
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": task["task_id"], "items": items}))
        self.assertIn("**Issue 1: Add index.html**", self.sent[-1][0])
        self.assertIn("to open these 1 issue(s)", self.sent[-1][0])
        self.assertTrue(self.run_(self.lp.confirm(self.room, ANNA)))
        _, to, filing = self.sent[-1]
        self.assertEqual((to, filing["kind"], filing["items"], filing["approved_by"]),
                         (ANNA_AGENT, "file_issues", items, "anna"))
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "file_issues", "task_id": filing["task_id"],
                                                                 "numbers": [5]}))
        self.assertIn("Opened #5", self.sent[-1][0])
        self.assertIn("take it all the way", self.sent[-1][0])              # level off: tells them how to start

    def test_a_failed_plan_says_what_to_do_in_plain_words(self):
        """Live test 2026-11-23: a 3B local model's plan failed with "⚠️ plan didn't work: the plan wasn't
        valid JSON: Expecting ',' delimiter…", and the agent's own line said "plan failed for #None"."""
        self.run_(self.lp.start_plan(self.room, ANNA))
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": self.sent[-1][2]["task_id"],
                                                                 "error": "the plan wasn't valid JSON"}))
        said = self.sent[-1][0]
        self.assertIn("couldn't turn the discussion into a plan", said)
        self.assertIn("let's build it", said)
        from adapters.agents import tasks
        self.assertEqual(tasks.issue_part({"kind": "plan"}), "")
        self.assertEqual(tasks.issue_part({"kind": "build", "issue": 12}), " for #12")

    def test_an_empty_revision_keeps_the_previous_plan(self):
        """Seen live: a re-plan came back empty (the agent remembered its earlier draft) and the
        3-issue plan waiting for ✅ was lost."""
        items = [{"title": "Add index.html", "criteria": ["exists"]}]
        self.run_(self.lp.start_plan(self.room, ANNA))
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": self.sent[-1][2]["task_id"],
                                                                 "items": items}))
        self.lp.pending.clear()                                    # a revision was asked for
        self.run_(self.lp.start_plan(self.room, ANNA))
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": self.sent[-1][2]["task_id"],
                                                                 "items": []}))
        self.assertIn("plan stays as it was", self.sent[-2][0])
        self.assertIn("**Issue 1: Add index.html**", self.sent[-1][0])
        self.assertEqual(self.lp.pending["!r"]["items"], items)     # still ✅-able

    def test_show_the_plan_again(self):
        self.lp.last_plans["!r"] = {"items": [{"title": "X", "criteria": ["y"]}], "agent": ANNA_AGENT, "by": ANNA}
        self.run_(self.lp.show_plan("!r"))
        self.assertIn("**Issue 1: X**", self.sent[-1][0])
        self.assertEqual(self.lp.pending["!r"]["kind"], "plan")

    def test_plan_prompt_ignores_unapproved_drafts(self):
        prompt = planning.plan_prompt("anna: hi", ["Existing thing"])
        self.assertIn("Ignore any plan you drafted earlier", prompt)
        self.assertIn("- Existing thing", prompt)

    def test_needs_the_requesters_own_agent(self):
        self.room.users.pop(ANNA_AGENT)
        self.run_(self.lp.start_plan(self.room, ANNA))
        self.assertIn("anna-agent isn't in this room", self.sent[-1][0])

    def test_only_the_asked_agent_and_task_count(self):
        self.run_(self.lp.start_plan(self.room, ANNA))
        task_id = self.sent[-1][2]["task_id"]
        items = [{"title": "x", "criteria": ["y"]}]
        self.run_(self.lp.on_plan_result(self.room, BETTY_AGENT, {"kind": "plan", "task_id": task_id, "items": items}))
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": "forged", "items": items}))
        self.assertEqual(self.lp.pending, {})

    def test_a_malformed_plan_from_the_agent_is_refused(self):
        self.run_(self.lp.start_plan(self.room, ANNA))
        task_id = self.sent[-1][2]["task_id"]
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": task_id, "items": "rm -rf"}))
        self.assertIn("malformed", self.sent[-1][0])
        self.assertEqual(self.lp.pending, {})

    def test_someone_elses_yes_doesnt_file_issues(self):
        self.run_(self.lp.start_plan(self.room, ANNA))
        task_id = self.sent[-1][2]["task_id"]
        self.run_(self.lp.on_plan_result(self.room, ANNA_AGENT, {"kind": "plan", "task_id": task_id,
                                                                 "items": [{"title": "x", "criteria": ["y"]}]}))
        self.assertFalse(self.run_(self.lp.confirm(self.room, BETTY)))


if __name__ == "__main__":
    unittest.main()
