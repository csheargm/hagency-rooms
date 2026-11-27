"""The user flow (R6): an answer to @pm's own message, without "@pm" and not as a reply (live, 2026-11-27).

@pm said "#13 is paused: there's no second person's agent here to review it", and the room owner wrote
"no need for a second person at this testing stage" a little later: the understanding step must read it.
Run: .venv/bin/python -m unittest tests.test_after_pm_flow"""
import asyncio
import time
import unittest
from types import SimpleNamespace

from common import github
from server import pm_implicit
from tests.owner_ai_room import ANNA, ANNA_AGENT, BETTY, PM, RoomBase

PAUSED = ("⏸ #13 is paused: there's no second person's agent here to review it. Review and merge it on "
          "GitHub, or invite a second person with their agent.")
ANSWER = "no need for a second person at this testing stage"
T0 = int(time.time() * 1000) - 10_000                  # ms: just now


class AfterPm(RoomBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.n = 0

    def post(self, sender, body, at_s, content=None):
        """A message seen by the sync loop (note_said), not handled further: @pm's own, an agent's."""
        self.n += 1
        ev = SimpleNamespace(sender=sender, body=body, event_id=f"$e{self.n}", server_timestamp=T0 + at_s * 1000,
                             source={"content": {"body": body, **(content or {})}})
        self.lp.note_said(self.room, ev)
        return ev

    def answer(self, sender, body, at_s, seen_first=True):
        """A person's message without "@pm": note_said runs before on_human (listen_early registers it first)."""
        self.n += 1
        ev = SimpleNamespace(sender=sender, body=body, event_id=f"$e{self.n}", server_timestamp=T0 + at_s * 1000,
                             source={"content": {"body": body, "m.mentions": {}}})
        if seen_first:
            self.lp.note_said(self.room, ev)

        async def go():
            await self.lp.on_human(self.room, ev)
            while self.lp.busy or self.b.tasks:
                await asyncio.gather(*self.lp.busy, *self.b.tasks)
        asyncio.run(go())

    def test_the_owners_answer_to_pms_status_message_is_understood(self):
        self.model = {"action": "none", "reply": "Understood: with only your agents here I can't get a "
                                                  "second-person review; merge PR #16 yourself on GitHub."}
        self.post(PM, PAUSED, 0)
        self.answer(ANNA, ANSWER, 20)
        self.assertEqual(len(self.asked), 1)
        self.assertIn(pm_implicit.AFTER_PM, self.turns[0][0])
        self.assertIn(ANSWER, self.turns[0][0])
        self.assertEqual(self.said, [self.model["reply"]])            # the model's words, posted
        self.lp.client.room_typing.assert_not_awaited()                # speculative: no typing shown

    def test_the_model_may_still_say_it_isnt_for_pm(self):
        self.model = {"action": "none", "reply": ""}
        self.post(PM, PAUSED, 0)
        self.answer(ANNA, ANSWER, 20)
        self.assertEqual((len(self.asked), self.said), (1, []))

    def test_someone_else_speaking_in_between_means_ordinary_chat(self):
        self.post(PM, PAUSED, 0)
        self.post(ANNA_AGENT, "I'll look at the colours next.", 5)
        self.answer(ANNA, ANSWER, 20)
        self.post(PM, PAUSED, 30)
        self.post(BETTY, "I had pizza for lunch", 31)
        self.answer(ANNA, ANSWER, 32)                                   # betty spoke after @pm
        self.assertEqual((self.asked, self.said), ([], []))

    def test_too_long_after_pm_is_ordinary_chat(self):
        self.post(PM, PAUSED, -pm_implicit.FOLLOW_S - 2)
        self.answer(ANNA, ANSWER, -1)
        self.assertEqual((self.asked, self.said), ([], []))

    def test_a_late_delivered_answer_is_ordinary_chat(self):
        """GPT review: both events reach @pm an hour late (a stuck sync): no answer to read now."""
        self.post(PM, PAUSED, -3600)
        self.answer(ANNA, ANSWER, -3580)
        self.assertEqual((self.asked, self.said), ([], []))

    def test_a_task_to_an_agent_or_an_edit_isnt_pm_speaking_to_people(self):
        self.post(PM, "anna-agent, please build #3", 0, {github.TASK_KEY: {"kind": "build"}})
        self.answer(ANNA, ANSWER, 5)
        self.post(ANNA_AGENT, "done with #3", 6)
        self.post(PM, "📋 board", 7, {"m.relates_to": {"rel_type": "m.replace", "event_id": "$board"}})
        self.answer(ANNA, ANSWER, 8)                                    # the board edit is no new message
        self.assertEqual((self.asked, self.said), ([], []))

    def test_a_task_right_after_pms_status_doesnt_hide_the_status(self):
        """GPT whole-batch review: @pm's task message was noted as the latest, by someone else, so the answer
        to the status just before it was ignored."""
        self.model = {"action": "none", "reply": ""}
        self.post(PM, PAUSED, 0)
        self.post(PM, "anna-agent, please build #3", 1, {github.TASK_KEY: {"kind": "build"}})
        self.answer(ANNA, ANSWER, 5)
        self.assertEqual(len(self.asked), 1)

    def test_without_a_model_it_stays_quiet(self):
        """No rule-based fallback for a speculative read: never "didn't catch that" to chat."""
        self.lp.agent_online = lambda agent: False
        self.post(PM, PAUSED, 0)
        self.answer(ANNA, ANSWER, 20)
        self.assertEqual(self.said, [])

    def test_an_exact_phrase_after_pm_is_never_acted_on(self):
        self.post(PM, PAUSED, 0)
        self.answer(ANNA, "retry #13", 20)
        self.assertEqual((self.asked, self.said, self.lp.pending), ([], [], {}))

    def test_the_budget_still_caps_reads(self):
        self.model = {"action": "none", "reply": ""}
        for i in range(pm_implicit.PER_MINUTE + 2):
            self.post(PM, PAUSED, -60 + i * 2)
            self.answer(ANNA, f"{ANSWER} {i}", -60 + i * 2 + 1)
        self.assertEqual(len(self.asked), pm_implicit.PER_MINUTE)

    def test_either_callback_order_works(self):
        self.model = {"action": "none", "reply": ""}
        self.post(PM, PAUSED, 0)
        self.answer(ANNA, ANSWER, 20, seen_first=False)
        self.assertEqual(len(self.asked), 1)


class NoteSaid(unittest.TestCase):
    def setUp(self):
        self.lp = pm_implicit.Implicit()
        self.lp.user_id = PM
        self.room = SimpleNamespace(room_id="!r")

    def ev(self, sender, eid, ms, content=None):
        return SimpleNamespace(sender=sender, event_id=eid, server_timestamp=T0 + ms, source={"content": content or {}})

    def test_keeps_only_the_last_two_messages(self):
        for i in range(5):
            self.lp.note_said(self.room, self.ev(PM, f"$p{i}", i))
        last = self.lp._said["!r"]
        self.assertEqual((last[0], last[3][0], len(last[3])), ("$p4", "$p3", 3))   # no chain: bounded memory

    def test_a_replayed_event_doesnt_hide_what_came_before(self):
        self.lp.note_said(self.room, self.ev(PM, "$p", 0))
        mine = self.ev(ANNA, "$a", 1000)
        self.lp.note_said(self.room, mine)
        self.lp.note_said(self.room, mine)
        self.assertTrue(self.lp.follows_pm("!r", mine))

    def test_nothing_seen_or_no_timestamp_is_no(self):
        self.assertFalse(self.lp.follows_pm("!r", self.ev(ANNA, "$a", 1)))
        self.lp.note_said(self.room, self.ev(PM, "$p", 0))
        self.assertFalse(self.lp.follows_pm("!r", SimpleNamespace(event_id="$x")))


if __name__ == "__main__":
    unittest.main()
