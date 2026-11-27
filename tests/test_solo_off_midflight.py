"""Solo review switched off while @pm asks GitHub whether a solo-checked PR may merge (GPT whole-batch review):
the regular merge (no GitHub approval needed) never goes out on the solo verdict, and a solo merge task carries
"solo" so the agent re-reads the switch itself before it merges.
Run: .venv/bin/python -m unittest tests.test_solo_off_midflight"""
import unittest
from unittest import mock

from common import github, intents
from tests import test_solo_github
from tests.test_solo_review import CX, KEY, Base


class SwitchedOffDuringTheGitHubCall(Base):
    def approved(self, off_midway):
        self.enable()
        item = self.built()
        item.update(stage="approved", sha="abc")
        info = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc", "reviewDecision": ""}

        async def merge_ready(pr, repo=None):
            if off_midway:                    # someone says "@pm solo review off" while GitHub is asked
                await self.lp.act(self.room, CX, intents.SOLO_OFF)
            return True, info
        with mock.patch("common.github.merge_ready", merge_ready):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        return item

    def test_it_pauses_instead_of_merging_on_the_solo_verdict(self):
        item = self.approved(off_midway=True)
        self.assertEqual((item["stage"], item.get("no_checker"), self.tasks("merge")), ("stopped", True, []))

    def test_a_solo_merge_task_says_it_is_solo(self):
        item = self.approved(off_midway=False)
        self.assertEqual(item["stage"], "merging")
        self.assertIs(self.tasks("merge")[-1]["solo"], True)


class AgentRechecksASoloMerge(unittest.TestCase):
    run_task = test_solo_github.BridgeRechecks.run_task

    def test_refused_while_solo_review_is_off(self):
        handle, extra = self.run_task({"kind": "merge", "pr": 9, "sha": "a", "solo": True}, on=False)
        handle.assert_not_awaited()
        self.assertIn("solo review is off", extra[github.RESULT_KEY]["error"])

    def test_a_merge_checked_by_another_person_never_needs_the_switch(self):
        self.run_task({"kind": "merge", "pr": 9, "sha": "a"}, on=False)[0].assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
