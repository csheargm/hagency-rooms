"""@pm understands with the room owner's agent (ADR 0022): the shared prompt and parser, a turn with no
tools, who owns a room, and the agent's side of the "understand" task. The flows: test_owner_ai_flow.py.
Run: .venv/bin/python -m unittest tests.test_owner_ai"""
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetStateResponse, ToDeviceResponse

from adapters.agents import agents
from adapters.agents import bridge
from common import github
from common import pm_actions
from common import room_state
from server import pm_brain
from server import pm_owner_brain

ANNA, BETTY = "@anna:bridge.localhost", "@betty:bridge.localhost"
PM = f"@{github.PM_BOT}:bridge.localhost"
ANNA_AGENT = "@anna-agent:bridge.localhost"
CTX = {"repo": "you/hello", "mode": "assist", "sender": "betty", "role": "contributor", "waiting": "nothing",
       "plan": "none", "board": "(none yet)", "transcript": "anna: hi", "replied": "", "text": "where are we"}


def state(*events, joined=(ANNA, BETTY, PM, ANNA_AGENT), levels=None) -> RoomGetStateResponse:
    """The room's state as the server has it now: membership and power levels included (never the sync cache)."""
    members = [{"type": "m.room.member", "state_key": u, "content": {"membership": "join"}} for u in joined]
    power = [{"type": "m.room.power_levels", "state_key": "", "content": {"users": levels}}] if levels else []
    return RoomGetStateResponse([*events, *members, *power], "!r")


def created_by(user: str, version: str = "12") -> dict:
    return {"type": "m.room.create", "sender": user, "state_key": "", "content": {"room_version": version}}


class Shared(unittest.TestCase):
    def test_pm_brain_and_the_agent_use_one_prompt_and_parser(self):
        """R3: the server's own model and the room owner's agent read with exactly the same code."""
        self.assertIs(pm_brain.parse, pm_actions.parse)
        self.assertIs(pm_brain.prompt, pm_actions.prompt)
        self.assertIs(pm_brain.ACTIONS, pm_actions.ACTIONS)

    def test_an_unaddressed_message_asks_whether_it_is_for_pm(self):
        said = pm_actions.prompt({**CTX, "unaddressed": True, "how": "they are the only person in the room"})
        self.assertIn("did not name @pm (they are the only person in the room)", said)
        self.assertIn('{"action": "none", "reply": ""}', said)
        self.assertNotIn("did not name @pm", pm_actions.prompt(CTX))

    def test_only_known_actions_and_clean_replies_pass(self):
        self.assertEqual(pm_actions.checked("status", "  Here\n you go "), ("status", "Here you go"))
        self.assertIsNone(pm_actions.checked("delete_repo", "x"))
        self.assertIsNone(pm_actions.checked(["stop"], "x"))
        self.assertEqual(pm_actions.checked("none", {"x": 1}), ("none", ""))
        self.assertIn("(link removed)", pm_actions.checked("none", "see https://evil.example")[1])

    def test_the_context_sent_to_an_agent_is_bounded_and_typed(self):
        out = pm_actions.clean_context({**CTX, "board": "b" * 9000, "transcript": "t" * 20000, "extra": "x",
                                        "unaddressed": "yes"})
        self.assertEqual((len(out["board"]), len(out["transcript"])), (pm_actions.MAX_FIELD, pm_actions.TRANSCRIPT_CHARS))
        self.assertNotIn("extra", out)
        self.assertIs(out["unaddressed"], False)              # only a real True counts
        for bad in (None, "text", {"text": ""}, {"text": 5}):
            self.assertIsNone(pm_actions.clean_context(bad), bad)


class NoTools(unittest.TestCase):
    def test_claude_gets_no_tools_at_all(self):
        argv = agents.without_tools(agents._claude_argv(Path("/w"), None))
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", argv)

    def test_codex_loses_its_shell_too(self):
        with mock.patch.object(agents, "_codex_lockdown", lambda: []):
            argv = agents.without_tools(agents._codex_argv(Path("/w"), None))
        self.assertEqual(argv[:2], ["codex", "exec"])
        for feature in agents.CODEX_TOOLS_OFF:
            self.assertIn(f"features.{feature}=false", argv)
        self.assertIn('sandbox_mode="read-only"', argv)

    def test_a_bare_turn_is_always_fresh_and_locked(self):
        for kw in ({"owner": True, "fresh": True}, {"write": True, "fresh": True}, {}):
            with self.assertRaises(agents.AgentError):
                asyncio.run(agents.run_turn("claude", Path("/tmp/none"), "x", bare=True, **kw))


