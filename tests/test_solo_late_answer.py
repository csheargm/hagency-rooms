"""A bypass the owner's agent added while @pm restarted is never left behind (GPT whole-batch review): the ask is on
disk before it is sent, its answer still counts when it arrives as "history" after the restart, and switching solo
review off before the answer says so plainly and takes it out once the answer comes.
Run: .venv/bin/python -m unittest tests.test_solo_late_answer"""
import unittest
from types import SimpleNamespace
from unittest import mock

from common import github, intents, solo_review
from tests.test_solo_review import CX, CXA, Base


class LateAnswer(Base):
    def setUp(self):
        super().setUp()
        self.lp.busy, self.lp.started_ms = set(), 10 ** 13      # @pm started after the answer was posted
        self.lp.sync_point = SimpleNamespace(first_time=lambda e: True)
        self.lp.refresh_boards, self.lp.kick = mock.AsyncMock(), (lambda: None)

    def posted_during_restart(self, job, **result):
        content = {github.RESULT_KEY: {"kind": solo_review.TASK_KIND, "task_id": job["task_id"], **result}}
        event = SimpleNamespace(server_timestamp=1, event_id="$late", sender=job["agent"], source={"content": content})
        self.run_(self.lp.on_message(self.room, event))

    def test_an_answer_posted_during_a_restart_is_recorded(self):
        self.enable()
        self.posted_during_restart(self.job(), on=True, changed=[42])
        self.assertEqual(self.recorded()["bypass_added"], [42])
        self.assertEqual(self.lp.solo_jobs(), {})

    def test_switched_off_before_the_answer_it_is_said_and_taken_out_when_it_comes(self):
        self.enable()
        job = self.job()
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))
        self.assertIn("hasn't said yet", self.said[-1][0])
        self.assertIn("Bypass list", self.said[-1][0])
        self.posted_during_restart(job, on=True, changed=[42])
        off = self.tasks(solo_review.TASK_KIND)[-1]
        self.assertEqual((off["on"], off["rulesets"]), (False, [42]))

    def test_other_old_messages_stay_history(self):
        self.enable()
        event = SimpleNamespace(server_timestamp=1, event_id="$old", sender=CXA, source={"content": {
            github.RESULT_KEY: {"kind": solo_review.TASK_KIND, "task_id": "not-asked", "on": True, "changed": [7]}}})
        self.assertFalse(self.lp.awaited_solo(event))
        self.assertTrue(self.lp.awaited_solo(SimpleNamespace(source={"content": {github.RESULT_KEY: {
            "kind": solo_review.TASK_KIND, "task_id": self.job()["task_id"]}}})))


if __name__ == "__main__":
    unittest.main()
