"""Screen test 4: Element's home page (step ①) and @pm's welcome still said "Bring your own (your Codex
or Claude Code)", though a person may run Hermes, a local Ollama model or a remote model too. Every
welcome text names the choices, from one sentence (agent_kinds.IN_WORDS).
Run: .venv/bin/python -m unittest tests.test_welcome_words"""
import unittest
from pathlib import Path

from common import agent_kinds
from server import pm_first
from tests import test_welcome as welcome

HOME = Path(__file__).resolve().parent.parent / "deploy" / "element_page" / "home.html"
STALE = "Codex or Claude Code)"


class WelcomeWords(welcome.Base):
    def test_a_newcomers_welcome_names_every_kind_and_how_to_choose(self):
        self.run_(self.lp.on_welcome_member(welcome.WELCOME_ROOM, welcome.member(welcome.NEW)))
        offer = self.said[0][1]
        self.assertIn(agent_kinds.IN_WORDS, offer)
        self.assertIn("bring my own using ollama", offer)
        self.assertNotIn("your Codex or Claude Code", offer)

    def test_the_words_name_every_kind(self):
        for kind in ("Codex", "Claude Code", "Hermes", "Ollama", "remote"):
            self.assertIn(kind, agent_kinds.IN_WORDS)
        self.assertEqual(len(agent_kinds.KINDS), 5)              # a new kind: say it here too

    def test_the_home_page_and_the_server_agent_reply_say_the_same(self):
        page = HOME.read_text(encoding="utf-8")
        self.assertIn(f"Bring your own ({agent_kinds.IN_WORDS}, on your computer)", page)
        self.assertNotIn(STALE, page)
        self.assertIn(agent_kinds.IN_WORDS, pm_first.SERVER_REPLY)


if __name__ == "__main__":
    unittest.main()
