"""Solo review switched off while @pm asks GitHub whether a solo-checked PR may merge (GPT whole-batch review):
the regular merge (no GitHub approval needed) never goes out on the solo verdict, and a solo merge task carries
"solo" so the agent re-reads the switch itself before it merges.
Run: .venv/bin/python -m unittest tests.test_solo_off_midflight"""
import asyncio
import unittest
from unittest import mock

from common import github, intents
from tests import test_solo_github
from tests.test_solo_review import CX, CXA, KEY, Base


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


class RefusedSoloMerge(Base):
    def test_a_merge_the_agent_refused_after_a_switch_off_retries_to_another_persons_check(self):
        """GPT final review: the generic steps said retry, but a retry refuses an open PR."""
        self.enable()
        item = self.built()
        item.update(stage="approved", sha="abc")
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(True, {"headRefOid": "abc"}))):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))            # after the merge went out
        self.run_(self.lp.on_result(self.room, CXA, {"kind": "merge", "issue": 7, "pr": 9, "task_id": item["task_id"],
                                                     "error": "solo review is off in this room"}))
        self.assertEqual((item["stage"], item.get("resume")), ("stopped", "approved"))
        self.assertIn("@pm retry #7", self.said[-1][0])
        self.run_(self.lp.retry(self.room, CX, 7))
        self.assertEqual(item["stage"], "approved")                       # the PR kept, the merge decided again
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(True, {"headRefOid": "abc"}))):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        self.assertEqual((item["stage"], item.get("no_checker")), ("stopped", True))   # waits for another person


class AgentRechecksASoloMerge(unittest.TestCase):
    run_task = test_solo_github.BridgeRechecks.run_task

    def test_refused_while_solo_review_is_off(self):
        handle, extra = self.run_task({"kind": "merge", "pr": 9, "sha": "a", "solo": True}, on=False)
        handle.assert_not_awaited()
        self.assertIn("solo review is off", extra[github.RESULT_KEY]["error"])

    def test_a_merge_checked_by_another_person_never_needs_the_switch(self):
        self.run_task({"kind": "merge", "pr": 9, "sha": "a"}, on=False)[0].assert_awaited_once()


class AgentMergesOnlyWhileStillOn(unittest.TestCase):
    """GPT review: the bridge read the switch before the task, then the merge awaited GitHub and went ahead."""

    def merge(self, still):
        from adapters.agents import tasks
        ran = []

        async def run(args, agent=None, **kw):
            ran.append(args)
        with mock.patch.object(tasks.github, "merge_ready", mock.AsyncMock(return_value=(True, {"headRefOid": "a"}))), \
                mock.patch.object(tasks, "run", run):
            coro = tasks.merge("cx-agent", "codex", None, {"repo": "o/r", "issue": 7, "pr": 9, "sha": "a", "solo": True,
                                                           **({"_still": still} if still else {})})
            try:
                asyncio.run(coro)
            except tasks.TaskError as e:
                return ran, str(e)
        return ran, None

    def test_switched_off_while_github_was_asked_it_doesnt_merge(self):
        ran, err = self.merge(mock.AsyncMock(return_value=False))
        self.assertEqual(ran, [])
        self.assertIn("switched off", err)

    def test_still_on_or_not_solo_it_merges(self):
        for still in (mock.AsyncMock(return_value=True), None):
            ran, err = self.merge(still)
            self.assertEqual((err, ran[-1][:2]), (None, ["pr", "merge"]))

    def test_the_bridge_hands_a_solo_merge_the_recheck(self):
        handle = test_solo_github.BridgeRechecks.run_task(self, {"kind": "merge", "pr": 9, "sha": "a", "solo": True},
                                                          on=True)[0]
        self.assertTrue(callable(handle.call_args.args[3].get("_still")))


if __name__ == "__main__":
    unittest.main()
