"""Paused issues, as people meet them (screen tests, 2026-11-21):
  - "drive to finish all issues" from the panel, a ✅ or !level left #7 and #9 paused: every way the
    mode changes now puts them back in the queue (server/pm_paused.py);
  - "cx-agent couldn't build it, needs a person" said neither why nor what a person can do;
  - "use your hagency tools: call next_task" showed people an agent's internal hint.
"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from common import room_state
from server import console, pm_brain, pm_paused
from server.loop import Loop
from server.loop_progress import issue_key
from server.status_board import render

ANNA, PM = "@anna:bridge.localhost", "@pm:bridge.localhost"
REPO = "your-name/hagency-hello"


def paused(*numbers, pr=None) -> dict:
    return {issue_key(REPO, n): {"room": "!r", "repo": REPO, "stage": "stopped", "title": f"Issue {n}",
                                 "why": "cx-agent couldn't build it", "ended": time.time(), **({"pr": pr} if pr else {})}
            for n in numbers}


class EveryModeSwitch(unittest.TestCase):
    """The chat command is tested in test_pm_chat; here the panel, a ✅-confirmed switch and !level."""
    def setUp(self):
        for target, name, value in ((pm_brain, "BRAIN", "off"), (room_state, "bound", mock.AsyncMock(return_value=True)),
                                    (room_state, "repo_of", mock.AsyncMock(return_value=REPO)),
                                    (room_state, "binding_of", mock.AsyncMock(return_value=(REPO, None))),
                                    (room_state, "is_moderator", mock.AsyncMock(return_value=True)),
                                    (Loop, "change_level", mock.AsyncMock(return_value=None))):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos, self.lp.state, self.lp.lock, self.lp.save = {}, paused(7, 9), asyncio.Lock(), mock.Mock()
        self.lp.user_id, self.lp.pending, self.lp.boards, self.said = PM, {}, {}, []

        async def say(rid, body, *a, **k):
            self.said.append(body)
        self.lp.say = say
        self.room = SimpleNamespace(room_id="!r", users={ANNA: None}, invited_users={})
        self.lp.client, self.lp.note_outcome = SimpleNamespace(rooms={"!r": self.room}), mock.Mock()

    def test_the_panel(self):
        with mock.patch.object(console.project_rooms, "is_front_door", return_value=False), \
                mock.patch.object(console.console_personal, "refusal", return_value=None):
            self.assertEqual("ok", asyncio.run(console.act(self.lp, "!r", ANNA, "mode", "autonomous")))
        self.assertIn("from the console", self.said[-1])
        self.assertIn("#7, #9 are paused after an agent couldn't finish them: back in the queue now.", self.said[-1])
        self.assertEqual({}, self.lp.state)

    def test_a_confirmed_switch(self):
        self.lp.pending["!r"] = {"kind": "mode", "level": "autonomous", "event_id": "$q", "by": ANNA,
                                 "until": time.time() + 60}
        asyncio.run(self.lp.confirm(self.room, ANNA))
        self.assertIn("back in the queue now", self.said[-1])
        self.assertEqual({}, self.lp.state)

    def test_the_level_command(self):
        asyncio.run(self.lp.command(self.room, ANNA, ["!level", "autonomous"]))
        self.assertIn("back in the queue now", self.said[-1])

    def test_switching_off_touches_nothing(self):
        self.assertEqual("", asyncio.run(pm_paused.after_switch(self.lp, "!r", "off")))
        self.assertEqual(2, len(self.lp.state))

    def test_an_issue_that_moved_on_meanwhile_is_left_alone(self):
        """GitHub is asked outside the lock: an issue the loop changed in between isn't touched."""
        self.lp.state = paused(7, pr=12)

        async def pr_open(pr, repo):
            self.lp.state[issue_key(REPO, 7)]["stage"] = "building"     # the loop moved it meanwhile
            return False
        self.lp.pr_open = pr_open
        self.assertEqual("", asyncio.run(pm_paused.retry_all(self.lp, "!r")))
        self.assertEqual("building", self.lp.state[issue_key(REPO, 7)]["stage"])


class WhatAPersonCanDo(unittest.TestCase):
    def test_the_reason_is_the_agents_own_shortened_and_cleaned(self):
        why = pm_paused.failure("cx-agent", "build", "codex exited 1: /Users/chris/x token ghp_abcdefghijklmnop " + "z" * 400)
        self.assertTrue(why.startswith("cx-agent couldn't build it: codex exited 1:"))
        self.assertNotIn("/Users/chris", why)
        self.assertNotIn("ghp_abcdefghijklmnop", why)
        self.assertLessEqual(len(why), len("cx-agent couldn't build it: ") + pm_paused.MAX_REASON)

    def test_no_reason_reads_as_before(self):
        self.assertEqual("cx-agent couldn't build it", pm_paused.failure("cx-agent", "build", ""))

    def test_the_steps_name_each_action_and_the_agents_log(self):
        steps = pm_paused.next_steps(7, "@cx-agent:matrix.hagencylabs.xyz")
        for part in ("**Try again:** say **@pm retry #7**", "**Make the issue clearer**",
                     "cx-agent's owner can read cx-agent.log", "**Do it yourself:**", "close #7"):
            self.assertIn(part, steps)
        self.assertNotIn("log", pm_paused.next_steps(7))       # no agent known: no log to point at

    def test_the_pause_message_escapes_the_agents_text(self):
        lp, said = Loop.__new__(Loop), []
        lp.save = mock.Mock()

        async def say(rid, body, *a, **k):
            said.append(body)
        lp.say = say
        item = {"agent": "@cx-agent:x"}
        asyncio.run(lp.stop("!r", issue_key(REPO, 7), item, "cx-agent couldn't build it: [click](http://evil) **bold**"))
        self.assertNotIn("[click](", said[-1])
        self.assertNotIn("**bold**", said[-1])
        self.assertIn("**What you can do:**", said[-1])
        self.assertEqual("stopped", item["stage"])

    def test_the_board_says_how_to_go_on(self):
        board = render(paused(9, 7), "autonomous", [], time.time(), REPO)
        self.assertIn("**Needs a person**", board)
        self.assertIn("say **@pm retry #7** to try again", board)


class AgentsHint(unittest.TestCase):
    def test_people_see_the_task_without_the_agents_hint(self):
        lp, sent = Loop.__new__(Loop), []
        lp.room_repos = {}

        async def room_send(rid, kind, content):
            sent.append(content)
            from nio import RoomSendResponse
            return RoomSendResponse("$e", rid)
        lp.client = SimpleNamespace(room_send=room_send)
        asyncio.run(lp.say("!r", "cx-agent: build #7 Hello", "@cx-agent:x", {"kind": "build", "issue": 7, "repo": REPO}))
        self.assertIn("call next_task", sent[-1]["body"])                 # agents read the plain body
        self.assertNotIn("next_task", sent[-1]["formatted_body"])         # people see this


if __name__ == "__main__":
    unittest.main()
