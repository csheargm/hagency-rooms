"""A paused issue whose agent opened a PR that was never recorded (the agent crashed after pushing):
"@pm retry #N" and a switch to autonomous re-queued it, and a second agent built a duplicate PR.
Both now find the open PR on the agent's branch for the issue first (server/pm_paused.py).
Run: .venv/bin/python -m unittest tests.test_unrecorded_pr"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from common import github
from common import room_state
from server import pm_paused
from server.loop import Loop
from server.loop_progress import issue_key
from tests.test_github_app import AppCase, REPO as APP_REPO

REPO, ANNA = "your-name/hagency-hello", "@anna:bridge.localhost"


def pr(number, branch, fork=False):
    return {"number": number, "headRefName": branch, "isCrossRepository": fork}


def paused(*numbers) -> dict:     # no PR recorded: the agent crashed before it could report one
    return {issue_key(REPO, n): {"room": "!r", "repo": REPO, "stage": "stopped", "title": f"Issue {n}",
                                 "why": "cx-agent couldn't build it", "ended": time.time()} for n in numbers}


class UnrecordedPR(unittest.TestCase):
    def setUp(self):
        for target, name, value in ((room_state, "is_moderator", mock.AsyncMock(return_value=True)),
                                    (room_state, "repo_of", mock.AsyncMock(return_value=REPO)),
                                    (Loop, "level", mock.AsyncMock(return_value="autonomous")),
                                    (pm_paused, "_open_numbers", mock.AsyncMock(return_value=None))):   # issues: all open
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos, self.lp.state, self.lp.lock, self.lp.save = {"!r": REPO}, paused(7, 9), asyncio.Lock(), mock.Mock()
        self.lp.client, self.lp.say = SimpleNamespace(rooms={}), mock.AsyncMock()
        self.lp.pr_open = mock.AsyncMock(return_value=False)
        self.room = SimpleNamespace(room_id="!r")
        self.listed: list = []
        self.heads: dict = {}             # a branch asked by name → GitHub's answer for it
        self.asked: list = []

    def github(self, prs):
        self.listed = prs
        return mock.patch.object(github, "run_json", mock.AsyncMock(side_effect=self.answer))

    async def answer(self, args, agent=None):
        assert args[:2] == ["pr", "list"] and args[args.index("-R") + 1] == REPO, args
        self.asked.append(args)
        head = args[args.index("--head") + 1] if "--head" in args else None
        listed = self.heads.get(head, []) if head else self.listed
        if isinstance(listed, Exception):
            raise listed
        return listed

    def retry(self, n):
        asyncio.run(self.lp.retry(self.room, ANNA, n))
        return self.lp.say.call_args.args[1]

    def test_retry_finds_the_unrecorded_pr(self):
        """The bug: nothing recorded, so the guard saw nothing and #9 was built a second time."""
        with self.github([pr(21, "agent/cx-agent/issue-9")]):
            said = self.retry(9)
        self.assertEqual(said, 'PR #21 for #9 is still open. Merge or close it on GitHub, then say "@pm retry #9" again.')
        self.assertIn(issue_key(REPO, 9), self.lp.state)

    def test_the_switch_to_autonomous_finds_it_too(self):
        with self.github([pr(21, "agent/cc-agent/issue-7")]):
            said = asyncio.run(pm_paused.retry_all(self.lp, "!r"))
        self.assertIn("I put #9 back in the queue", said)
        self.assertIn('#7 stays paused: its PR #21 may still be open. Merge or close it on GitHub, then say '
                      '"@pm retry #7".', said)
        self.assertEqual([issue_key(REPO, 7)], list(self.lp.state))

    def test_unrelated_prs_dont_block(self):
        others = [pr(30, "agent/cx-agent/issue-19"), pr(31, "agent/cx-agent/issue-9-old"), pr(32, "fix-9"),
                  pr(33, "agent/cx-agent/issue-9", fork=True), pr(34, "someone/agent/cx-agent/issue-9")]
        with self.github(others):
            self.assertIn("back in the queue", self.retry(9))
            self.assertIn("I put #7 back in the queue", asyncio.run(pm_paused.retry_all(self.lp, "!r")))
        self.assertEqual({}, self.lp.state)

    def test_github_unreachable_means_no_retry_on_either_path(self):
        with self.github(github.GitHubError("gh pr list failed: timeout")):
            said = self.retry(9)
            self.assertIn("GitHub couldn't tell me whether #9 already has an open PR, so it stays paused", said)
            said = asyncio.run(pm_paused.retry_all(self.lp, "!r"))
        self.assertIn("#7 stays paused: GitHub couldn't tell me whether it has an open PR", said)
        self.assertEqual(2, len(self.lp.state))

    def test_a_strange_answer_counts_as_unreachable(self):
        with self.github({"message": "rate limited"}):
            self.assertIn("GitHub couldn't tell me", self.retry(9))
        self.assertIn(issue_key(REPO, 9), self.lp.state)

    def test_the_recorded_pr_still_comes_first(self):
        self.lp.state[issue_key(REPO, 9)]["pr"] = 14
        self.lp.pr_open = mock.AsyncMock(return_value=True)
        with self.github([pr(21, "agent/cx-agent/issue-9")]):
            self.assertIn("PR #14 for #9 is still open", self.retry(9))

    def test_a_full_list_cant_tell(self):
        """GPT review: the issue's PR may be past the last one listed, so not finding it proves nothing."""
        with self.github([pr(n, f"agent/cx-agent/issue-{1000 + n}") for n in range(pm_paused.MAX_OPEN_PRS)]):
            self.assertIn("GitHub couldn't tell me", self.retry(9))
        self.assertIn(issue_key(REPO, 9), self.lp.state)

    def test_github_is_asked_outside_the_lock(self):
        """GPT review: a retry's GitHub calls held up every room while they waited."""
        async def answer(args, agent=None):
            self.assertFalse(self.lp.lock.locked())
            return []
        with mock.patch.object(github, "run_json", mock.AsyncMock(side_effect=answer)):
            self.assertIn("back in the queue", self.retry(9))

    def test_a_pause_that_changed_meanwhile_is_left_alone(self):
        async def answer(args, agent=None):
            self.lp.state[issue_key(REPO, 9)]["stage"] = "building"      # the loop moved it meanwhile
            return []
        with mock.patch.object(github, "run_json", mock.AsyncMock(side_effect=answer)):
            self.assertIn("isn't paused anymore", self.retry(9))
        self.assertEqual("building", self.lp.state[issue_key(REPO, 9)]["stage"])

    def test_the_builders_branch_is_asked_by_name(self):
        """GPT review: a paged list can skip a PR when another closes between pages; the builder's own
        branch is then asked by name."""
        self.lp.state[issue_key(REPO, 9)]["builder"] = "@cx-agent:bridge.localhost"
        self.heads["agent/cx-agent/issue-9"] = [pr(21, "agent/cx-agent/issue-9")]
        with self.github([]):
            self.assertIn("PR #21 for #9 is still open", self.retry(9))
        self.heads["agent/cx-agent/issue-9"] = [pr(22, "agent/cx-agent/issue-9", fork=True)]
        with self.github([]):
            self.assertIn("back in the queue", self.retry(9))

    def test_switching_with_nothing_paused_asks_github_nothing(self):
        self.lp.state = {}
        with self.github([pr(21, "agent/cx-agent/issue-9")]):
            self.assertEqual("", asyncio.run(pm_paused.retry_all(self.lp, "!r")))
        self.assertEqual([], self.asked)