class Owners(unittest.TestCase):
    def owners(self, users, events, levels=None, cached=None):
        cached = cached or levels or {}               # the sync cache: never what counts (GPT review)
        room = SimpleNamespace(room_id="!r", users=dict.fromkeys(users), invited_users={},
                               power_levels=SimpleNamespace(get_user_level=lambda u: cached.get(u, 0)))
        client = SimpleNamespace(user_id=PM, room_get_state=mock.AsyncMock(return_value=state(*events, joined=users, levels=levels)))
        return asyncio.run(room_state.owners(client, room))

    def test_the_creator_owns_a_room_they_made(self):
        self.assertEqual(self.owners([ANNA, BETTY, ANNA_AGENT], [created_by(ANNA)], {BETTY: 50}), [ANNA])

    def test_a_room_pm_made_is_owned_by_whom_it_gave_level_100(self):
        """pm_projects.make_project_room: @pm creates it, the person gets 100."""
        self.assertEqual(self.owners([PM, BETTY, ANNA], [created_by(PM)], {PM: 100, BETTY: 100}), [BETTY])

    def test_creators_come_first_and_agents_never_count(self):
        got = self.owners([ANNA, BETTY, ANNA_AGENT], [created_by(BETTY)], {ANNA: 100, ANNA_AGENT: 100})
        self.assertEqual(got, [BETTY, ANNA])

    def test_a_just_demoted_owner_no_longer_counts(self):
        """GPT review: the sync cache still said 100; the state read now says 0."""
        self.assertEqual(self.owners([PM, BETTY], [created_by(PM)], {BETTY: 0}, cached={BETTY: 100}), [])

    def test_unreadable_state_means_no_owner(self):
        room = SimpleNamespace(room_id="!r", users=dict.fromkeys([ANNA]), invited_users={},
                               power_levels=SimpleNamespace(get_user_level=lambda u: 100))
        client = SimpleNamespace(user_id=PM, room_get_state=mock.AsyncMock(return_value=None))
        self.assertEqual(asyncio.run(room_state.owners(client, room)), [])


class AgentSide(unittest.TestCase):
    """The room owner's agent takes the task only from @pm, for a room its owner owns, and runs no tools."""
    def setUp(self):
        self.b = bridge.Bridge("anna", "claude")
        self.room = SimpleNamespace(room_id="!r", users=dict.fromkeys([ANNA, BETTY, PM, ANNA_AGENT]), invited_users={},
                                    power_levels=SimpleNamespace(get_user_level=lambda u: 0))
        self.sent, self.turns = [], []

        async def to_device(message, txn):
            self.sent.append(message.content)
            return ToDeviceResponse(message)

        async def run_turn(backend, workdir, prompt, **kw):
            self.turns.append(kw)
            return '{"action": "status", "reply": "Here is where we are."}'
        self.b.client = SimpleNamespace(rooms={"!r": self.room}, user_id=ANNA_AGENT, access_token="tok", to_device=to_device,
                                        room_get_state=mock.AsyncMock(return_value=state(created_by(ANNA))))
        self.b.is_stopped = mock.AsyncMock(return_value=False)
        p = mock.patch.object(agents, "run_turn", run_turn)
        p.start()
        self.addCleanup(p.stop)

    def ask(self, sender=PM, **over):
        content = {"task_id": "a" * 32, "room": "!r", "sent_at": time.time(), "context": CTX, **over}

        async def go():
            await self.b.on_understand(SimpleNamespace(type=pm_actions.TASK_TYPE, sender=sender, source={"content": content}))
            await asyncio.gather(*list(self.b.tasks))
        asyncio.run(go())

    def test_it_reads_the_message_with_no_tools_and_answers_pm(self):
        self.ask()
        self.assertEqual(self.sent, [{"task_id": "a" * 32, "ack": True},
                                     {"task_id": "a" * 32, "action": "status", "reply": "Here is where we are."}])
        self.assertEqual(self.turns, [{"fresh": True, "bare": True}])

    def test_only_pms_account_is_obeyed(self):
        self.ask(sender=BETTY)
        self.assertEqual((self.sent, self.turns), ([], []))

    def test_refused_unless_its_owner_owns_the_room(self):
        self.b.client.room_get_state = mock.AsyncMock(return_value=state(created_by(BETTY)))
        self.ask()
        self.assertEqual(self.sent, [{"task_id": "a" * 32, "error": "my owner doesn't own that room"}])
        self.assertEqual(self.turns, [])

    def test_refused_when_stopped_old_or_for_another_room(self):
        self.b.is_stopped = mock.AsyncMock(return_value=True)
        self.ask()
        self.b.is_stopped = mock.AsyncMock(return_value=False)
        self.ask(sent_at=time.time() - 3600)
        self.ask(room="!elsewhere")
        self.assertEqual([s["error"] for s in self.sent], ["my owner stopped me", "too old", "not in that room"])
        self.assertEqual(self.turns, [])

    def test_a_model_answer_outside_the_list_is_an_error(self):
        with mock.patch.object(agents, "run_turn", mock.AsyncMock(return_value='{"action": "rm -rf", "reply": ""}')):
            self.ask()
        self.assertEqual(self.sent[-1], {"task_id": "a" * 32, "error": "no usable answer"})

    def test_more_than_two_at_once_are_refused_not_queued(self):
        self.b._understanding = 2
        self.ask()
        self.assertEqual((self.sent, self.turns), ([{"task_id": "a" * 32, "error": "busy"}], []))


