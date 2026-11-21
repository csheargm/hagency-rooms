"""Unit tests: the loop never waits forever. Run: .venv/bin/python -m unittest tests.test_loop_watchdog"""
import asyncio
import time
import unittest
from unittest import mock

from common import github
from common import room_state
from server import loop_progress
from server.loop import Loop

AGENT = "@b-agent:x"


class Watchdog(unittest.TestCase):
    def setUp(self):
        for name, value in (("bound", True), ("repo_of", "your-name/hagency-hello")):   # a project room
            _p = mock.patch.object(room_state, name, mock.AsyncMock(return_value=value))
            _p.start()
            self.addCleanup(_p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos = {"!r": "your-name/hagency-hello"}
        self.lp.level = mock.AsyncMock(return_value="autonomous")   # dispatch checks the mode
        self.lp.state = {}
        self.lp.save = lambda: None
        self.lp.say = mock.AsyncMock()
        self.lp.open_issues = {}
        self.lp.boards, self.lp.save_boards = {}, lambda: None
        owners = mock.patch("server.ownership.owner_id", lambda a: a.replace("-agent", ""))   # each agent: its own person
        owners.start()
        self.addCleanup(owners.stop)
        self.lp._seen = {}                            # nobody known offline

    def item(self, stage, age_s, **kw):
        sent = time.time() - age_s
        item = {"room": "!r", "repo": "your-name/hagency-hello", "stage": stage, "seen_stage": stage, "since": sent, "builder": AGENT,
                "agent": AGENT, "body": "b", "task": {"kind": "build", "issue": 7}, "sent_at": sent,
                "sends": 1, "task_id": "OLD", "task_ids": ["OLD"], **kw}
        self.lp.state["7"] = item
        return item

    def run_watchdog(self, item):
        return asyncio.run(self.lp.watchdog("!r", "7", item))

    def test_answer_in_time_is_left_alone(self):
        item = self.item("building", 10)
        self.assertFalse(self.run_watchdog(item))
        self.lp.say.assert_not_called()

    def test_overdue_step_is_resent_with_a_new_task_id(self):
        item = self.item("building", loop_progress.STEP_TIMEOUT_S + 1)
        self.assertTrue(self.run_watchdog(item))
        self.assertEqual((item["stage"], item["sends"]), ("building", 2))
        self.assertNotEqual(item["task_id"], "OLD")
        self.assertEqual(item["task_ids"], ["OLD", item["task_id"]])   # the slow original still counts
        _, _, agent, task = self.lp.say.call_args_list[0].args
        self.assertEqual((agent, task["kind"]), (AGENT, "build"))

    def test_second_silence_stops_the_item(self):
        item = self.item("reviewing", loop_progress.STEP_TIMEOUT_S + 1, sends=2)
        self.assertTrue(self.run_watchdog(item))
        self.assertEqual(item["stage"], "stopped")
        self.assertIn("@pm retry #7", self.lp.say.call_args.args[1])

    def test_item_from_before_the_watchdog_stops_instead_of_guessing(self):
        item = self.item("building", loop_progress.STEP_TIMEOUT_S + 1)
        del item["task"]
        self.run_watchdog(item)
        self.assertEqual(item["stage"], "stopped")

    def gh(self, item, info):
        with mock.patch.object(github, "merge_ready", mock.AsyncMock(return_value=(False, info))):
            with mock.patch.object(self.lp, "level", mock.AsyncMock(return_value="autonomous")), \
                    mock.patch.object(github, "run_json", mock.AsyncMock(return_value=[])):
                self.lp.client = mock.Mock(rooms={"!r": None})
                asyncio.run(self.lp.tick())

    def test_github_blocking_too_long_stops(self):
        item = self.item("approved", loop_progress.GITHUB_WAIT_S + 1, pr=9)
        self.gh(item, {"state": "OPEN", "mergeable": "UNKNOWN", "reviewDecision": "APPROVED"})
        self.assertEqual(item["stage"], "stopped")

    def test_github_finishing_at_the_deadline_is_not_stopped(self):
        item = self.item("merge_sent", loop_progress.GITHUB_WAIT_S + 1, pr=9)
        self.gh(item, {"state": "MERGED"})
        self.assertEqual(item["stage"], "done")

    def test_original_result_still_counts_after_a_resend(self):
        from types import SimpleNamespace
        item = self.item("building", 5, task_ids=["OLD", "NEW"], task_id="NEW")
        result = {"kind": "build", "issue": 7, "already_done": True, "task_id": "OLD"}
        self.lp.agents_in = lambda room: [AGENT, "@r-agent:x"]
        asyncio.run(self.lp.on_result(SimpleNamespace(room_id="!r"), AGENT, result))
        self.assertEqual(item["stage"], "verifying")

    def test_person_stages_never_time_out(self):
        for stage in ("ready", "close_ready"):
            item = self.item(stage, 10 * loop_progress.GITHUB_WAIT_S)
            self.assertFalse(self.run_watchdog(item))

    def test_new_stage_restarts_the_clock(self):
        item = self.item("approved", loop_progress.GITHUB_WAIT_S + 1, seen_stage="reviewing")
        self.assertFalse(self.run_watchdog(item))
        self.assertEqual(item["seen_stage"], "approved")

    def test_a_step_decided_while_stopped_is_held_then_sent_on_resume(self):
        """GPT review: a result arriving after "stop" used to send the next step anyway."""
        item = self.item("reviewing", 0)
        with mock.patch.object(self.lp, "level", mock.AsyncMock(return_value="off")):
            asyncio.run(self.lp.dispatch("!r", item, AGENT, "review it", {"kind": "review", "issue": 7}))
        self.assertTrue(item["held"])
        self.lp.say.assert_not_called()
        self.gh(item, {"state": "OPEN"})                      # tick with the room back on
        self.assertNotIn("held", item)
        self.assertEqual(self.lp.say.call_args.args[1], "review it")

    def test_merge_conflict_stops_instead_of_blocking_the_queue(self):
        item = self.item("approved", 5, pr=9, sha="abc")
        info = {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "reviewDecision": "APPROVED"}
        with mock.patch.object(github, "merge_ready", mock.AsyncMock(return_value=(False, info))):
            asyncio.run(self.lp.advance("!r", "7", item, "autonomous"))
        self.assertEqual(item["stage"], "stopped")
        self.assertIn("merge conflicts", self.lp.say.call_args.args[1])


class AllDone(unittest.TestCase):
    def setUp(self):
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos = {"!r": github.REPO}
        self.lp.boards, self.lp.save_boards, self.lp.open_issues = {}, lambda: None, {"!r": []}
        self.lp.say = mock.AsyncMock()
        self.lp.state = {f"{github.REPO}#{n}": {"room": "!r", "repo": github.REPO, "stage": "done", "ended": n * 100}
                         for n in (1, 2)}                 # keys as migrate() writes them (GPT review 10: key=int)
        p = mock.patch.object(github, "run_json", mock.AsyncMock(side_effect=github.GitHubError("404")))
        p.start()
        self.addCleanup(p.stop)

    def announce(self):
        asyncio.run(self.lp.announce_done("!r"))

    def test_says_once_when_everything_is_merged_with_where_to_try_it(self):
        """the owner: when all done, pm should tell the room, and where to test-drive it."""
        self.announce()
        text = self.lp.say.call_args.args[1]
        self.assertIn("All done: #1, #2 merged", text)
        self.assertIn(f"https://github.com/{github.REPO}", text)
        self.announce()
        self.assertEqual(self.lp.say.call_count, 1)           # not again for the same work

    def test_links_the_live_site_when_the_repo_publishes_one(self):
        with mock.patch.object(github, "run_json", mock.AsyncMock(return_value={"html_url": "https://me.github.io/x/"})):
            self.announce()
        self.assertIn("https://me.github.io/x/ (live site", self.lp.say.call_args.args[1])

    def test_not_while_work_remains(self):
        self.lp.open_issues["!r"] = [{"number": 3, "title": "t"}]
        self.announce()
        self.lp.state["3"] = {"room": "!r", "repo": github.REPO, "stage": "reviewing"}
        self.lp.open_issues["!r"] = []
        self.announce()
        self.lp.say.assert_not_called()

    def test_the_next_round_is_announced_too(self):
        self.announce()
        self.lp.state[f"{github.REPO}#5"] = {"room": "!r", "repo": github.REPO, "stage": "done", "ended": time.time() + 1}
        self.announce()
        self.assertIn("All done: #5 merged", self.lp.say.call_args.args[1])


class FastPoll(unittest.TestCase):
    def test_waiting_on_github_checks_every_few_seconds(self):
        """the owner: 30 seconds feels long once the automation runs."""
        from server import loop
        lp = Loop.__new__(Loop)
        lp.room_repos = {}
        lp.busy, lp.state, ticks = set(), {"7": {"room": "!r", "repo": "your-name/hagency-hello", "stage": "approved"}}, []

        async def locked_tick():
            ticks.append(1)

        async def scenario():
            lp.locked_tick = locked_tick
            lp.at_start = mock.AsyncMock()        # its start-up work (status_board.at_start) needs a client
            with mock.patch.object(loop, "POLL_S", 60), mock.patch.object(loop, "FAST_POLL_S", 0.02):
                runner = asyncio.create_task(lp.poll_forever())
                await asyncio.sleep(0.15)
                runner.cancel()
        asyncio.run(scenario())
        self.assertGreaterEqual(len(ticks), 3)


class Kick(unittest.TestCase):
    def test_kick_runs_a_tick_now_instead_of_at_the_next_poll(self):
        """Seen live: after the owner's ✅ nothing visible happened for up to 30s."""
        from server import loop
        lp = Loop.__new__(Loop)
        lp.room_repos = {}
        lp.busy, lp.state, ticks = set(), {}, []

        async def locked_tick():
            ticks.append(time.monotonic())

        async def scenario():
            lp.locked_tick = locked_tick
            lp.at_start = mock.AsyncMock()        # its start-up work (status_board.at_start) needs a client
            with mock.patch.object(loop, "POLL_S", 60):
                runner = asyncio.create_task(lp.poll_forever())
                await asyncio.sleep(0.05)
                lp.kick()
                await asyncio.sleep(0.05)
                runner.cancel()
        asyncio.run(scenario())
        self.assertEqual(len(ticks), 2)            # the start, then the kick — not 60 s later


if __name__ == "__main__":
    unittest.main()
