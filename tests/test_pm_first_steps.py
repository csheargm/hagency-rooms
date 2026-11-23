"""@pm's answer to "what should we do first?" (intents.START, pm_chat.FIRST_STEPS).

Live test 2026-11-23: in a new project room, "@pm what should we do first in this project?" only got
"Noted. When you're ready, say "let's build it" …", which doesn't answer the question.
"""
import unittest
from unittest import mock

from common import intents
from common import room_state
from server import pm_chat
from tests.test_pm_chat import ANNA, Talk


class Phrases(unittest.TestCase):
    def test_questions_about_where_to_start(self):
        for text in ("@pm what should we do first in this project?", "what's next", "what next?",
                     "how do we get started?", "where do I start", "next steps please"):
            self.assertEqual(intents.classify(text), intents.START, text)

    def test_commands_keep_their_meaning(self):
        for text, intent in (("let's build it", intents.PLAN), ("start building", intents.PLAN),
                             ("where are we", intents.STATUS), ("help", intents.HELP), ("stop", intents.OFF),
                             ("what do we do about the login bug, stop it?", intents.OFF)):
            self.assertEqual(intents.classify(text), intent, text)


class Answer(Talk):
    def test_with_a_repo_it_says_so_and_goes_on(self):
        self.tell(ANNA, "@pm what should we do first in this project?")
        self.assertIn("✓ This room works on **your-name/hagency-hello**.", self.said[-1])
        self.assertIn("let's build it", self.said[-1])
        self.assertNotIn("Noted", self.said[-1])

    def test_without_a_repo_picking_one_comes_first(self):
        with mock.patch.object(room_state, "repo_of", mock.AsyncMock(return_value=None)):
            self.tell(ANNA, "@pm what's next?")
        self.assertIn("1. " + pm_chat.REPO_STEP, self.said[-1])

    def test_where_are_we_in_a_room_without_a_repo_says_the_next_step(self):
        """Live test 2026-11-23: a new room answered only "Mode: stopped: …", which read as broken."""
        self.lp.client.rooms = {"!r": self.room}
        self.lp.agents_in = lambda room: []
        with mock.patch.object(room_state, "repo_of", mock.AsyncMock(return_value=None)):
            self.tell(ANNA, "@pm where are we?")
        self.assertTrue(self.said[-1].startswith("No GitHub repo yet. " + pm_chat.REPO_STEP), self.said[-1])
        self.tell(ANNA, "@pm where are we?")                               # with one: as before
        self.assertTrue(self.said[-1].startswith("Mode: "), self.said[-1])


if __name__ == "__main__":
    unittest.main()
