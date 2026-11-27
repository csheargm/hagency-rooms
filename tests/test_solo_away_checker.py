"""Default-on solo review never replaces another person's agent that is in the room but briefly offline (GPT
whole-batch review): the item waits for it, said plainly, and a sibling's verdict doesn't count while another
person's agent is here to check. Run: .venv/bin/python -m unittest tests.test_solo_away_checker"""
import unittest

from tests.test_solo_review import BA, CX, CXA, CXC, KEY, Base


class AwayChecker(Base):
    EXPLICIT_OFF = False                      # on by default

    def test_another_persons_offline_agent_is_waited_for(self):
        self.agents = [CXA, CXC, BA]
        self.lp.agent_online = lambda a: a != "betty-agent"
        item = self.built()
        self.assertEqual((item["stage"], item.get("no_checker"), self.tasks("review")), ("stopped", True, []))
        self.assertIn("betty-agent, is here but offline", self.said[-1][0])
        self.assertIn("no second person's agent online here", self.said[-1][0])   # never "no one here" (GPT review)
        self.assertNotIn("away_checker", item)
        self.lp.agent_online = lambda a: True   # back: "@pm retry #7" gives it to the other person's agent
        self.run_(self.lp.retry(self.room, CX, 7))
        self.assertEqual((item["stage"], item["reviewer"], item["solo"]), ("reviewing", BA, False))

    def test_a_siblings_verdict_doesnt_count_once_another_persons_agent_is_here(self):
        item = self.built()
        self.assertEqual((item["reviewer"], item["solo"]), (CXC, True))
        self.agents = [CXA, CXC, BA]          # another person's agent came (back) meanwhile
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        self.assertEqual((item["stage"], item.get("no_checker")), ("stopped", True))
        self.assertIn("doesn't count", " ".join(s for s, _, _ in self.said))

    def fix_round(self, betty_online):
        """CXC (solo) asks for changes, betty-agent joins, CXA's fix comes back: who reviews it again."""
        self.lp.client.rooms = {"!r": self.room}
        item = self.built()
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc", "verdict":
                                                     "request_changes", "reasons": "x", "task_id": item["task_id"]}))
        self.agents = [CXA, CXC, BA]
        self.lp.agent_online = lambda a: betty_online or a != "betty-agent"
        self.run_(self.lp.on_result(self.room, CXA, {"kind": "fix", "issue": 7, "pr": 9, "sha": "def",
                                                     "task_id": item["task_id"]}))
        return item

    def test_after_fixes_another_persons_agent_that_joined_reviews(self):
        """GPT review: the re-review went to the sibling, whose verdict was then refused."""
        item = self.fix_round(betty_online=True)
        self.assertEqual((item["stage"], item["reviewer"], item["solo"]), ("reviewing", BA, False))
        self.assertEqual([m for _, m, t in self.said if t and t["kind"] == "review"][-1], BA)   # sent to betty-agent
        self.assertNotIn("solo", self.tasks("review")[-1])

    def test_after_fixes_an_offline_other_persons_agent_is_waited_for(self):
        item = self.fix_round(betty_online=False)
        self.assertEqual((item["stage"], item.get("no_checker")), ("stopped", True))
        self.assertIn("is here but offline", self.said[-1][0])

    def test_a_refused_announcement_pauses_instead_of_leaving_the_fix_task(self):
        """GPT review round 2: Matrix refusing "review requested from …" left it at "reviewing" with the fix task."""
        from server.loop import SendError
        real = self.lp.say

        async def say(rid, body, mention=None, task=None, **kw):
            if body.startswith("review requested from betty"):
                raise SendError("refused")
            return await real(rid, body, mention, task, **kw)
        self.lp.say = say
        with self.assertRaises(SendError):                   # take_result logs it
            self.fix_round(betty_online=True)
        item = self.lp.state[KEY]
        self.assertEqual((item["stage"], item.get("no_checker")), ("stopped", True))
        self.lp.say = real
        self.run_(self.lp.retry(self.room, CX, 7))
        self.assertEqual((item["stage"], item["reviewer"]), ("reviewing", BA))

    def test_alone_the_siblings_verdict_still_counts(self):
        item = self.built()
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        self.assertEqual(item["stage"], "approved")


if __name__ == "__main__":
    unittest.main()
