"""Default-on solo review never replaces another person's agent that is in the room but briefly offline (GPT
whole-batch review): the item waits for it, said plainly, and a sibling's verdict doesn't count while another
person's agent is here to check. Run: .venv/bin/python -m unittest tests.test_solo_away_checker"""
import unittest

from tests.test_solo_review import BA, CX, CXA, CXC, Base


class AwayChecker(Base):
    EXPLICIT_OFF = False                      # on by default

    def test_another_persons_offline_agent_is_waited_for(self):
        self.agents = [CXA, CXC, BA]
        self.lp.agent_online = lambda a: a != "betty-agent"
        item = self.built()
        self.assertEqual((item["stage"], item.get("no_checker"), self.tasks("review")), ("stopped", True, []))
        self.assertIn("betty-agent, is here but offline", self.said[-1][0])
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

    def test_alone_the_siblings_verdict_still_counts(self):
        item = self.built()
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        self.assertEqual(item["stage"], "approved")


if __name__ == "__main__":
    unittest.main()
