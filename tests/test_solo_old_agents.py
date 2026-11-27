"""Solo review on by default, with agents on older code (server/pm_solo_code.py, review of PR #74): code from
before the default reads only an explicit @pm event, on and for the room's repo, so @pm writes it out before
an agent re-checks the switch; code from before solo review itself gets "needs an update first", never a solo
step it would refuse or not know.
Run: .venv/bin/python -m unittest tests.test_solo_old_agents"""
import unittest
from unittest import mock

from common import code_report, intents, registry, solo_review
from server import pm_solo_code
from tests.test_solo_review import CX, CXA, CXC, KEY, PM, REPO, Base


def old_reader_on(events: list, bound: str) -> bool:
    """The switch as agent code from before PR #74 read it (its state_in + live): an explicit @pm event, on,
    for the bound repo; nothing else."""
    for ev in events:
        if ev.get("type") == solo_review.SOLO_EVENT and ev.get("state_key") == "" and ev.get("sender") == PM:
            c = ev.get("content") or {}
            return c.get("on") is True and c.get("repo") == bound
    return False


class OldAgents(Base):
    EXPLICIT_OFF = False                     # a new room: on by default

    def events(self):
        return list(self.lp.client.events.values())

    def approve_and_advance(self):
        item = self.built()
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        info = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc", "reviewDecision": "REVIEW_REQUIRED"}
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(False, info))), \
                mock.patch("common.github.run_json", mock.AsyncMock(return_value={"statusCheckRollup": []})):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        return item

    def test_the_default_is_written_out_before_an_admin_merge(self):
        self.assertFalse(old_reader_on(self.events(), REPO))        # never set: older code reads "off"
        item = self.approve_and_advance()
        self.assertTrue(self.tasks("merge")[-1]["admin"])
        self.assertEqual(item["stage"], "merging")
        self.assertTrue(old_reader_on(self.events(), REPO))         # so the agent doesn't refuse it
        self.assertTrue(self.recorded()["default"])                 # still nobody's choice

    def test_switching_on_records_the_rooms_repo(self):
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))
        self.run_(self.lp.solo_from_panel(self.room, CX, "on"))
        self.assertTrue(old_reader_on(self.events(), REPO))

    def test_off_is_never_written_as_on(self):
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))
        self.assertIsNone(self.run_(self.lp.record_on("!r")))
        self.assertFalse(old_reader_on(self.events(), REPO))

    def test_the_ruleset_task_goes_out_with_the_switch_written(self):
        self.run_(self.lp.ask_ruleset(self.room, CX, True, [], REPO))
        self.assertEqual(len(self.tasks(solo_review.TASK_KIND)), 1)
        self.assertTrue(old_reader_on(self.events(), REPO))

    def test_a_builder_on_code_from_before_solo_review_needs_an_update_not_a_contradictory_pause(self):
        with mock.patch("server.console_auto_update.update_for_task", mock.AsyncMock(return_value=True)) as asked:
            self.lp.runs_solo = lambda a: a != CXA                    # only the builder is old
            item = self.approve_and_advance()
        self.assertEqual(self.tasks("merge"), [])
        self.assertEqual(item["stage"], "stopped")
        said = self.said[-1][0]
        self.assertIn("cx-agent needs an update first", said)
        self.assertIn("@pm retry #7", said)
        self.assertNotIn("solo review is off", said)
        asked.assert_awaited()
        self.run_(self.lp.retry(self.room, CX, 7))
        self.assertEqual(item["stage"], "approved")                 # the merge is tried again, PR kept

    def test_a_checker_on_old_code_gets_no_solo_check(self):
        with mock.patch("server.console_auto_update.update_for_task", mock.AsyncMock(return_value=False)):
            self.lp.runs_solo = lambda a: a != CXC
            item = self.built()
        self.assertEqual(self.tasks("review"), [])
        self.assertEqual(item["stage"], "stopped")
        self.assertIn(pm_solo_code.update_line("cx-codex-agent", False, 7), self.said[-1][0])
        self.assertNotIn("old_checker", item)



class Gate(unittest.TestCase):
    def test_an_agents_reported_code_decides(self):
        since = code_report.TASK_SINCE[code_report.SOLO_STEP]
        loop = pm_solo_code.SoloCode()
        for code, runs in ((None, False), ({"time": since - 1}, False), ({"time": since}, True)):
            with mock.patch("server.ownership.owner_id", lambda a: CX), \
                    mock.patch("common.registry.find", lambda owner, agent: {registry.CODE: code}):
                self.assertEqual(loop.runs_solo(CXA), runs, code)


if __name__ == "__main__":
    unittest.main()
