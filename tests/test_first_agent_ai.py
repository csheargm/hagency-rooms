"""User flow (R6): a newcomer chooses which AI their FIRST agent runs (screen test: the picker was only
under "Add an agent", which shows once an agent has connected, and 🔌 offered only Codex or Claude
Code). In @pm's chat ("bring my own using ollama qwen3:8b") and in My Hagency's picker, the choice
reaches the Connect page link, the install line, POST /connect's answer and connect.py, exactly as
for an added agent. Until it connects they may choose again; after that, its AI stays.
Run: .venv/bin/python -m unittest tests.test_first_agent_ai"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from server import console
from server import console_agents
from server import pm_first
from common import pm_kinds
from common import registry
from server import remote_agent
from tests import test_kind_flow as kind_flow
from tests import test_panel_cards as cards
from tests.test_more_agents_flows import S, text

NEWB = f"@newb:{S}"


class FirstAgentAI(kind_flow.KindFlow):
    def setUp(self):
        super().setUp()
        for name, value in (("open_dm", lambda agent, password, person: "!agentdm"),):
            p = mock.patch.object(remote_agent, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("ensure_user", mock.Mock(return_value=("agent-pw", False))), ("shared_secret", lambda: "s")):
            p = mock.patch.object(remote_agent.setup_accounts, name, value)   # Synapse: faked
            p.start()
            self.addCleanup(p.stop)
        self.lp._welcomes = {NEWB: {"dm": "!dm", "offer": "$offer", "done": []}}
        self.lp.dm_rooms = {NEWB: "!dm"}
        self.lp.client = SimpleNamespace(rooms={"!r": SimpleNamespace(users={NEWB: 0}, invited_users={})})
        self.lp.is_person = lambda r, u: True
        for target, name in ((kind_flow.on_computer.connect_kinds, "problem"),):
            p = mock.patch.object(target, name, return_value=None)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(kind_flow.on_computer.connect_kinds.subprocess, "run", return_value=mock.Mock(returncode=0))
        p.start()
        self.addCleanup(p.stop)

    def say_(self, words: str) -> str:
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(NEWB, words)))
        return self.said[-1][1]

    def first_ai(self) -> dict:
        found = registry.find(NEWB, None)
        return {k: found[k] for k in pm_kinds.FIELDS if found.get(k)}

    def test_bring_my_own_using_a_kind_runs_that_kind(self):
        message = self.say_("bring my own using ollama qwen3:8b")
        self.assertIn("**newb-agent**", message)
        self.assert_message_names(message, "ollama")
        self.assertIn("It runs Ollama", message)                  # and how to choose another, until it connects
        answer, install, saved, _ = self.run_on_computer(message)
        self.assertEqual(answer["user_id"], f"@newb-agent:{S}")
        self.assertEqual((answer["agent_kind"], answer["model"]), ("ollama", "qwen3:8b"))
        self.assertEqual((install.args[2], saved["model"]), ("ollama", "qwen3:8b"))

    def test_plain_bring_my_own_says_how_to_choose_and_a_bad_choice_says_why(self):
        message = self.say_("bring my own")
        self.assert_message_names(message, None)
        self.assertIn("bring my own using", message)
        self.assertIn("My Hagency", message)
        self.assertEqual(self.say_("bring my own using sk-proj-abcdef0123456789xyz"), pm_kinds.NO_KEYS)

    def test_the_picker_in_my_hagency_sets_up_the_first_agent(self):
        self.assertTrue(console_agents.first_pick(self.lp, NEWB))   # no agent yet: the picker shows
        picked = json.dumps({"name": "", "agent_kind": "hermes", "model": "", "base_url": ""})
        self.assertIn("Setting up your agent", self.run_(console.act(self.lp, "!r", NEWB, "add", picked)))
        message = self.said[-1][1]
        self.assertIn("**newb-agent**", message)
        self.assert_message_names(message, "hermes")
        answer, install, _, _ = self.run_on_computer(message)
        self.assertEqual((answer["agent_kind"], install.args[2]), ("hermes", "hermes"))

    def test_it_can_be_chosen_again_until_it_connects_then_it_stays(self):
        self.say_("bring my own")
        self.assertEqual(self.first_ai(), {})
        self.assertTrue(console_agents.first_pick(self.lp, NEWB))   # never connected: still the first agent's picker
        self.say_(f"bring my own using remote {kind_flow.OPENROUTER} openai/gpt-4o")
        self.assertEqual(self.first_ai(), {"agent_kind": "remote", "model": "openai/gpt-4o", "base_url": kind_flow.OPENROUTER})
        self.say_("bring my own using claude")                    # the model and URL don't linger
        self.assertEqual(self.first_ai(), {"agent_kind": "claude"})
        self.assertEqual(remote_agent.credentials_for(NEWB)["agent_kind"], "claude")
        self.lp._seen = {"newb-agent": {"online": True, "ever": True}}   # it connected
        self.assertEqual(self.say_("bring my own using hermes"), pm_first.CONNECTED)
        self.assertEqual(self.first_ai(), {"agent_kind": "claude"})
        self.assertFalse(console_agents.first_pick(self.lp, NEWB))
        mine = console.my_agents(self.lp, self.lp.client.rooms["!r"], "!r", NEWB)
        self.assertTrue(console.can_add(NEWB, mine))              # now "Add an agent" adds a further one

    def test_the_one_on_my_computer_clears_an_earlier_choice(self):
        """GPT review: "AI: the one on my computer" sent no choice, so the earlier one stayed."""
        self.say_("bring my own using ollama qwen3:8b")
        auto = json.dumps({"name": "", "agent_kind": "", "model": "", "base_url": ""})   # what addValue sends now
        self.run_(console.act(self.lp, "!r", NEWB, "add", auto))
        self.assertEqual(self.first_ai(), {})
        self.assert_message_names(self.said[-1][1], None)
        self.say_("bring my own")                                # plain words keep what it has
        self.assertEqual(self.first_ai(), {})

    def test_a_second_click_while_setting_up_says_so(self):
        """GPT review: the panel promised a Connect page though nothing was started."""
        self.lp._provisioning = {NEWB}
        picked = json.dumps({"name": "", "agent_kind": "claude", "model": "", "base_url": ""})
        self.assertIn("already setting up", self.run_(console.act(self.lp, "!r", NEWB, "add", picked)))

    def test_hermes_says_where_its_line_runs(self):
        message = self.say_("bring my own using hermes")
        self.assertIn("Mac or Linux; on Windows only inside WSL", message)
        self.assertIn("invite patch", message)
        self.assertNotIn("Mac, Windows or Linux", message)

    def test_the_first_agents_picker_shows_until_one_has_connected(self):
        self.assertTrue(console_agents.first_pick(self.lp, NEWB))   # none yet
        self.say_("bring my own")
        self.assertTrue(console_agents.first_pick(self.lp, NEWB))   # one, never connected
        self.lp._seen = {"newb-agent": {"online": False, "ever": True}}
        self.assertFalse(console_agents.first_pick(self.lp, NEWB))  # it connected once: offline isn't new


KINDS = [{"kind": "ollama", "name": "Ollama (a model on your computer)", "windows": True, "needs": ["Ollama"]}]


@unittest.skipIf(cards.NODE is None, "node not installed")
class ThePickerCard(unittest.TestCase):
    """My Hagency's "My agents" card, rendered by node from static/panel_agents.js."""
    def card(self, state: dict) -> str:
        return cards.render("panel-agents.js", f"agentCards({json.dumps({'kinds': KINDS, 'personal': True, **state})})")

    def test_with_no_agent_it_offers_the_picker_for_the_first(self):
        html = self.card({"agents": [], "can_add": False, "first_pick": True})
        self.assertIn('<select id="newkind">', html)
        self.assertIn("Get my agent", html)
        self.assertNotIn('id="newagent"', html)                # the first agent needs no name
        self.assertIn("here, in this chat", cards.text_of(html))

    def test_with_only_a_never_connected_agent_it_still_offers_it(self):
        html = self.card({"agents": [cards.NEVER], "can_add": False, "first_pick": True})
        self.assertIn('<select id="newkind">', html)
        self.assertIn("Use this AI", html)

    def test_once_connected_it_is_add_an_agent_again(self):
        html = self.card({"agents": [{**cards.NEVER, "connected": True}], "can_add": True, "first_pick": False})
        self.assertIn("Add an agent", html)
        self.assertIn('id="newagent"', html)


if __name__ == "__main__":
    unittest.main()