class ListedThroughTheApp(AppCase):
    """The public server reads GitHub through the App: "gh pr list" has the same shape there."""
    def raw(self, n, ref, head_repo=APP_REPO, state="open"):
        return {"number": n, "title": f"t{n}", "state": state,
                "head": {"ref": ref, "repo": {"full_name": head_repo} if head_repo else None}}

    def test_open_prs_with_branch_and_fork(self):
        self.fake.pulls[APP_REPO] = [self.raw(1, "agent/x/issue-4"), self.raw(2, "agent/x/issue-5", "eve/hello"),
                                     self.raw(3, "agent/x/issue-6", None), self.raw(4, "agent/x/issue-8", state="closed")]
        self.assertEqual(asyncio.run(pm_paused.agent_prs(APP_REPO, {5: "x"})), {4: 1})
        out = asyncio.run(github.run_json(["pr", "list", "-R", APP_REPO, "--json", "number,isCrossRepository"]))
        self.assertEqual(out, [{"number": 1, "isCrossRepository": False}, {"number": 2, "isCrossRepository": True},
                               {"number": 3, "isCrossRepository": True}])

    def test_a_named_branch_is_this_repos_own(self):
        self.fake.pulls[APP_REPO] = [self.raw(1, "agent/x/issue-4", "eve/hello"), self.raw(2, "agent/x/issue-4")]
        out = asyncio.run(github.run_json(["pr", "list", "-R", APP_REPO, "--head", "agent/x/issue-4", "--json", "number"]))
        self.assertEqual(out, [{"number": 2}])

    def test_only_open_prs_are_listed(self):
        with self.assertRaises(github.GitHubError):
            asyncio.run(github.run_json(["pr", "list", "-R", APP_REPO, "--state", "all", "--json", "number"]))


if __name__ == "__main__":
    unittest.main()
