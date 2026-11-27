"""Flow tests: a visitor without an agent is offered the trial agent by @pm, takes it, and it answers;
with no OLLAMA_API_KEY nothing changes; the trial agent never builds, reviews or merges (ADR 0025).
Run: .venv/bin/python -m unittest tests.test_trial_flow"""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import registry
from server import console
from server import pm_trial
from server import pm_welcome
from server import trial_agent
from server import trial_budget
from server import trial_models
from server import trial_turn
from server.loop import Loop
from tests.test_trial_agent import hear
from tests.test_welcome import Base, DM, NEW, PM, S, WELCOME_ROOM, member, reaction, text

ME = f"@{trial_agent.LOCALPART}:{S}"
NOW_MS = 10**13


def said_to(test, room_id):
    return [b for r, b in test.said if r == room_id]


class TrialBase(Base):
    """A running trial agent (fake client and model) beside @pm; no tests of its own."""
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20, "TRIAL_PER_VISITOR": "2"})
        env.start()
        self.addCleanup(env.stop)
        self.trial_posts, self.asked = [], []

        async def send(room_id, kind, content):
            self.trial_posts.append((room_id, content["body"]))
            return SimpleNamespace(event_id="$t")
        self.trial_client = SimpleNamespace(user_id=ME, rooms={}, room_send=send, room_typing=mock.AsyncMock(),
                                            room_leave=mock.AsyncMock(), join=mock.AsyncMock(),
                                            room_create=mock.AsyncMock(return_value=SimpleNamespace(room_id="!trialdm")))

        async def model(chat, count=None, model=None):
            self.asked.append(chat)
            return "A pull request is a proposed change."
        folder = Path(tempfile.mkdtemp())

        async def no_balance():
            return None
        self.lp.trial = trial_agent.TrialAgent(self.trial_client, self.lp.trial_eligible, ask=model, data_dir=folder,
                                               budget=trial_budget.Budget(folder / "b.json", fetch=no_balance))
        self.lp.trial.started_ms = 0
        self.lp.addressed_text = lambda event, room=None: None
        repo = mock.patch.object(trial_turn.room_state, "repo_of", mock.AsyncMock(return_value=None))
        repo.start()
        self.addCleanup(repo.stop)

    def join_welcome(self):
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))


