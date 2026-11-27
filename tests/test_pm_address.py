"""Unit tests: a message addressed to @pm with Element's mention pill (pm_address.py).
Live test 2026-11-26: "@Product Manager retry #13" from a phone got "I didn't catch that".
Run: .venv/bin/python -m unittest tests.test_pm_address"""
import unittest
from types import SimpleNamespace

from common import intents
from server.pm_address import Addressing, unaddressed

PM = "@pm:matrix.hagencylabs.xyz"
LINK = f"https://matrix.to/#/{PM}"


class Room(Addressing):
    user_id, dm_rooms = PM, {}

    def direct_chats(self):
        return {}

    def welcomes(self):
        return {}


def event(body, mentions=(PM,)):
    return SimpleNamespace(body=body, sender="@anna:matrix.hagencylabs.xyz",
                           source={"content": {"body": body, "m.mentions": {"user_ids": list(mentions)}}})


class Pill(unittest.TestCase):
    def said(self, body, mentions=(PM,)):
        return Room().addressed_text(event(body, mentions), SimpleNamespace(room_id="!r", users={}))

    def test_every_pill_form_reads_as_the_words_after_it(self):
        for body in (f"[Product Manager]({LINK}) retry #13", f"[Product Manager]({LINK}): retry #13",
                     f"{LINK} retry #13", f"{PM} retry #13", f"{PM}: retry #13", "Product Manager retry #13"):
            with self.subTest(body=body):
                text = self.said(body)
                self.assertEqual(text, "retry #13")
                self.assertEqual((intents.classify(text), intents.issue_named(text)), (intents.RETRY, 13))

    def test_a_pill_of_someone_else_is_not_stripped(self):
        other = "https://matrix.to/#/@mallory:evil.example"
        self.assertIsNone(self.said(f"[Product Manager]({other}) stop", mentions=()))
        self.assertIsNone(self.said("@pm:evil.example stop", mentions=()))

    def test_a_pill_later_in_the_message_still_counts_as_a_mention(self):
        self.assertEqual(self.said(f"can you show the plan [Product Manager]({LINK})"),
                         f"can you show the plan [Product Manager]({LINK})")

    def test_private_chat_reading_drops_the_pill_too(self):
        self.assertEqual(unaddressed(f"[Product Manager]({LINK}) add another agent", PM), "add another agent")
        self.assertEqual(unaddressed("add another agent", PM), "add another agent")


if __name__ == "__main__":
    unittest.main()
