"""Stop / Start an agent from the Hagency panel (agent_stop.py, pm_stop.py, ADR 0009): the owner's flow
end to end, from the panel's button or "@pm stop my agent" to the agent going quiet and back.
Run: .venv/bin/python -m unittest tests.test_agent_stop"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomPutStateError, RoomPutStateResponse

from adapters.agents import agent_stop
from adapters.agents import bridge
from adapters.agents import bridge_voice
from server import console
from server import console_personal
from common import matrix_calls
from server import panel
from server import pm_stop
from common import registry
from adapters.agents import workdirs
from server.loop import Loop

S = "bridge.localhost"
ANNA, AGENT, PM = f"@anna:{S}", f"@anna-agent:{S}", f"@pm:{S}"
CX = f"@cx:{S}"
WELCOME = "!welcome:x"


def msg(sender, body, eid):
    content = {"body": body, "msgtype": "m.text", "m.mentions": {"user_ids": [AGENT]}}
    return SimpleNamespace(sender=sender, body=body, event_id=eid, source={"content": content},
                           server_timestamp=int(time.time() * 1000))


class Switch:
    """The Welcome room's state, as Synapse would keep it: what @pm writes, the agent reads."""
    def __init__(self):
        self.events, self.reads = {}, 0

    async def alias(self, client, alias):
        return WELCOME

    async def get(self, client, path):
        self.reads += 1
        key = urllib.parse.unquote(path.split(f"/state/{agent_stop.STOP_TYPE}/")[1].split("?")[0])
        found = self.events.get(key)
        return (200, found) if found else (404, {"errcode": "M_NOT_FOUND"})

    async def put(self, room_id, event_type, content, state_key=""):
        assert room_id == WELCOME and event_type == agent_stop.STOP_TYPE
        if state_key.startswith("@") and state_key != PM:   # Matrix's auth rule (GPT review: every write was refused)
            return RoomPutStateError("M_FORBIDDEN")
        self.events[state_key] = {"sender": PM, "content": content}
        return RoomPutStateResponse(event_id="$s", room_id=room_id)

    def patches(self):
        return (mock.patch.object(matrix_calls, "room_of_alias", self.alias),
                mock.patch.object(matrix_calls, "get_json", self.get))


class AgentReadsTheSwitch(unittest.TestCase):
    def setUp(self):
        self.switch = Switch()
        for p in self.switch.patches():
            p.start()
            self.addCleanup(p.stop)
        self.client = SimpleNamespace(access_token="t")

    def read(self):
        return asyncio.run(agent_stop.read_stopped(self.client, AGENT))

    def test_only_pm_can_stop_an_agent(self):
        key = agent_stop.switch_key(AGENT)
        self.switch.events[key] = {"sender": f"@mallory:{S}", "content": {"stopped": True}}
        self.assertFalse(self.read())
        self.switch.events[key] = {"sender": PM, "content": {"stopped": True}}
        self.assertTrue(self.read())
        self.switch.events[key] = {"sender": PM, "content": {"stopped": "yes"}}   # only a real true
        self.assertFalse(self.read())

    def test_never_stopped_or_not_signed_in_means_running(self):
        self.assertFalse(self.read())                                        # 404
        self.client.access_token = ""
        self.switch.events[agent_stop.switch_key(AGENT)] = {"sender": PM, "content": {"stopped": True}}
        self.assertFalse(self.read())
        self.assertEqual(self.switch.reads, 1)                               # no call without a sign-in

    def test_a_network_failure_fails_open_and_is_logged(self):
        async def broken(client, path):
            raise OSError("down")
        with mock.patch.object(matrix_calls, "get_json", broken), self.assertLogs("bridge", "WARNING"):
            self.assertFalse(self.read())

    def test_a_proxy_404_is_logged_not_taken_for_never_stopped(self):
        async def page(client, path):
            return 404, {}
        with mock.patch.object(matrix_calls, "get_json", page), self.assertLogs("bridge", "WARNING"):
            self.assertFalse(self.read())

    def test_a_refused_read_is_logged_not_silent(self):
        async def refused(client, path):
            return 403, {"errcode": "M_FORBIDDEN"}
        with mock.patch.object(matrix_calls, "get_json", refused), self.assertLogs("bridge", "WARNING"):
            self.assertFalse(self.read())