class TrialFlow(TrialBase):
    def test_visitor_joins_is_offered_accepts_and_the_trial_agent_answers(self):
        self.join_welcome()
        offer = said_to(self, "!dm")[0]
        self.assertIn("try the server agent", offer)
        self.assertEqual([b[2] for b in self.buttons], ["🔌", "🧪"])
        self.run_(self.lp.on_welcome_reaction(DM, reaction(NEW, "🧪")))
        ready = said_to(self, "!dm")[-1]
        self.assertTrue(ready.startswith("🧪 Your server agent is ready"), ready)
        self.assertIn("Ollama Cloud", ready)
        self.assertEqual(self.trial_client.room_create.call_args.kwargs["invite"], [NEW])
        # They accept its invite and write to it.
        self.trial_client.rooms["!trialdm"] = SimpleNamespace(room_id="!trialdm", users={ME: None, NEW: None}, invited_users={})
        msg = SimpleNamespace(sender=NEW, body="What is a pull request?", source={"content": {}}, server_timestamp=NOW_MS, event_id="$q")
        self.run_(hear(self.lp.trial, self.trial_client.rooms["!trialdm"], msg))
        self.assertEqual(self.trial_posts[-1], ("!trialdm", "A pull request is a proposed change.\n\n— Server agent · glm-5.3"))

    def test_typing_it_in_our_chat_works_too(self):
        self.join_welcome()
        self.assertTrue(self.run_(self.lp.on_welcome_text(DM, text(NEW, "try the trial agent"))))
        self.assertTrue(said_to(self, "!dm")[-1].startswith("🧪 Your server agent is ready"))

    def test_cap_reached_says_when_it_resets_and_how_to_bring_their_own(self):
        self.join_welcome()
        self.run_(self.lp.on_welcome_reaction(DM, reaction(NEW, "🧪")))
        dm = SimpleNamespace(room_id="!trialdm", users={ME: None, NEW: None}, invited_users={})
        self.trial_client.rooms["!trialdm"] = dm
        for n in range(3):
            self.run_(hear(self.lp.trial, dm, SimpleNamespace(sender=NEW, body=f"q{n}", source={"content": {}},
                                                                    server_timestamp=NOW_MS, event_id=f"$q{n}")))
        last = self.trial_posts[-1][1]
        self.assertIn("00:00 UTC", last)
        self.assertIn("bring my own", last)
        self.assertEqual(len(self.asked), 2)

    def test_someone_with_their_own_connected_agent_is_offered_it_too(self):
        """Owner, 2026-11-27: the server agent is for everyone (more: test_server_agent_everyone)."""
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({NEW: {"agent": "remote"}}))
        with mock.patch.object(registry, "PATH", path):
            self.lp._seen = {registry.agent_of(NEW): {"online": True, "ever": True}}
            self.assertTrue(self.lp.trial_eligible(NEW))
            self.assertIsNotNone(self.lp.trial_view(NEW))
            said = self.run_(self.lp.start_trial("!dm", NEW))
        self.assertTrue(said.startswith("🧪 Your server agent is ready"), said)
        self.trial_client.room_create.assert_awaited_once()

    def test_agents_bots_and_pm_are_never_offered_it(self):
        for who in (f"@newbie1-agent:{S}", f"@admin-bot:{S}", PM):
            self.assertFalse(self.lp.trial_eligible(who))
            self.assertIsNone(self.lp.trial_view(who))
            self.assertEqual(self.run_(self.lp.start_trial("!dm", who)), pm_trial.NOT_FOR_YOU)

    def test_the_panel_card_and_its_button(self):
        self.assertEqual(self.lp.trial_view(NEW), {"on": False, "used": 0, "per_day": 2, "tier": "free", "model": "glm-5.3",
                                                       "models": trial_models.names()})
        self.assertEqual(self.run_(self.lp.trial_action(NEW))["result"], "ok")   # with its chat: test_trial_open_chat
        self.assertTrue(self.lp.trial_view(NEW)["on"])
        self.assertIn("trial", console.console_personal.ACTIONS)

    def test_the_trial_agent_is_never_a_builder_reviewer_or_merger(self):
        project = SimpleNamespace(room_id="!proj", users={ME: None, NEW: None, PM: None}, invited_users={})
        self.assertEqual(Loop.agents_in(self.lp, project), [])         # nobody's agent: never given a step
        self.assertFalse(Loop.is_person(self.lp, project, ME))           # and never steers @pm

    def test_a_crashed_trial_agent_is_no_longer_offered(self):
        self.lp.trial.task = SimpleNamespace(done=lambda: True)
        self.join_welcome()
        self.assertNotIn("trial", said_to(self, "!dm")[0].lower())
        self.assertEqual([b[2] for b in self.buttons], ["🔌"])


class TrialOff(Base):
    """No OLLAMA_API_KEY: the welcome flow is exactly as before."""
    def test_no_key_no_offer_and_the_words_fall_through(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(asyncio.run(trial_agent.start(lambda who: True)))
        self.lp.trial = None
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.assertEqual(said_to(self, "!dm")[0], pm_welcome.WELCOME.format(name="newbie1"))
        self.assertEqual([b[2] for b in self.buttons], ["🔌"])
        self.lp.addressed_text = lambda event, room=None: None
        self.assertFalse(self.run_(self.lp.on_trial_text(DM, text(NEW, "try the trial agent"))))
        self.assertIsNone(self.lp.trial_view(NEW))
        self.assertEqual(self.run_(self.lp.start_trial("!dm", NEW)), pm_trial.UNAVAILABLE)
