"""My agents' "Get a fresh code" (console_agents.fresh_code; console.act "code").

Screen test 2026-11-23: an agent that never connected could only get a fresh code by typing a command
in @pm's chat ("resend me the code", or "add another agent called pi", which reads like adding another).
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from common import registry
from server import console  # noqa: F401  (first, as the app does: console_agents is reached through it)
from server import console_agents
from server import console_personal

ME = "@newbie1:matrix.example.org"
FIRST = {"localpart": "newbie1-agent", "label": "main", "first": True, "agent": registry.REMOTE}
PI = {"localpart": "newbie1-pi-agent", "label": "pi", "first": False, "agent": registry.REMOTE}


class FreshCode(unittest.TestCase):
    def setUp(self):
        self.loop = SimpleNamespace(ever_connected=lambda lp: lp in self.connected,
                                    private_room=mock.AsyncMock(return_value="!dm"),
                                    choose=mock.AsyncMock(), add_agent=mock.AsyncMock())
        self.connected = set()
        self.agents = {"newbie1-agent": FIRST, "newbie1-pi-agent": PI}
        patches = [mock.patch.object(registry, "find", lambda person, agent: self.agents.get(agent) if agent else FIRST),
                   mock.patch.object(registry, "owns", lambda person, lp: person == ME and lp in self.agents)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def press(self, agent, user=ME):
        return asyncio.run(console_agents.fresh_code(self.loop, user, agent))

    def test_the_first_agent_gets_the_same_connect_page_as_the_plug_button(self):
        self.assertEqual(self.press("newbie1-agent"), console_agents.FRESH)
        self.loop.choose.assert_awaited_once_with("!dm", ME, console_agents.BRING)
        self.loop.add_agent.assert_not_awaited()

    def test_a_further_agent_gets_its_own_code_not_a_new_agent(self):
        """By its slot: add_agent finishes one whose setup failed halfway, then sends its code (GPT review)."""
        self.assertEqual(self.press("newbie1-pi-agent"), console_agents.FRESH)
        self.loop.add_agent.assert_awaited_once_with("!dm", ME, "pi")
        self.loop.choose.assert_not_awaited()
        self.assertEqual(self.loop._provisioning, set())                  # not left marked busy

    def test_a_click_while_one_is_being_set_up_waits(self):
        """GPT review: a second click mid-setup said "on their way" and sent nothing."""
        self.loop._provisioning = {ME}
        self.assertEqual(self.press("newbie1-pi-agent"), console_agents.BUSY)
        self.loop.add_agent.assert_not_awaited()
        self.assertEqual(self.loop._codes_sent if hasattr(self.loop, "_codes_sent") else {}, {})   # nothing charged

    def test_one_that_has_connected_is_told_to_run_its_line_again(self):
        self.connected.add("newbie1-pi-agent")
        self.assertIn("connected before", self.press("newbie1-pi-agent"))
        self.loop.add_agent.assert_not_awaited()

    def test_someone_elses_agent_is_refused(self):
        self.assertIn("isn't one of your agents", self.press("newbie1-pi-agent", user="@other:matrix.example.org"))
        self.loop.add_agent.assert_not_awaited()

    def test_five_codes_an_hour_as_when_typed(self):
        for _ in range(5):
            self.assertEqual(self.press("newbie1-pi-agent"), console_agents.FRESH)
        self.assertEqual(self.press("newbie1-pi-agent"), console_agents.TOO_MANY)
        self.assertEqual(self.loop.add_agent.await_count, 5)

    def test_it_works_in_my_hagency(self):
        self.assertIn("code", console_personal.ACTIONS)


if __name__ == "__main__":
    unittest.main()