class OwnerStopsAndStartsTheirAgent(unittest.TestCase):
    """R6: the whole flow, @pm's button to the agent's behaviour, through the one shared switch."""
    people = {ANNA: {"agent": "remote", "dm": "!a", "agent_kind": "codex"}}

    def setUp(self):
        self.switch = Switch()
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps(self.people))
        for p in (*self.switch.patches(), mock.patch.object(registry, "PATH", path),
                  mock.patch.object(workdirs, "WORK_HOME", Path(tempfile.mkdtemp())),
                  mock.patch.object(bridge.lifecycle, "is_stale", lambda *a: False),
                  mock.patch.object(bridge.room_context, "snapshot", lambda *a: {"status": "", "conversation": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.said = PM, []

        async def say(room_id, body, *a, **k):
            self.said.append(body)
            return "$m"
        self.lp.say = say
        self.lp.client = SimpleNamespace(room_put_state=self.switch.put)
        self.b = bridge.Bridge("anna", "codex")
        self.b.client.access_token = "t"
        self.room = SimpleNamespace(room_id="!dev", display_name="dev", joined_count=2, invited_count=0,
                                    users={ANNA: None, AGENT: None}, invited_users={}, user_name=lambda u: u)
        self.sent, self.turns = [], []

        async def send(room_id, body, reply_to=None, **kw):
            self.sent.append((body, kw.get("extra")))
            return "$x"

        async def run_turn(backend, workdir, prompt, **kw):
            self.turns.append(prompt)
            return "hello"
        self.b.send, self.b.keep_typing = send, mock.AsyncMock()
        self.b.client.room_typing = mock.AsyncMock()
        p = mock.patch.object(bridge.agents, "run_turn", run_turn)
        p.start()
        self.addCleanup(p.stop)

    def deliver(self, *events):
        async def go():
            for ev in events:
                await self.b.on_message(self.room, ev)
                await asyncio.gather(*list(self.b.tasks))
        asyncio.run(go())

    def test_stop_silences_it_with_one_notice_and_start_brings_it_back(self):
        self.assertTrue(asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True)))
        self.assertIn("Stopped: **anna-agent**", self.said[-1])
        self.assertTrue(registry.agents_of(ANNA)[0]["stopped"])
        self.deliver(msg(ANNA, "anna-agent hi", "$1"), msg(ANNA, "anna-agent hello?", "$2"))
        self.assertEqual(self.turns, [])                                     # no work while stopped
        self.assertEqual([s for s, _ in self.sent], [agent_stop.REPLY.format(owner="anna")])   # told once
        self.assertTrue(asyncio.run(self.lp.set_stopped("!dm", ANNA, None, False)))
        self.assertEqual(self.said[-1], pm_stop.STARTED.format(agent="anna-agent"))
        self.assertFalse(registry.agents_of(ANNA)[0]["stopped"])
        self.deliver(msg(ANNA, "anna-agent hi again", "$3"))
        self.assertEqual(len(self.turns), 1)                                 # answers again

    def test_a_loop_task_waits_while_stopped_and_its_resend_runs_after_start(self):
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        self.b.run_task = mock.AsyncMock()
        task = {"kind": "build", "issue": 7, "task_id": "t1"}
        asyncio.run(self.b.task_unless_stopped("!dev", task))
        self.b.run_task.assert_not_awaited()
        self.assertEqual(self.sent, [])      # no error result: that would pause the issue for good (GPT review)
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, False))
        asyncio.run(self.b.task_unless_stopped("!dev", {**task, "task_id": "t2"}))   # the loop's resend
        self.b.run_task.assert_awaited_once()

    def test_voice_and_work_queued_before_stop_are_held_too(self):
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        ev = msg(ANNA, "what's the weather", "$v")    # on_voice and a queued text both end in answer_locked
        asyncio.run(self.b.answer_locked(self.room, ev, "anna said by voice: what's the weather"))
        self.assertEqual(self.turns, [])
        self.assertEqual(len(self.sent), 1)

    def test_planning_while_stopped_is_answered_so_nobody_waits(self):
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        self.b.run_task = mock.AsyncMock()
        asyncio.run(self.b.task_unless_stopped("!dev", {"kind": "plan", "task_id": "p1"}))
        self.b.run_task.assert_not_awaited()
        result = self.sent[-1][1][bridge.github.RESULT_KEY]
        self.assertEqual((result["kind"], result["task_id"]), ("plan", "p1"))
        self.assertIn("stopped", result["error"])

    def test_voice_is_never_transcribed_while_stopped(self):
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        listen = mock.AsyncMock(return_value="hi")
        ev = SimpleNamespace(sender=ANNA, event_id="$a", source={"content": {}}, server_timestamp=int(time.time() * 1000))
        self.room.joined_count = 2                                     # a private chat: clearly for me
        with mock.patch.object(bridge_voice.voice, "listen", listen):
            asyncio.run(self.b.hear(self.room, ev))
        listen.assert_not_awaited()
        self.assertEqual(len(self.sent), 1)                            # the one notice

    def test_an_unaddressed_follow_up_stays_quiet_and_keeps_the_notice(self):
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        asyncio.run(self.b.answer_locked(self.room, msg(ANNA, "95130", "$f"), "anna: 95130", follow_up="$q"))
        self.assertEqual(self.sent, [])
        self.deliver(msg(ANNA, "anna-agent hi", "$h"))
        self.assertEqual(len(self.sent), 1)

    def test_a_button_waiting_for_its_turn_is_held_after_stop(self):
        self.b.offers = lambda: {"$o": {"room": "!dev"}}
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        self.b.do = mock.AsyncMock(return_value=True)
        asyncio.run(self.b.act_on("!dev", "$o", "✅"))
        self.b.do.assert_not_awaited()

    def test_a_code_change_request_is_held_too(self):
        asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True))
        self.b.go_back = mock.AsyncMock()
        asyncio.run(self.b.change_code(self.room, msg(ANNA, "anna-agent go back to the official version", "$c")))
        self.b.go_back.assert_not_awaited()

    def test_a_failed_write_says_so_and_records_nothing(self):
        async def refused(*a, **k):
            return RoomPutStateError("M_FORBIDDEN")
        self.lp.client.room_put_state = refused
        with self.assertLogs("loop", "ERROR"):
            self.assertFalse(asyncio.run(self.lp.set_stopped("!dm", ANNA, None, True)))
        self.assertIn("couldn't stop", self.said[-1])
        self.assertFalse(registry.agents_of(ANNA)[0]["stopped"])

    def test_the_chat_command_and_the_panel_button_do_the_same(self):
        self.lp.set_stopped = mock.AsyncMock(return_value=True)
        m = pm_stop.STOP_RE.search("please stop my codex agent")
        self.assertEqual((m.group("verb"), m.group("label")), ("stop", "codex"))
        self.assertFalse(pm_stop.wants_stop(pm_stop.STOP_RE.search("start my agent").group("verb")))
        self.assertIsNone(pm_stop.STOP_RE.search("stop my agent from deleting files"))   # a request, not the command
        self.assertIn("stop", console_personal.ACTIONS)
        self.assertIn("start", console_personal.ACTIONS)
        room = SimpleNamespace(room_id="!r", users={ANNA: 0}, invited_users={})
        self.lp.client.rooms = {"!r": room}
        self.lp.is_person = lambda r, u: u == ANNA
        self.lp.private_room = mock.AsyncMock(return_value="!dm")
        with mock.patch("server.project_rooms.is_front_door", return_value=False), \
                mock.patch.object(console_personal, "refusal", return_value=None):
            self.assertEqual(asyncio.run(console.act(self.lp, "!r", ANNA, "start", "", "anna-agent")), "ok")
        self.lp.set_stopped.assert_awaited_once_with("!dm", ANNA, "anna-agent", False)


