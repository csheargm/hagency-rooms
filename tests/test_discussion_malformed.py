"""Unit test: a malformed event in a room's history never stops @pm reading it (pm_plan.discussion).
GPT re-check 2026-11-26: a non-string body raised in .lstrip(), so every request needing the history failed.
Run: .venv/bin/python -m unittest tests.test_discussion_malformed"""
import unittest
from types import SimpleNamespace
from unittest import mock

from server import pm_plan


class Malformed(unittest.TestCase):
    def test_bad_bodies_and_contents_are_skipped(self):
        chunk = [{"type": "m.room.message", "sender": "@anna:s", "content": {"body": "keep this"}},
                 {"type": "m.room.message", "sender": "@eve:s", "content": {"body": ["not", "text"]}},
                 {"type": "m.room.message", "sender": "@eve:s", "content": "not a dict"},
                 {"type": "m.room.message", "sender": "@eve:s", "content": {"body": {"x": 1}}}]   # newest first
        me = SimpleNamespace(user_id="@pm:s", client=SimpleNamespace(access_token="t"))
        with mock.patch.object(pm_plan, "api", return_value={"chunk": chunk}):
            self.assertEqual(pm_plan.Planner.discussion(me, "!r"), "anna: keep this")


if __name__ == "__main__":
    unittest.main()
