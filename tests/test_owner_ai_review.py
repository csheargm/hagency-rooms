"""Review findings on ADR 0022, as user flows (R6) in the simulated room of tests/owner_ai_room.py.
Run: .venv/bin/python -m unittest tests.test_owner_ai_review"""
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetEventResponse

from server import pm_brain
from server import pm_chat
from server import pm_implicit
from server import pm_owner_brain
from server import project_rooms
from server.pm_cards import HERMES, STOPPED
from tests.owner_ai_room import ANNA, BETTY, PM, RoomBase


class Review(RoomBase, unittest.TestCase):
    def replies_are_to_pm(self):
        mine = RoomGetEventResponse()
        mine.event = SimpleNamespace(sender=PM, body="Mode: assist.")
        self.lp.client.room_get_event = mock.AsyncMock(return_value=mine)

    def wait_for(self, pending: dict) -> None:
        self.lp.pending["!r"] = {**pending, "until": time.time() + 300}

    # --- every ask of the owner's agent is capped, named or not ---------------------------
    def test_rapid_named_messages_cant_run_up_the_owners_bill(self):
        cap = pm_owner_brain.SENDER_CAPS[0][0]
        for _ in range(cap + 4):
            self.tell(BETTY, "@pm hi")
        self.assertEqual(len(self.asked), cap)
        self.assertTrue(self.said[-1].startswith(pm_owner_brain.BUSY + "I didn't catch that"))
        self.tell(BETTY, "@pm stop")                  # never reaches a model: still instant
        self.assertEqual(self.levels, ["off"])

    def test_the_room_has_its_own_cap_across_people(self):
        with mock.patch.object(pm_owner_brain, "ROOM_CAPS", ((3, 60),)):
            for who in (ANNA, BETTY, ANNA, BETTY, ANNA):
                self.tell(who, "@pm hi")
        self.assertEqual(len(self.asked), 3)

    # --- a waiting plan is never replaced by an unaddressed message -----------------------
    def test_an_unaddressed_stop_keeps_the_plan_question(self):
        self.lp.agent_online = lambda agent: False
        self.wait_for({"kind": "plan", "event_id": "$plan", "by": ANNA})
        self.tell(ANNA, "let's stop here and grab lunch", mention=False)
        self.assertEqual((self.lp.pending["!r"]["event_id"], self.said), ("$plan", []))

    def test_an_unaddressed_message_never_replaces_a_plan_request(self):
        self.wait_for({"kind": "plan_request", "event_id": "$ask"})
        self.model = {"action": "stop", "reply": ""}
        self.tell(BETTY, "can we stop all the building for today?", mention=False)
        self.assertEqual(self.lp.pending["!r"]["kind"], "plan_request")
        self.assertEqual(self.levels, [])

    def test_a_reply_to_the_plan_without_at_pm_revises_it(self):
        self.room.power_levels = SimpleNamespace(get_user_level=lambda u: 100 if u == ANNA else 0)
        self.lp.start_plan = mock.AsyncMock()
        self.replies_are_to_pm()
        self.wait_for({"kind": "plan", "event_id": "$plan", "by": ANNA})
        self.tell(ANNA, "drop the French page from it", mention=False, reply_to="$plan")
        self.lp.start_plan.assert_awaited_once_with(self.room, ANNA)
        self.assertEqual(self.asked, [])

    # --- the front doors: only "@pm" counts ---------------------------------------------
    def test_the_lobby_ignores_unaddressed_messages(self):
        with mock.patch.object(project_rooms, "FRONT_DOOR_IDS", {"!r"}):
            self.tell(BETTY, "@pm help")
            said = len(self.said)
            self.tell(BETTY, "we should stop for today", mention=False)
            self.tell(BETTY, "where are we?", mention=False)
        self.assertEqual((len(self.said), len(self.asked), self.levels), (said, 1, []))

    # --- never silence for someone plainly talking to @pm ---------------------------------
    def test_a_bare_yes_from_the_person_asked_is_told_how_to_confirm(self):
        self.model = {"action": "autonomous", "reply": ""}
        self.tell(ANNA, "could the agents just take it all the way now?", mention=False)
        self.tell(ANNA, "yes", mention=False)
        self.assertEqual((self.said[-1], self.levels), (pm_implicit.HOW_TO_CONFIRM, []))
        self.tell(ANNA, "yes", mention=False, reply_to=self.lp.pending["!r"]["event_id"])
        self.assertEqual(self.levels, ["autonomous"])

    def test_a_reply_to_pm_with_the_agent_offline_says_so(self):
        self.lp.agent_online = lambda agent: False
        self.replies_are_to_pm()
        self.tell(BETTY, "what about the login page?", mention=False, reply_to="$m1")
        self.assertTrue(self.said[-1].startswith(pm_owner_brain.OFFLINE + "I didn't catch that"))

    def test_a_stopped_or_hermes_owner_agent_is_named_as_such(self):
        for reason in (STOPPED, HERMES):
            with mock.patch.object(pm_owner_brain, "cannot", lambda user, agent, r=reason: r):
                self.tell(BETTY, "@pm blorp")
            self.assertTrue(self.said[-1].startswith(pm_owner_brain.CANNOT[reason]))
        self.assertEqual(self.asked, [])

    def test_typing_shows_for_a_reply_but_not_for_a_maybe(self):
        self.model = {"action": "none", "reply": ""}
        self.tell(BETTY, "what do you think of the colours?", mention=False)
        self.lp.client.room_typing.assert_not_awaited()
        self.replies_are_to_pm()
        self.tell(BETTY, "and the fonts?", mention=False, reply_to="$m1")
        self.lp.client.room_typing.assert_any_await("!r", True, timeout=30_000)

    # --- newcomers learn it inside the app ------------------------------------------------
    def test_help_and_about_say_how_it_works(self):
        self.assertIn('skip "@pm" when you reply', pm_chat.HELP)
        with mock.patch.object(pm_brain, "BRAIN", "owner"):
            self.assertIn("share recent messages here with the room owner's agent", pm_brain.about())


if __name__ == "__main__":
    unittest.main()