class HermesAndSeveral(unittest.TestCase):
    people = {CX: {"agent": "remote", "agent_kind": "hermes",
                   "more": [{"label": "codex", "agent": "remote"}, {"label": "two", "agent": "remote"}]}}

    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps(self.people))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.said = PM, []

        async def say(room_id, body, *a, **k):
            self.said.append(body)
        self.lp.say = say
        self.lp.write_stopped = mock.AsyncMock(return_value=True)

    def test_several_asks_which_and_hermes_says_how_to_stop_it_on_its_computer(self):
        self.assertFalse(asyncio.run(self.lp.set_stopped("!dm", CX, None, True)))
        self.assertIn("Say \"stop my agent NAME\"", self.said[-1])
        self.assertFalse(asyncio.run(self.lp.set_stopped("!dm", CX, "main", True)))
        self.assertIn("runs Hermes", self.said[-1])
        self.assertTrue(asyncio.run(self.lp.set_stopped("!dm", CX, "codex", True)))
        self.lp.write_stopped.assert_awaited_once_with("cx-codex-agent", True)
        self.assertTrue(registry.find(CX, "codex")["stopped"])
        self.assertFalse(registry.find(CX, "two")["stopped"])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PanelButton(unittest.TestCase):
    def status(self, **agent):
        esc = re.search(r"^const esc = .*$", panel.PAGE, re.M).group(0)
        source = "\n".join([esc, "const cap = t => t;", panel.SCRIPTS["panel-agents.js"].read_text(),
                            f"process.stdout.write(agentStatus({json.dumps(agent)}) + stopHint({json.dumps(agent)}))"])
        done = subprocess.run([shutil.which("node"), "-e", source], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return done.stdout

    def test_stop_then_start_and_none_for_hermes_or_never_connected(self):
        base = {"name": "cx-agent", "remote": True}
        self.assertIn('data-a="stop"', self.status(**base, online=True))
        stopped = self.status(**base, online=True, stopped=True)
        self.assertIn('data-a="start"', stopped)
        self.assertIn("Stopped", stopped)
        self.assertIn("until you click Start", stopped)
        self.assertNotIn('data-a="st', self.status(**base, online=None, connected=False))
        hermes = self.status(**base, online=True, hermes=True)
        self.assertNotIn('data-a="st', hermes)
        self.assertIn("connect.py --stop", hermes)


if __name__ == "__main__":
    unittest.main()
