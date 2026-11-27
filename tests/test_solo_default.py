"""Solo review ON by default (owner decision, docs/adr/0023-solo-review.md): a room that never set it lets the
owner's other agent check the work, another person's agent is still preferred, anyone can switch it off and an
explicit off stays off. GitHub's ruleset is never changed just because the default is on: @pm asks the room's
owner/moderator once with a 👍 (pm_solo_rules.offer_bypass) and only then sends the ruleset task.
Run: .venv/bin/python -m unittest tests.test_solo_default"""
import unittest
from types import SimpleNamespace
from unittest import mock

from common import intents, solo_review
from server import pm_solo_rules
from tests.test_solo_review import BA, BETTY, CX, CXA, CXC, KEY, PM, REPO, Base

ASK = pm_solo_rules.BYPASS_QUESTION.format(repo=REPO)


class Default(Base):
    EXPLICIT_OFF = False                     # a new room: nobody set solo review

    def refused_merge(self):
        """Default on, Autonomous: CXC approves CXA's PR, the admin merge comes back refused (no bypass yet)."""
        item = self.built()
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        info = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "headRefOid": "abc",
                "reviewDecision": "REVIEW_REQUIRED"}
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(False, info))), \
                mock.patch("common.github.run_json", mock.AsyncMock(return_value={"statusCheckRollup": []})):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        self.assertTrue(self.tasks("merge")[-1]["admin"])
        self.run_(self.lp.on_result(self.room, CXA, {"kind": "merge", "issue": 7, "pr": 9, "task_id": item["task_id"],
                                                     "error": solo_review.NO_BYPASS}))
        return item

    def test_a_new_room_is_on_and_the_owners_other_agent_reviews(self):
        self.assertTrue(self.run_(self.lp.solo_on("!r")))            # the panel and pinned status read this
        self.assertTrue(self.run_(solo_review.enabled(self.lp.client, "!r")))
        item = self.built()
        self.assertEqual((item["stage"], item["reviewer"], item["solo"]), ("reviewing", CXC, True))
        self.assertTrue(self.tasks("review")[-1]["solo"])
        self.assertEqual(self.tasks(solo_review.TASK_KIND), [])      # the default alone never touches GitHub

    def test_another_persons_agent_is_still_preferred(self):
        self.agents = [CXA, CXC, BA]
        item = self.built()
        self.assertEqual((item["reviewer"], item["solo"]), (BA, False))

    def test_flow_first_solo_merge_asks_for_the_bypass_once_and_only_a_thumb_changes_github(self):
        item = self.refused_merge()
        self.assertEqual(item["stage"], "stopped")
        self.assertEqual(self.said[-1][0], ASK)                       # the owner's own words, after the pause
        self.assertEqual(self.tasks(solo_review.TASK_KIND), [])      # nothing changed on GitHub yet
        self.assertEqual(self.recorded()["asked"], REPO)
        self.thumb(CX)
        ruleset = self.tasks(solo_review.TASK_KIND)
        self.assertEqual(len(ruleset), 1)
        self.assertEqual((ruleset[0]["repo"], ruleset[0]["on"]), (REPO, True))
        self.answer(self.job(), on=True, changed=[42], already=[])
        self.assertTrue(solo_review.bypass_ok(self.recorded(), REPO))
        self.run_(self.lp.retry(self.room, CX, 7))
        self.assertEqual(item["stage"], "approved")                   # the merge is tried again, PR kept

    def test_asked_once_a_second_refusal_doesnt_ask_again(self):
        self.refused_merge()
        self.lp.pending.pop("!r")                                     # unanswered and gone
        self.run_(self.lp.offer_bypass("!r"))
        self.assertNotIn("!r", self.lp.pending)
        self.assertEqual(sum(b == ASK for b, _, _ in self.said), 1)

    def test_any_moderator_may_answer_but_not_someone_else(self):
        self.refused_merge()
        self.thumb(BETTY)                                             # not a moderator here
        self.assertEqual(self.tasks(solo_review.TASK_KIND), [])
        with mock.patch("common.room_state.is_moderator", mock.AsyncMock(return_value=True)):
            self.thumb(BETTY)                                         # a maintainer, not the owner asked
        self.assertEqual(len(self.tasks(solo_review.TASK_KIND)), 1)

    def test_a_bypass_already_there_is_never_asked_about(self):
        self.answer_known_bypass()
        self.assertFalse(self.run_(self.lp.offer_bypass("!r", CX, again=True)))
        self.assertNotIn("!r", self.lp.pending)

    def answer_known_bypass(self):
        self.run_(self.lp.write_solo("!r", None, repo=REPO, ok=True))
        self.assertTrue(self.recorded()["default"])                   # still nobody's choice

    def test_a_repo_without_a_review_rule_is_never_asked_about(self):
        self.lp.review_rule = mock.AsyncMock(return_value=False)
        self.assertFalse(self.run_(self.lp.offer_bypass("!r")))
        self.assertNotIn("!r", self.lp.pending)

    def test_binding_a_repo_asks_at_the_next_poll_not_before(self):
        self.run_(self.lp.follow_repo("!r", None))                    # the room, before its repo
        self.assertEqual(self.lp.solo_bound("!r", CX, REPO, "This room now works on o/r."), "This room now works on o/r.")
        self.assertNotIn("!r", self.lp.pending)
        self.run_(self.lp.follow_repo("!r", REPO))
        self.assertEqual(self.said[-1][0], ASK)
        self.assertEqual(self.lp.pending["!r"]["by"], CX)
        self.assertEqual(self.tasks(solo_review.TASK_KIND), [])
        self.thumb(CX)
        self.assertEqual(len(self.tasks(solo_review.TASK_KIND)), 1)

    def test_a_thumb_after_switching_off_changes_nothing(self):
        self.refused_merge()
        asked = self.lp.pending["!r"]
        self.run_(self.lp.act(self.room, BETTY, intents.SOLO_OFF))   # drops the waiting question too
        self.lp.pending["!r"] = asked
        self.thumb(CX)
        self.assertEqual(self.tasks(solo_review.TASK_KIND), [])
        self.assertIn(pm_solo_rules.BYPASS_OFF, self.said[-1][0])

    def test_explicit_off_stays_off_also_across_a_repo_change(self):
        self.run_(self.lp.act(self.room, BETTY, intents.classify("@pm solo review off")))   # anyone, at once
        self.assertEqual((self.recorded()["on"], self.recorded()["default"]), (False, False))
        self.run_(self.lp.follow_repo("!r", REPO))
        self.lp.client.bind("o/other")
        self.run_(self.lp.follow_repo("!r", "o/other"))
        self.assertFalse(self.run_(self.lp.solo_on("!r")))
        self.assertEqual(self.built()["stage"], "stopped")            # paused for another person's check
        self.assertFalse(self.run_(self.lp.offer_bypass("!r", CX, again=True)))

    def test_allow_when_already_on_offers_the_bypass(self):
        self.run_(self.lp.act(self.room, CX, intents.classify("@pm allow solo review")))
        self.assertIn("already on", self.said[-2][0])
        self.assertEqual(self.said[-1][0], ASK)


class Reads(unittest.TestCase):
    def test_one_event_read(self):
        """mcp_build's single GET: never set is on; any other failure is off (fail closed)."""
        self.assertTrue(solo_review.event_state({"_status": 404, "errcode": "M_NOT_FOUND"}, PM)["on"])
        self.assertFalse(solo_review.event_state({"_status": 403, "errcode": "M_FORBIDDEN"}, PM)["on"])
        self.assertFalse(solo_review.event_state({"_status": 500}, PM)["on"])
        off = {"type": solo_review.SOLO_EVENT, "state_key": "", "sender": PM, "content": solo_review.content(False)}
        self.assertFalse(solo_review.event_state(off, PM)["on"])
        self.assertFalse(solo_review.state_in([{**off, "sender": CX, "content": {"on": True}}], PM)["on"])

    def test_a_bypass_counts_only_for_its_own_repo(self):
        state = solo_review.content(True, repo=REPO, ok=True)
        self.assertTrue(solo_review.bypass_ok(state, REPO))
        self.assertFalse(solo_review.bypass_ok(state, "o/other"))
        self.assertFalse(solo_review.live(solo_review.default_state(), None))   # no repo: nothing to review


if __name__ == "__main__":
    unittest.main()
