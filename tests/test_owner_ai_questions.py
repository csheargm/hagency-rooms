"""GPT re-check of ADR 0022, as user flows (R6): a question never changes the mode; a question an unnamed
message led to never takes a bare "yes"; an inferred "show the plan" only shows it; and a question posted
while a stop (or another question) landed is never installed. Room: tests/owner_ai_room.py.
Run: .venv/bin/python -m unittest tests.test_owner_ai_questions"""
import asyncio
import time
import unittest
from unittest import mock

from common import intents
from server import pm_chat
from server import pm_implicit
from server import pm_plan
from server import pm_understand
from tests.owner_ai_room import ANNA, BETTY, RoomBase

PLAN = {"items": [{"title": "Hello page", "criteria": ["says hello"]}], "agent": "@anna-agent:bridge.localhost",
        "by": ANNA}


class QuestionsNeverSwitch(RoomBase, unittest.TestCase):
    def test_a_maintainers_question_about_autonomous_mode_only_asks(self):
        self.tell(ANNA, "@pm What does autonomous mode do?")
        self.assertEqual(self.levels, [])
        self.assertIn("switch to **autonomous** (agents build, review and merge on their own)", self.said[-1])
        self.assertEqual(self.asked, [])              # a phrase the rules know: no model either
        self.react(ANNA)
        self.assertEqual(self.levels, ["autonomous"])

    def test_a_contributors_question_is_answered(self):
        self.tell(BETTY, "@pm can you take it all the way")
        self.assertEqual(self.levels, [])
        self.assertIn(pm_chat.LEVEL_WORDS["autonomous"], self.said[-1])
        self.assertIn(pm_chat.ONLY_MODERATORS, self.said[-1])

    def test_a_plain_command_still_switches_at_once(self):
        """As on origin/master: an explicit phrase from the owner or a maintainer needs no extra 👍."""
        self.tell(ANNA, "@pm take it all the way")
        self.assertEqual(self.levels, ["autonomous"])

    def test_questions_are_told_from_commands(self):
        for text in ("what does assist do", "Is autonomous safe?", "how do we go all the way"):
            self.assertTrue(intents.is_question(text), text)
        for text in ("take it all the way", "let us approve each change", "go all in!"):
            self.assertFalse(intents.is_question(text), text)


class UnnamedQuestionsNeedMore(RoomBase, unittest.TestCase):
    def test_a_setting_asked_without_at_pm_takes_no_bare_yes(self):
        self.lp.set_follow = mock.AsyncMock()
        self.model = {"action": "follow_always", "reply": ""}
        self.tell(ANNA, "can you bring my agent every time?", mention=False)
        self.assertEqual(self.lp.pending["!r"]["kind"], "setting")
        self.tell(ANNA, "yes", mention=False)
        self.lp.set_follow.assert_not_awaited()
        self.assertEqual(self.said[-1], pm_implicit.HOW_TO_CONFIRM)
        self.tell(ANNA, "@pm yes")
        self.lp.set_follow.assert_awaited_once()

    def test_a_setting_asked_with_at_pm_still_takes_a_yes(self):
        self.lp.set_follow = mock.AsyncMock()
        self.tell(ANNA, "@pm bring my agent if you can, maybe")      # not plainly said: asked
        self.tell(ANNA, "yes", mention=False)
        self.lp.set_follow.assert_awaited_once()


class InferredShowPlan(RoomBase, unittest.TestCase):
    def test_an_inferred_show_plan_installs_nothing(self):
        self.lp.last_plans["!r"] = PLAN
        waiting = {"kind": "mode", "level": "autonomous", "event_id": "$q", "by": BETTY, "until": time.time() + 300}
        self.lp.pending["!r"] = waiting
        self.model = {"action": "show_plan", "reply": ""}
        self.tell(ANNA, "can you show what we planned?", mention=False)
        self.assertIs(self.lp.pending["!r"], waiting)
        self.assertIn("Hello page", self.said[-1])
        self.assertTrue(self.said[-1].endswith(pm_plan.PLAN_TO_APPROVE))

    def test_a_named_show_plan_still_asks_for_approval(self):
        self.lp.last_plans["!r"] = PLAN
        self.tell(ANNA, "@pm show the plan")
        self.assertEqual(self.lp.pending["!r"]["kind"], "plan")

    def test_a_stop_while_the_plan_is_posted_installs_no_approval(self):
        self.lp.last_plans["!r"] = PLAN
        say = self.lp.say

        async def stop_meanwhile(room_id, body, *a, **k):
            if "Click 👍 below" in body:
                self.lp.invalidate("!r")
            return await say(room_id, body, *a, **k)
        self.lp.say = stop_meanwhile
        asyncio.run(self.lp.show_plan("!r"))
        self.assertEqual(self.lp.pending, {})

    def test_a_contributors_plan_request_never_replaces_a_waiting_plan(self):
        plan = {"kind": "plan", "event_id": "$plan", "by": ANNA, "until": time.time() + 300}
        self.lp.pending["!r"] = plan
        self.tell(BETTY, "@pm plan this")
        self.assertIs(self.lp.pending["!r"], plan)
        self.assertEqual(self.said[-1], pm_understand.ANSWER_FIRST)


if __name__ == "__main__":
    unittest.main()
