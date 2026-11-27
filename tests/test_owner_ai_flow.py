"""The user flow (R6): a project room where @pm understands with the room owner's agent (ADR 0022).

A real Loop and a real agent adapter (Bridge) talk through a fake to-device bus; only the model's answer
and Matrix itself are faked. Run: .venv/bin/python -m unittest tests.test_owner_ai_flow"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetEventResponse

from adapters.agents import agents
from common import pm_actions
from server import pm_owner_brain
from tests.owner_ai_room import ANNA, ANNA_AGENT, BETTY, PM, RoomBase


class Room(RoomBase, unittest.TestCase):
    # --- addressed: the owner's agent understands it -----------------------------------
    def test_the_owners_agent_understands_a_message_to_pm(self):
        self.model = {"action": "status", "reply": "Let me check."}
        self.tell(BETTY, "@pm how far along is the French page?")
        self.assertIn("Mode:", self.said[-1])
        prompt, kw = self.turns[0]
        self.assertEqual(kw, {"fresh": True, "bare": True})         # one turn, no tools
        self.assertIn("in French too", prompt)
        self.assertIn("how far along is the French page?", prompt)

    def test_a_plain_answer_is_said_as_the_model_wrote_it(self):
        self.model = {"action": "none", "reply": "The French page is next in the plan."}
        self.tell(ANNA, "@pm what comes after the hello page?")
        self.assertEqual(self.said[-1], "The French page is next in the plan.")

    def test_an_offline_owner_agent_falls_back_and_says_so(self):
        self.lp.agent_online = lambda agent: False
        self.tell(BETTY, "@pm blorp")
        self.assertEqual((self.asked, self.levels), ([], []))
        self.assertTrue(self.said[-1].startswith(pm_owner_brain.OFFLINE + "I didn't catch that"))

    def test_old_agent_code_never_answers_and_isnt_asked_again_for_a_while(self):
        async def ignore(event):
            return None                               # an adapter from before ADR 0022: no listener
        self.b.on_understand = ignore
        self.tell(BETTY, "@pm blorp")
        self.assertIn(pm_owner_brain.NOT_ANSWERING, self.said[-1])
        self.tell(BETTY, "@pm where are we?")         # no second 8 s wait: the rules answer at once
        self.assertEqual(len(self.asked), 1)
        self.assertIn("Mode:", self.said[-1])

    def test_no_owner_agent_in_the_room_uses_the_rules(self):
        self.lp.own_agent = lambda room, person: None
        self.tell(BETTY, "@pm where are we?")
        self.assertEqual(self.asked, [])
        self.assertIn("Mode:", self.said[-1])

    def test_the_models_action_still_needs_permission(self):
        self.model = {"action": "autonomous", "reply": ""}
        self.tell(BETTY, "@pm please just finish everything yourself")
        self.assertEqual(self.levels, [])
        self.assertIn("Only the room owner or a maintainer", self.said[-1])

    def test_stop_is_instant_and_never_asks_a_model(self):
        self.tell(BETTY, "@pm stop")
        self.assertEqual((self.levels, self.asked), (["off"], []))

    # --- without "@pm" ----------------------------------------------------------------
    def test_a_reply_to_pm_needs_no_at_pm(self):
        mine = RoomGetEventResponse()
        mine.event = SimpleNamespace(sender=PM, body="Mode: assist.")
        self.lp.client.room_get_event = mock.AsyncMock(return_value=mine)
        self.model = {"action": "status", "reply": ""}
        self.tell(BETTY, "and what's next after that?", mention=False, reply_to="$m1")
        self.assertIn("Mode:", self.said[-1])
        self.assertIn("did not name @pm (it replies to one of @pm's messages)", self.turns[0][0])

    def test_talking_on_with_pm_needs_no_at_pm(self):
        self.model = {"action": "status", "reply": ""}
        self.tell(ANNA, "@pm how is the hello page going?")
        self.tell(ANNA, "and how long will the rest take?", mention=False)
        self.assertEqual(len(self.turns), 2)
        self.assertIn("@pm was just talking with them", self.turns[1][0])

    def test_unaddressed_high_impact_asks_first(self):
        self.model = {"action": "stop", "reply": ""}
        self.tell(BETTY, "can we stop all the building for today?", mention=False)
        self.assertEqual(self.levels, [])
        self.assertIn("Do you want me to stop?", self.said[-1])
        self.react(ANNA)                              # only the person asked confirms
        self.assertEqual(self.levels, [])
        self.react(BETTY)
        self.assertEqual(self.levels, ["off"])

    def test_unaddressed_mode_change_asks_first_even_from_the_owner(self):
        self.model = {"action": "autonomous", "reply": ""}
        self.tell(ANNA, "could the agents just take it all the way now?", mention=False)
        self.assertIn("Do you want me to switch to **autonomous**", self.said[-1])
        self.react(ANNA)
        self.assertEqual(self.levels, ["autonomous"])

    def test_a_stray_yes_doesnt_confirm_a_question_asked_without_being_named(self):
        """GPT review: a maintainer's "yes" to someone else switched the room to autonomous."""
        self.model = {"action": "autonomous", "reply": ""}
        self.tell(ANNA, "could the agents just take it all the way now?", mention=False)
        self.model = {"action": "none", "reply": ""}
        self.tell(ANNA, "yes", mention=False)
        self.assertEqual(self.levels, [])
        self.tell(ANNA, "@pm yes")                    # named, or a 👍 / a reply to the question: counts
        self.assertEqual(self.levels, ["autonomous"])

    def test_a_repo_without_at_pm_is_asked_and_never_left_to_the_model(self):
        self.room.power_levels = SimpleNamespace(get_user_level=lambda u: 100 if u == ANNA else 0)
        self.lp.set_repo = mock.AsyncMock()
        self.tell(ANNA, "@pm where are we?")
        self.tell(ANNA, "this room works on you/other", mention=False)
        self.assertEqual(self.turns, [])              # a phrase the rules know (named or not): never a model
        self.assertIn("Should this room work on **you/other**?", self.said[-1])
        self.react(ANNA)
        self.lp.set_repo.assert_awaited_once_with(self.room, ANNA, "you/other")

    def test_a_forged_answer_cant_break_pm(self):
        """GPT review: anyone can send @pm a to-device message; a list as task_id raised in the sync loop."""
        self.lp.brain_waits()["x"] = {"agent": ANNA_AGENT}
        asyncio.run(self.lp.on_understood(SimpleNamespace(type=pm_actions.RESULT_TYPE, sender=BETTY,
                                                          source={"content": {"task_id": ["x"], "ack": True}})))

    def test_plain_chat_between_people_is_ignored(self):
        self.tell(BETTY, "I had pizza for lunch", mention=False)
        self.tell(BETTY, "anna: can you look at the colours?", mention=False)      # for someone else
        self.tell(BETTY, "anna-agent what does this file do?", mention=False)
        self.assertEqual((self.asked, self.said), ([], []))

    def test_the_owners_agent_can_say_it_isnt_for_pm(self):
        self.model = {"action": "none", "reply": ""}
        self.tell(BETTY, "what do you think of the colours?", mention=False)
        self.assertEqual((len(self.asked), self.said), (1, []))
        self.assertIn("it may be for @pm, or for someone else", self.turns[0][0])

    def test_reads_of_unaddressed_messages_are_capped(self):
        self.model = {"action": "none", "reply": ""}
        for i in range(pm_owner_brain_reads() + 3):
            self.tell(BETTY, f"what do you think of option {i}?", mention=False)
        self.assertEqual(len(self.asked), pm_owner_brain_reads())

    def test_the_only_person_in_the_room_needs_no_at_pm(self):
        del self.room.users[BETTY]
        self.lp.agent_online = lambda agent: False    # no model at all: the phrase rules, low impact only
        self.tell(ANNA, "where are we?", mention=False)
        self.assertIn("Mode:", self.said[-1])
        self.tell(ANNA, "nice weather", mention=False)          # nothing the rules know: quiet
        self.assertEqual(len(self.said), 1)

    def test_a_stop_while_the_owners_agent_thinks_drops_its_answer(self):
        gate = asyncio.Event()

        async def slow(backend, workdir, prompt, **kw):
            await gate.wait()
            return '{"action": "none", "reply": "A long answer"}'

        async def go():
            with mock.patch.object(agents, "run_turn", slow):
                await self.lp.on_human(self.room, SimpleNamespace(sender=ANNA, body="@pm tell me everything",
                                                                  source={"content": {"m.mentions": {"user_ids": [PM]}}}))
                await asyncio.sleep(0.01)
                await self.lp.on_human(self.room, SimpleNamespace(sender=BETTY, body="@pm stop",
                                                                  source={"content": {"m.mentions": {"user_ids": [PM]}}}))
                gate.set()
                await asyncio.gather(*self.lp.busy, *self.b.tasks)
        asyncio.run(go())
        self.assertEqual(self.levels, ["off"])
        self.assertNotIn("A long answer", self.said)


def pm_owner_brain_reads() -> int:
    from server import pm_implicit
    return pm_implicit.PER_MINUTE


if __name__ == "__main__":
    unittest.main()
