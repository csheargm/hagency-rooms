"""Solo review (docs/adr/0023-solo-review.md, server/pm_solo.py): the room owner, testing alone with several of
their OWN agents, lets another of them check the work. Off by default; on only by a moderator after a 👍; off
at once by anyone; never the builder itself; a paused item goes on with "@pm retry #N".
Run: .venv/bin/python -m unittest tests.test_solo_review"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetStateResponse

from common import intents, pm_actions, solo_review
from server import pm_solo, routing
from server.loop import Loop

S = "bridge.localhost"
CX, BETTY = f"@cx:{S}", f"@betty:{S}"
CXA, CXC, BA = f"@cx-agent:{S}", f"@cx-codex-agent:{S}", f"@betty-agent:{S}"
OWNERS = {CXA: CX, CXC: CX, BA: BETTY}
PM = f"@pm:{S}"
REPO, KEY = "o/r", "o/r#7"


class FakeClient:
    """Room state as the server keeps it: what @pm writes, read back."""
    user_id = PM

    def __init__(self):
        self.events: dict[str, dict] = {}
        self.room_send = mock.AsyncMock()

    async def room_get_state(self, rid):
        return RoomGetStateResponse(list(self.events.values()), rid)

    async def room_put_state(self, rid, kind, content, state_key=""):
        self.events[kind] = {"type": kind, "state_key": "", "sender": PM, "content": content}
        return SimpleNamespace(event_id="$s")


class Base(unittest.TestCase):
    def setUp(self):
        lp = self.lp = Loop.__new__(Loop)
        lp.client, lp.generation, lp.pending, lp.state, lp.room_repos = FakeClient(), {}, {}, {}, {"!r": REPO}
        lp.save, lp.lock, lp.agent_online = (lambda: None), asyncio.Lock(), (lambda a: True)
        lp.level = mock.AsyncMock(return_value="assist")
        lp.key_for_result = lambda rid, result: KEY
        lp.pr_is_builders = mock.AsyncMock(return_value=True)
        self.agents = [CXA, CXC]
        lp.agents_in = lambda room: list(self.agents)
        self.said: list[tuple] = []

        async def say(rid, body, mention=None, task=None, **kw):
            self.said.append((body, mention, task))
            return f"$e{len(self.said)}"
        lp.say = say
        self.room = SimpleNamespace(room_id="!r")
        for target, value in (("server.ownership.owner_id", OWNERS.get),
                              ("server.ownership.own_agent_id", {CX: CXA, BETTY: BA}.get),
                              ("common.room_state.is_moderator", mock.AsyncMock(side_effect=lambda c, r, u: u == CX)),
                              ("common.room_state.owners", mock.AsyncMock(return_value=[CX])),
                              ("server.pm_solo_rules.cannot", lambda owner, agent: None)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_(self, coro):
        return asyncio.run(coro)

    def tasks(self, kind=None):
        return [t for _, _, t in self.said if t and (kind is None or t["kind"] == kind)]

    def built(self):
        """CXA reports its PR: the review step is handed out (or the item pauses)."""
        self.lp.state[KEY] = {"room": "!r", "repo": REPO, "stage": "building", "builder": CXA, "task_id": "B",
                              "task_ids": ["B"], "title": "t"}
        self.run_(self.lp.on_result(self.room, CXA, {"kind": "build", "issue": 7, "pr": 9, "task_id": "B"}))
        return self.lp.state[KEY]

    def job(self) -> dict:
        """The latest ruleset ask, with its task id."""
        task_id, job = list(self.lp.solo_jobs().items())[-1]
        return {**job, "task_id": task_id}

    def answer(self, job, **result):
        self.run_(self.lp.on_result(self.room, job["agent"], {"kind": solo_review.TASK_KIND, "task_id": job["task_id"],
                                                              **result}))

    def recorded(self):
        return solo_review.state_in(list(self.lp.client.events.values()), PM)

    def enable(self, who=CX):
        self.run_(self.lp.act(self.room, who, intents.classify("@pm allow solo review")))
        asked = self.lp.pending.get("!r")
        if asked:
            self.run_(self.lp.on_reaction(self.room, SimpleNamespace(sender=who, key="👍", reacts_to=asked["event_id"])))


class Room(Base):
    def test_off_by_default_a_solo_owners_build_pauses_and_says_how_to_go_on(self):
        item = self.built()
        self.assertEqual(item["stage"], "stopped")
        self.assertTrue(item["no_checker"])
        paused = self.said[-1][0]
        self.assertIn("no second person's agent", paused)
        self.assertIn("@pm allow solo review", paused)
        self.assertIn("Solo review in the Hagency panel", paused)
        self.assertEqual(self.tasks("review"), [])

    def test_enabled_with_a_thumb_the_owners_other_agent_reviews_and_its_verdict_counts(self):
        self.enable()
        self.assertTrue(solo_review.state_in(list(self.lp.client.events.values()), PM)["on"])
        ruleset = self.tasks(solo_review.TASK_KIND)
        self.assertEqual(len(ruleset), 1)                 # the owner's agent is asked to update GitHub's rule
        self.assertEqual((ruleset[0]["repo"], ruleset[0]["on"]), (REPO, True))
        item = self.built()
        review = self.tasks("review")[-1]
        self.assertEqual(item["reviewer"], CXC)            # never the builder itself
        self.assertTrue(review["solo"])                    # the verdict goes on GitHub as a comment
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        self.assertEqual(item["stage"], "approved")

    def test_only_a_moderator_can_switch_it_on(self):
        self.enable(who=BETTY)
        self.assertNotIn("!r", self.lp.pending)
        self.assertIn("Only the room owner or a maintainer", self.said[-1][0])
        self.assertFalse(self.run_(self.lp.solo_on("!r")))
        self.assertEqual(self.built()["stage"], "stopped")

    def test_a_thumb_from_someone_else_doesnt_switch_it_on(self):
        self.run_(self.lp.act(self.room, CX, intents.SOLO_ON))
        asked = self.lp.pending["!r"]
        self.run_(self.lp.on_reaction(self.room, SimpleNamespace(sender=BETTY, key="👍", reacts_to=asked["event_id"])))
        self.assertFalse(self.run_(self.lp.solo_on("!r")))

    def test_the_builder_never_checks_its_own_work(self):
        self.enable()
        self.agents = [CXA]                                # cx's only agent here is the builder
        item = self.built()
        self.assertEqual(item["stage"], "stopped")
        self.assertIn("none of the builder's owner's other agents", self.said[-1][0])
        self.assertEqual(routing.own_checkers([CXA, CXC, BA], CXA, OWNERS.get), [CXC])
        self.assertEqual(routing.own_checkers([CXA, "@odd-agent:x"], CXA, OWNERS.get), [])
        self.assertEqual(routing.own_checkers([CXA, CXC], "@odd-agent:x", OWNERS.get), [])

    def test_another_persons_agent_is_still_preferred(self):
        self.enable()
        self.agents = [CXA, CXC, BA]
        item = self.built()
        self.assertEqual((item["reviewer"], item["solo"]), (BA, False))
        self.assertNotIn("solo", self.tasks("review")[-1])

    def test_paused_then_enabled_retry_goes_back_to_the_review_with_the_pr_kept(self):
        item = self.built()
        self.assertEqual(item["stage"], "stopped")
        self.enable()
        self.assertIn('say "@pm retry #7"', " ".join(b for b, _, _ in self.said))
        self.run_(self.lp.retry(self.room, CX, 7))
        self.assertIs(self.lp.state[KEY], item)            # not requeued: the open PR #9 is kept
        self.assertEqual((item["stage"], item["pr"], item["reviewer"]), ("reviewing", 9, CXC))
        self.assertNotIn("no_checker", item)
        self.assertTrue(self.tasks("review")[-1]["solo"])

    def test_a_solo_check_after_switching_off_pauses_instead_of_counting(self):
        self.enable()
        item = self.built()
        self.run_(self.lp.act(self.room, BETTY, intents.classify("@pm solo review off")))   # anyone, at once
        self.assertFalse(self.run_(self.lp.solo_on("!r")))
        self.run_(self.lp.on_result(self.room, CXC, {"kind": "review", "issue": 7, "pr": 9, "sha": "abc",
                                                     "verdict": "approve", "task_id": item["task_id"]}))
        self.assertEqual(item["stage"], "stopped")
        self.assertIn("switched off", self.said[-1][0])

    def test_switching_off_takes_out_only_the_bypass_hagency_added(self):
        self.enable()
        job = self.job()
        self.run_(self.lp.on_result(self.room, job["agent"], {"kind": solo_review.TASK_KIND, "on": True, "changed": [42],
                                                              "already": [], "task_id": job["task_id"]}))
        self.assertEqual(solo_review.state_in(list(self.lp.client.events.values()), PM)["bypass_added"], [42])
        self.assertIn("I'll take that out again", self.said[-1][0])
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))
        off = self.tasks(solo_review.TASK_KIND)[-1]
        self.assertEqual((off["on"], off["rulesets"]), (False, [42]))

    def test_an_existing_bypass_counts_as_done_and_is_never_taken_out(self):
        self.enable()
        job = self.job()
        self.run_(self.lp.on_result(self.room, job["agent"], {"kind": solo_review.TASK_KIND, "on": True, "changed": [],
                                                              "already": [42], "task_id": job["task_id"]}))
        self.assertIn("already lets repo admins", self.said[-1][0])
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))
        self.assertEqual(len(self.tasks(solo_review.TASK_KIND)), 1)   # nothing of ours to take out

    def test_a_failed_ruleset_update_is_said_with_what_to_change(self):
        self.enable()
        job = self.job()
        self.run_(self.lp.on_result(self.room, job["agent"], {"kind": solo_review.TASK_KIND, "error": "HTTP 403",
                                                              "task_id": job["task_id"]}))
        self.assertIn("couldn't update GitHub's review rule", self.said[-1][0])
        self.assertIn("Bypass list", self.said[-1][0])
        self.assertIn("Repository admin", self.said[-1][0])

    def test_a_result_from_anyone_but_the_asked_agent_is_ignored(self):
        self.enable()
        job = self.job()
        before = len(self.said)
        self.run_(self.lp.on_result(self.room, CXC if job["agent"] != CXC else CXA,
                                    {"kind": solo_review.TASK_KIND, "changed": [1], "task_id": job["task_id"]}))
        self.assertEqual(len(self.said), before)

    def test_switched_off_while_the_bypass_was_added_it_is_taken_out_and_never_turns_solo_back_on(self):
        """GPT review: a late "added" answer wrote on=True back from a stale read, and left the bypass in place."""
        self.enable()
        job = self.job()
        self.run_(self.lp.act(self.room, BETTY, intents.SOLO_OFF))   # nothing recorded yet: no removal asked
        self.answer(job, on=True, changed=[42], already=[])
        self.assertEqual((self.recorded()["on"], self.recorded()["bypass_added"]), (False, [42]))
        removal = self.tasks(solo_review.TASK_KIND)[-1]
        self.assertEqual((removal["on"], removal["rulesets"]), (False, [42]))
        self.answer(self.job(), on=False, changed=[42], already=[])
        self.assertEqual(self.recorded()["bypass_added"], [])

    def test_switched_on_again_while_it_was_taken_out_it_is_put_back(self):
        self.enable()
        self.answer(self.job(), on=True, changed=[42], already=[])
        self.run_(self.lp.act(self.room, CX, intents.SOLO_OFF))
        removal = self.job()
        self.run_(self.lp.solo_from_panel(self.room, CX, "on"))
        asks = len(self.tasks(solo_review.TASK_KIND))
        self.answer(removal, on=False, changed=[42], already=[])
        self.assertEqual(len(self.tasks(solo_review.TASK_KIND)), asks + 1)
        self.assertTrue(self.tasks(solo_review.TASK_KIND)[-1]["on"])

    def test_a_partial_failure_still_records_what_was_changed(self):
        self.enable()
        self.answer(self.job(), on=True, changed=[42], already=[], error="HTTP 403 on 43")
        self.assertEqual(self.recorded()["bypass_added"], [42])
        self.assertIn("couldn't update", self.said[-1][0])

    def test_retry_when_the_step_cant_be_announced_stays_paused(self):
        item = self.built()
        self.enable()

        async def broken(*a, **k):
            raise RuntimeError("Matrix refused")
        self.lp.say = broken
        with self.assertRaises(RuntimeError):
            self.run_(self.lp.retry(self.room, CX, 7))
        self.assertEqual((item["stage"], item["no_checker"]), ("stopped", True))

    def test_from_the_panel_on_needs_a_moderator_off_does_not(self):
        self.assertIn("only the room owner", self.run_(self.lp.solo_from_panel(self.room, BETTY, "on")))
        self.assertEqual(self.run_(self.lp.solo_from_panel(self.room, CX, "on")), "ok")
        self.assertTrue(self.run_(self.lp.solo_on("!r")))
        self.assertEqual(self.run_(self.lp.solo_from_panel(self.room, BETTY, "off")), "ok")
        self.assertFalse(self.run_(self.lp.solo_on("!r")))

    def test_the_owner_ai_may_pick_it_but_on_is_still_asked(self):
        self.assertEqual(pm_actions.INTENT_OF["solo_review_on"], intents.SOLO_ON)
        self.run_(self.lp.apply(self.room, CX, "solo_review_on", "Sure.", "no need for a second person", None, 0, True))
        self.assertEqual(self.lp.pending["!r"]["kind"], "solo")
        self.assertFalse(self.run_(self.lp.solo_on("!r")))   # only the 👍 switches it on


class Merging(Base):
    """GitHub never counts an approval from the PR's own account: in solo mode the merge goes past the review
    rule as the repo's admin (Autonomous), or a person is told how (Assist)."""

    def approved(self, lvl):
        self.enable()
        item = self.built()
        item.update(stage="approved", sha="abc")
        info = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "headRefOid": "abc",
                "reviewDecision": "REVIEW_REQUIRED"}
        checks = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(False, info))), \
                mock.patch("common.github.run_json", mock.AsyncMock(return_value=checks)):
            self.run_(self.lp.advance("!r", KEY, item, lvl))
        return item

    def test_autonomous_merges_as_admin(self):
        item = self.approved("autonomous")
        merge = self.tasks("merge")[-1]
        self.assertEqual((item["stage"], merge["admin"], merge["sha"]), ("merging", True, "abc"))

    def test_assist_tells_the_person_how_to_merge(self):
        item = self.approved("assist")
        self.assertEqual(item["stage"], "ready")
        self.assertIn("Merge without waiting for requirements", self.said[-1][0])

    def test_a_red_check_never_merges(self):
        self.enable()
        item = self.built()
        item.update(stage="approved", sha="abc")
        info = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefOid": "abc", "reviewDecision": "REVIEW_REQUIRED"}
        with mock.patch("common.github.merge_ready", mock.AsyncMock(return_value=(False, info))), \
                mock.patch("common.github.run_json", mock.AsyncMock(return_value={"statusCheckRollup": [
                    {"status": "COMPLETED", "conclusion": "FAILURE"}]})):
            self.run_(self.lp.advance("!r", KEY, item, "autonomous"))
        self.assertEqual(item["stage"], "stopped")
        self.assertEqual(self.tasks("merge"), [])


