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

    def test_more_forms_and_without_the_mentions_field(self):
        enc = "https://matrix.to/#/%40pm%3Amatrix.hagencylabs.xyz"
        for body in (f"{LINK}: retry #13", f"{LINK}, retry #13", f"[PM]({enc}) retry #13", f"{PM}, retry #13"):
            for mentions in ((PM,), ()):
                with self.subTest(body=body, mentions=mentions):
                    self.assertEqual(self.said(body, mentions), "retry #13")

    def test_links_and_ids_that_name_more_or_other_than_pm_are_refused(self):
        for body in (f"[Product Manager]({LINK}/extra) stop", f"{LINK}/extra stop", "@pm.ops:example.org stop",
                     "@pm:matrix.hagencylabs.xyz.evil stop",
                     "https://matrix.to/#/@pm:MATRIX.HAGENCYLABS.XYZ stop", "@pm+ops:x stop", "@pm:[::1] stop",
                     "@pm:retry #13", "@pm:x; stop", f"{PM}:: retry #13", f"[Product Manager](https://evil.example/#/{PM}) stop"):
            with self.subTest(body=body):
                self.assertIsNone(self.said(body, mentions=()))

    def test_the_short_forms_still_work(self):
        for body in ("@pm retry #13", "pm: retry #13", "@pm: retry #13", "Product Manager, retry #13"):
            with self.subTest(body=body):
                self.assertEqual(self.said(body, mentions=()), "retry #13")

    def test_an_unclosed_link_is_read_in_linear_time(self):
        import time
        body = "[x](" + "https://matrix.to/#/" * 2000
        start = time.perf_counter()
        self.said(body, mentions=())
        self.assertLess(time.perf_counter() - start, 0.05)

    def test_a_pill_later_in_the_message_still_counts_as_a_mention(self):
        self.assertEqual(self.said(f"can you show the plan [Product Manager]({LINK})"),
                         f"can you show the plan [Product Manager]({LINK})")

    def test_private_chat_reading_drops_the_pill_too(self):
        self.assertEqual(unaddressed(f"[Product Manager]({LINK}) add another agent", PM), "add another agent")
        self.assertEqual(unaddressed("add another agent", PM), "add another agent")


if __name__ == "__main__":
    unittest.main()
