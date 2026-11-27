"""Solo steps whose safety checks landed after solo review itself go only to code that has them (GPT review of the
whole-batch fixes): a solo merge (admin or not) needs code that re-reads the switch at the merge and refuses
code-owner rules (code_report.SOLO_MERGE), adding the bypass needs code that refuses them (SOLO_ADD); taking the
bypass out again works on any code that knows the task. Run: .venv/bin/python -m unittest tests.test_solo_version_gate"""
import unittest
from unittest import mock

from common import code_report, registry, solo_review
from server import pm_solo_code
from tests.test_solo_review import CX, CXA, CXC, KEY, REPO, Base

REAL_GATE = code_report.runs_task                              # Base lets every code run everything
FIRST = code_report.TASK_SINCE[code_report.SOLO_STEP]          # solo review itself
OLD = {"commit": "a" * 40, "time": FIRST, "own": False}         # does solo checks, not the later fixes
NEW = {**OLD, "time": code_report.TASK_SINCE[code_report.SOLO_MERGE]}


class Gate(Base):
    EXPLICIT_OFF = False

    def setUp(self):
        super().setUp()
        self.codes = {"cx-agent": OLD, "cx-codex-agent": NEW}
        for target, value in (("common.code_report.runs_task", REAL_GATE),
                              ("common.registry.find", lambda owner, agent: {registry.CODE: self.codes.get(agent)}),
                              ("server.console_auto_update.update_for_task", mock.AsyncMock(return_value=True))):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def approved(self, need_review):
        item = self.built()
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        info = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc",
                "reviewDecision": "REVIEW_REQUIRED" if need_review else ""}
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(not need_review, info))), \
                mock.patch("common.github.run_json", mock.AsyncMock(return_value={"statusCheckRollup": []})):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        return item

    def test_an_older_builder_gets_no_solo_merge_of_either_kind(self):
        for need_review in (True, False):
            with self.subTest(admin=need_review):
                item = self.approved(need_review)
                self.assertEqual((item["stage"], item.get("resume"), self.tasks("merge")), ("stopped", "approved", []))
                self.assertIn("cx-agent needs an update first", self.said[-1][0])

    def test_an_updated_builder_merges(self):
        self.codes["cx-agent"] = NEW
        self.approved(False)
        self.assertIs(self.tasks("merge")[-1]["solo"], True)

    def test_adding_the_bypass_needs_the_update_but_taking_it_out_doesnt(self):
        self.codes["cx-codex-agent"] = OLD    # every agent of the owner's on the older code
        self.run_(self.lp.ask_ruleset(self.room, CX, True, [], REPO))
        self.assertEqual(self.tasks(solo_review.TASK_KIND), [])
        self.assertIn("needs an update first", self.said[-1][0])
        self.run_(self.lp.ask_ruleset(self.room, CX, False, [42], REPO))
        self.assertEqual(self.tasks(solo_review.TASK_KIND)[-1]["rulesets"], [42])

    def saved_merge(self):
        """A solo merge sent while the builder ran updated code, which it no longer does."""
        self.codes["cx-agent"] = NEW
        item = self.approved(False)
        self.codes["cx-agent"] = OLD
        self.said.clear()
        return item

    def test_a_held_or_resent_solo_merge_is_gated_too(self):
        """GPT review round 2: held tasks and the watchdog's resend sent the saved task without the gate."""
        from server import loop_progress
        item = self.saved_merge()
        item["sent_at"] = 1                   # no answer for long: the watchdog would resend it
        with mock.patch.object(loop_progress, "STEP_TIMEOUT_S", 1):
            self.assertTrue(self.run_(self.lp.watchdog("!r", KEY, item)))
        self.assertEqual((item["stage"], self.tasks("merge")), ("stopped", []))
        self.assertIn("cx-agent needs an update first", self.said[-1][0])
        item.update(stage="merging", held=True)    # decided while the room was stopped, sent at the next tick
        self.said.clear()
        self.run_(self.lp.send_held("!r", KEY, item))
        self.assertEqual((item["stage"], "held" in item, self.tasks("merge")), ("stopped", False, []))

    def test_the_gates_by_reported_time(self):
        loop = pm_solo_code.SoloCode()
        self.assertTrue(loop.runs_solo(CXA))                                 # solo checks: still fine
        self.assertFalse(loop.runs_solo(CXA, code_report.SOLO_MERGE))
        self.assertTrue(loop.runs_solo(CXC, code_report.SOLO_MERGE))


if __name__ == "__main__":
    unittest.main()
