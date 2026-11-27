"""GPT review of ADR 0022, as user flows (R6) in the simulated room of tests/owner_ai_room.py: the model's
reading never acts alone, a stop invalidates everything whatever triggered it, budgets come before any
work, and the owner's agent must still be the owner's when the context goes out.
Run: .venv/bin/python -m unittest tests.test_owner_ai_safety"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetEventResponse, RoomGetStateResponse

from adapters.agents import agents
from common import intents
from common import room_state
from server import pm_chat
from server import pm_owner_brain
from server import pm_settings
from server import pm_understand
from server.loop import Loop
from tests.owner_ai_room import ANNA, ANNA_AGENT, BETTY, CREATE, PM, RoomBase


def named(sender: str, body: str) -> SimpleNamespace:
    return SimpleNamespace(sender=sender, body=body, source={"content": {"body": body, "m.mentions": {"user_ids": [PM]}}})


class ModelNeverActsAlone(RoomBase, unittest.TestCase):
    """Finding 1 (and 5): recognized phrases stay on the rules; what only the model read is asked first."""
    def test_a_question_the_model_reads_as_stop_is_asked_first(self):
        self.model = {"action": "stop", "reply": ""}
        self.tell(BETTY, "@pm how is the hello page going?")
        self.assertEqual(self.levels, [])
        self.assertIn("Do you want me to stop?", self.said[-1])
        self.react(BETTY)
        self.assertEqual(self.levels, ["off"])

    def test_a_mode_the_model_read_waits_for_the_owners_thumb(self):
        self.model = {"action": "autonomous", "reply": ""}
        self.tell(ANNA, "@pm how is the hello page going?")
        self.assertEqual(self.levels, [])
        self.assertIn("switch to **autonomous**", self.said[-1])
        self.react(ANNA)
        self.assertEqual(self.levels, ["autonomous"])

    def test_a_plan_the_model_read_is_asked_first(self):
        self.lp.start_plan = mock.AsyncMock()
        self.model = {"action": "plan", "reply": "I'll draft it."}
        self.tell(ANNA, "@pm the colours seem settled now")
        self.lp.start_plan.assert_not_awaited()
        self.assertNotIn("I'll draft it.", self.said)
        self.react(ANNA)
        self.lp.start_plan.assert_awaited_once_with(self.room, ANNA)

    def test_a_redraft_the_model_read_keeps_the_plan_question(self):
        self.lp.start_plan = mock.AsyncMock()
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$plan", "by": ANNA, "until": time.time() + 300}
        self.model = {"action": "plan", "reply": ""}
        self.tell(ANNA, "@pm the colours seem settled now")
        self.assertEqual(self.lp.pending["!r"]["event_id"], "$plan")
        self.lp.start_plan.assert_not_awaited()
        asyncio.run(self.lp.on_reaction(self.room, SimpleNamespace(sender=BETTY, key="👍", reacts_to="$m1")))
        self.lp.start_plan.assert_not_awaited()       # only the person who asked
        asyncio.run(self.lp.on_reaction(self.room, SimpleNamespace(sender=ANNA, key="👍", reacts_to="$m1")))
        self.lp.start_plan.assert_awaited_once_with(self.room, ANNA)

    def test_an_approval_the_model_read_never_approves(self):
        self.lp.pending["!r"] = {"kind": "mode", "level": "autonomous", "event_id": "$q", "by": ANNA,
                                 "until": time.time() + 300}
        self.model = {"action": "approve", "reply": ""}
        self.tell(ANNA, "@pm the colours seem settled now")
        self.assertEqual((self.levels, self.said[-1]), ([], pm_understand.CLICK_TO_APPROVE))

    def test_a_setting_the_model_read_is_asked_first(self):
        self.lp.set_follow = mock.AsyncMock()
        self.model = {"action": "follow_always", "reply": ""}
        self.tell(ANNA, "@pm the colours seem settled now")
        self.lp.set_follow.assert_not_awaited()
        self.assertEqual(self.lp.pending["!r"]["kind"], "setting")

    def test_known_commands_never_wait_for_a_model_even_an_old_adapter(self):
        async def ignore(event):
            return None                               # an adapter from before ADR 0022: never acks
        self.b.on_understand = ignore
        for body in ("@pm help", "@pm where are we?", "@pm show the plan"):
            self.tell(BETTY, body)
        self.assertEqual((self.asked, self.said[0]), ([], pm_chat.HELP))
        self.assertIn("Mode:", self.said[1])


class StopInvalidates(RoomBase, unittest.TestCase):
    """Finding 2: one invalidation on every stop path; checked when a question is installed and consumed."""
    def test_a_stop_while_the_mode_question_is_sent_never_installs_it(self):
        say = self.lp.say

        async def stop_meanwhile(room_id, body, *a, **k):
            if "autonomous" in body:
                await self.lp.act(self.room, BETTY, intents.OFF)
            return await say(room_id, body, *a, **k)
        self.lp.say = stop_meanwhile
        self.model = {"action": "autonomous", "reply": ""}
        self.tell(ANNA, "could the agents just take it all the way now?", mention=False)
        self.assertEqual((self.lp.pending, self.levels), ({}, ["off"]))

    def test_a_plan_question_is_never_overwritten_during_a_send(self):
        say, plan = self.lp.say, {"kind": "plan", "event_id": "$plan", "by": ANNA, "until": time.time() + 300}

        async def plan_meanwhile(room_id, body, *a, **k):
            if "stop" in body:
                self.lp.pending["!r"] = plan
            return await say(room_id, body, *a, **k)
        self.lp.say = plan_meanwhile
        self.model = {"action": "stop", "reply": ""}
        self.tell(BETTY, "can we stop all the building for today?", mention=False)
        self.assertIs(self.lp.pending["!r"], plan)

    def test_level_off_while_the_model_thinks_drops_its_mode_question(self):
        self.lp.change_level = Loop.change_level.__get__(self.lp)          # the real one: !level off
        self.lp.write_level = mock.AsyncMock(side_effect=lambda rid, level, **k: self.levels.append(level))
        gate = asyncio.Event()

        async def slow(backend, workdir, prompt, **kw):
            await gate.wait()
            return '{"action": "autonomous", "reply": ""}'

        async def go():
            with mock.patch.object(agents, "run_turn", slow):
                await self.lp.on_human(self.room, named(ANNA, "@pm how is the hello page going?"))
                await asyncio.sleep(0.01)
                await self.lp.on_human(self.room, SimpleNamespace(sender=BETTY, body="!level off", source={"content": {}}))
                gate.set()
                await asyncio.gather(*self.lp.busy, *self.b.tasks)
        asyncio.run(go())
        self.assertEqual((self.levels, self.lp.pending), (["off"], {}))

    def test_a_stop_during_the_confirm_check_cancels_the_thumb(self):
        self.lp.pending["!r"] = {"kind": "mode", "level": "autonomous", "event_id": "$q", "by": ANNA,
                                 "until": time.time() + 300}

        async def stop_meanwhile(client, room, sender):
            self.lp.invalidate("!r")                  # "@pm stop" lands while the 👍's authority is checked
            return True
        with mock.patch.object(room_state, "is_moderator", stop_meanwhile):
            self.react(ANNA)
        self.assertEqual(self.levels, [])


    def test_a_console_stop_during_the_confirmed_switchs_moderator_check_wins(self):
        """GPT re-check: the 👍 passed, then act() looked up the moderator; a stop meanwhile saved "off",
        and the older confirmation then saved "autonomous"."""
        self.lp.change_level = Loop.change_level.__get__(self.lp)          # the real write, under its lock
        self.lp.write_level = Loop.write_level.__get__(self.lp)
        self.lp.kick = mock.Mock()
        saved = []
        self.lp.client.room_put_state = mock.AsyncMock(side_effect=lambda rid, kind, content: saved.append(content)
                                                       or SimpleNamespace(event_id="$s"))
        self.lp.pending["!r"] = {"kind": "implicit", "intent": intents.AUTONOMOUS, "event_id": "$q", "by": ANNA,
                                 "until": time.time() + 300}
        checks = []

        async def stop_on_second_check(client, room, sender):
            checks.append(sender)
            if len(checks) == 1:                      # act()'s own check (an implicit 👍 has none): the stop lands now
                await self.lp.change_level("!r", "off")
            return True
        with mock.patch.object(room_state, "is_moderator", stop_on_second_check), \
                mock.patch.object(room_state, "binding_of", mock.AsyncMock(return_value=("you/hello", False))):
            self.react(ANNA)
        self.assertEqual([c.get("level") for c in saved], ["off"])
        self.assertIn(pm_settings.STOPPED_MEANWHILE, self.said)


class RedraftOnTheFallback(RoomBase, unittest.TestCase):
    def test_a_reply_to_an_older_pm_message_never_redrafts_without_a_thumb(self):
        """GPT re-check: plan waiting, the owner's agent offline, a moderator's reply to an older @pm message
        went fall_back -> by_rules -> revise_plan and cleared the approval."""
        mine = RoomGetEventResponse()
        mine.event = SimpleNamespace(sender=PM, body="Mode: assist.")
        self.lp.client.room_get_event = mock.AsyncMock(return_value=mine)
        self.lp.agent_online = lambda agent: False
        self.lp.start_plan = mock.AsyncMock()
        plan = {"kind": "plan", "event_id": "$plan", "by": ANNA, "until": time.time() + 300}
        self.lp.pending["!r"] = plan
        self.tell(ANNA, "drop the French page", mention=False, reply_to="$older")
        self.lp.start_plan.assert_not_awaited()
        self.assertIs(self.lp.pending["!r"], plan)
        self.assertIn("redraft the plan", self.said[-1])


    def test_a_failed_ask_to_redraft_never_falls_through_to_a_redraft(self):
        """GPT re-check: the first moderator check failed (a passing Matrix error), so by_rules redrafted."""
        mine = RoomGetEventResponse()
        mine.event = SimpleNamespace(sender=PM, body="Mode: assist.")
        self.lp.client.room_get_event = mock.AsyncMock(return_value=mine)
        self.lp.agent_online = lambda agent: False
        self.lp.start_plan = mock.AsyncMock()
        self.lp.ask_redraft = mock.AsyncMock(return_value=False)
        plan = {"kind": "plan", "event_id": "$plan", "by": ANNA, "until": time.time() + 300}
        self.lp.pending["!r"] = plan
        self.tell(ANNA, "drop the French page", mention=False, reply_to="$older")
        self.lp.start_plan.assert_not_awaited()
        self.assertIs(self.lp.pending["!r"], plan)

class OwnersAgentAndBudgets(RoomBase, unittest.TestCase):
    """Findings 3 and 4."""
    def test_an_agent_that_left_after_admission_gets_no_context(self):
        def leave(room_id, include_self=False):
            left = [e for e in CREATE.events if e.get("state_key") != ANNA_AGENT]   # it left while the history was read
            self.lp.client.room_get_state = mock.AsyncMock(return_value=RoomGetStateResponse(left, "!r"))
            return "anna: hi"
        self.lp.discussion = leave
        self.tell(BETTY, "@pm colours?")
        self.assertEqual(self.asked, [])
        self.assertTrue(self.said[-1].startswith(pm_owner_brain.NO_AGENT))

    def test_an_owner_who_lost_the_room_gets_no_context(self):
        def handed_over(room_id, include_self=False):
            self.lp.client.room_get_state = mock.AsyncMock(return_value=RoomGetStateResponse(
                [{"type": "m.room.create", "sender": BETTY, "state_key": "", "content": {"room_version": "12"}}], "!r"))
            return "anna: hi"
        self.lp.discussion = handed_over
        self.tell(BETTY, "@pm how is the hello page going?")
        self.assertEqual(self.asked, [])

    def test_the_owners_agent_has_one_budget_across_its_rooms(self):
        other = SimpleNamespace(**{**vars(self.room), "room_id": "!r2"})
        self.lp.client.rooms["!r2"] = other
        async def go():
            for room in (self.room, other, self.room, other, self.room):   # under each room's own cap
                await self.lp.on_human(room, named(ANNA, "@pm how is the hello page going?"))
                await asyncio.gather(*self.lp.busy, *self.b.tasks)
        with mock.patch.object(pm_owner_brain, "AGENT_CAPS", ((3, 60),)):
            asyncio.run(go())
        self.assertEqual(len(self.asked), 3)

    def test_a_refused_ask_reads_no_history(self):
        self.lp.discussion = mock.Mock(return_value="")
        with mock.patch.object(pm_owner_brain, "SENDER_CAPS", ((1, 60),)):
            self.tell(BETTY, "@pm how is the hello page going?")
            self.tell(BETTY, "@pm and the French page?")
        self.assertEqual((len(self.asked), self.lp.discussion.call_count), (1, 1))
        self.assertTrue(self.said[-1].startswith(pm_owner_brain.BUSY))

    def test_understanding_jobs_at_once_are_bounded(self):
        gate = asyncio.Event()

        async def slow(backend, workdir, prompt, **kw):
            await gate.wait()
            return '{"action": "none", "reply": "ok"}'

        async def go():
            with mock.patch.object(agents, "run_turn", slow), mock.patch.object(pm_understand, "MAX_JOBS", 2):
                for i in range(4):
                    await self.lp.on_human(self.room, named(BETTY, f"@pm what about option {i}?"))
                await asyncio.sleep(0.01)
                gate.set()
                await asyncio.gather(*self.lp.busy, *self.b.tasks)
        asyncio.run(go())
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(sum(s.startswith(pm_understand.TOO_MANY) for s in self.said), 2)


if __name__ == "__main__":
    unittest.main()