class Words(unittest.TestCase):
    def test_phrases(self):
        for text in ("@pm allow solo review", "solo review on", "please enable solo review", "turn on solo-review",
                     "let my other agents review each other"):
            self.assertEqual(intents.classify(text), intents.SOLO_ON, text)
        for text in ("@pm solo review off", "turn off solo review", "disallow solo review", "don't allow solo review",
                     "no solo review", "stop solo review"):
            self.assertEqual(intents.classify(text), intents.SOLO_OFF, text)
        self.assertEqual(intents.classify("stop everything and turn off solo review"), intents.OFF)   # stop wins
        self.assertEqual(intents.classify("stop everything, solo review off"), intents.OFF)          # GPT review
        self.assertNotEqual(intents.classify("turn off solo reviewer"), intents.SOLO_OFF)
        self.assertEqual(intents.classify("@pm stop"), intents.OFF)
        self.assertEqual(intents.classify("turn it off"), intents.OFF)

    def test_state_counts_only_if_pm_wrote_it(self):
        mine = {"type": solo_review.SOLO_EVENT, "state_key": "", "sender": PM, "content": {"on": True}}
        forged = {**mine, "sender": CX}
        self.assertTrue(solo_review.state_in([mine], PM)["on"])
        self.assertFalse(solo_review.state_in([forged], PM)["on"])
        self.assertFalse(solo_review.state_in([], PM)["on"])

    def test_checks(self):
        self.assertEqual(solo_review.checks_state([]), "passed")
        self.assertEqual(solo_review.checks_state([{"state": "SUCCESS"}, {"conclusion": "SKIPPED"}]), "passed")
        self.assertEqual(solo_review.checks_state([{"status": "IN_PROGRESS", "conclusion": None}]), "pending")
        self.assertEqual(solo_review.checks_state([{"status": "COMPLETED", "conclusion": "FAILURE"}]), "failed")
        self.assertEqual(solo_review.checks_state(None), "failed")      # unknown: never merged past the rule

    def test_check_task(self):
        self.assertEqual(routing.check_task({"stage": "reviewing", "pr": 9, "solo": True}, 7),
                         {"kind": "review", "issue": 7, "pr": 9, "solo": True})
        self.assertEqual(routing.check_task({"stage": "verifying"}, 7), {"kind": "verify", "issue": 7})

    def test_hint_names_the_issue(self):
        self.assertIn("@pm retry #7", pm_solo.HINT.format(n=7))


if __name__ == "__main__":
    unittest.main()