class PmSide(unittest.TestCase):
    """@pm takes an answer only from the agent it asked, and only an action from the list."""
    def setUp(self):
        self.pm = pm_owner_brain.OwnerBrain()
        self.room = SimpleNamespace(room_id="!r", users=dict.fromkeys([ANNA, PM, ANNA_AGENT]), invited_users={},
                                    power_levels=SimpleNamespace(get_user_level=lambda u: 0))
        self.answers = []

        async def to_device(message, txn):
            for sender, content in self.answers:
                await self.pm.on_understood(SimpleNamespace(type=pm_actions.RESULT_TYPE, sender=sender, source={
                    "content": {"task_id": message.content["task_id"], **content}}))
            return ToDeviceResponse(message)
        self.pm.client = SimpleNamespace(user_id=PM, to_device=to_device, add_to_device_callback=mock.Mock(),
                                         room_get_state=mock.AsyncMock(return_value=state(created_by(ANNA))))
        self.pm.own_agent = lambda room, person: ANNA_AGENT if person == ANNA else None
        self.pm.agent_online = lambda agent: True
        for target, name, value in ((pm_owner_brain, "cannot", lambda u, a: None), (pm_owner_brain, "ACK_S", 0.05),
                                    (pm_owner_brain, "WAIT_S", 0.05)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def ask(self, *answers):
        self.answers = list(answers)
        return asyncio.run(self.pm.ask_owner(self.room, ANNA_AGENT, CTX))

    def test_the_asked_agents_answer_counts(self):
        self.assertEqual(self.ask((ANNA_AGENT, {"ack": True}), (ANNA_AGENT, {"action": "status", "reply": "ok"})),
                         ("status", "ok"))

    def test_someone_elses_answer_is_ignored(self):
        self.assertEqual(self.ask(("@betty-agent:bridge.localhost", {"action": "stop", "reply": ""})),
                         pm_owner_brain.NOT_ANSWERING)

    def test_an_action_outside_the_list_or_an_error_or_slowness_is_no_answer(self):
        """R14: nothing empty or failed counts as understood."""
        for answer in ({"action": "merge_everything", "reply": ""}, {"error": "busy"}, {"ack": True}):
            self.pm.__dict__.pop("_brain_quiet", None)
            self.assertEqual(self.ask((ANNA_AGENT, answer)), pm_owner_brain.FAILED, answer)


class ExplicitModeChange(unittest.TestCase):
    def test_any_explicit_mode_change_drops_a_question_asked_without_being_named(self):
        """GPT review: "!level off", then a 👍 on the old implicit "switch to autonomous?" restarted it."""
        from server.loop import Loop
        s = Loop.__new__(Loop)
        s.pending, s.generation = {"!r": {"kind": "implicit", "intent": "autonomous", "event_id": "$q", "until": time.time() + 60}}, {}
        s.write_level = mock.AsyncMock(return_value=None)
        asyncio.run(s.change_level("!r", "off"))
        self.assertEqual((s.pending, s.generation), ({}, {"!r": 1}))   # a stop also drops thoughts in flight


if __name__ == "__main__":
    unittest.main()
