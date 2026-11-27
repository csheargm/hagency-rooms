"""Unit tests: the trial agent's settings and daily caps (ADR 0025). Run: .venv/bin/python -m unittest tests.test_trial_limits"""
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import trial_limits

V1, V2 = "@newbie1:bridge.localhost", "@newbie2:bridge.localhost"
NOON = datetime.datetime(2026, 11, 27, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
NEXT_DAY = NOON + 13 * 3600


class Settings(unittest.TestCase):
    def test_off_without_a_key_and_on_with_one(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(trial_limits.enabled())
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "  "}, clear=True):
            self.assertFalse(trial_limits.enabled())      # blank is unset
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20}, clear=True):
            self.assertTrue(trial_limits.enabled())
        with mock.patch.dict(os.environ, {"OLLAMA_API_KEY": "k" * 20, "TRIAL_PER_SERVER": "0"}, clear=True):
            self.assertFalse(trial_limits.enabled())      # a server cap of 0 turns it off

    def test_defaults_and_overrides(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual((trial_limits.number("TRIAL_PER_VISITOR"), trial_limits.number("TRIAL_PER_SERVER")), (50, 1000))
            self.assertEqual(trial_limits.api_url(), "https://ollama.com/api/chat")
        with mock.patch.dict(os.environ, {"TRIAL_PER_VISITOR": "5", "TRIAL_TIMEOUT_S": "abc"}, clear=True):
            self.assertEqual(trial_limits.number("TRIAL_PER_VISITOR"), 5)
            self.assertEqual(trial_limits.number("TRIAL_TIMEOUT_S"), 90)   # malformed: the default

    def test_reset_time_is_said_in_words(self):
        self.assertEqual(trial_limits.resets_in(NOON), "at 00:00 UTC (in 12 h 0 min)")


class Caps(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "data" / "trial-usage.json"
        self.now = NOON
        env = mock.patch.dict(os.environ, {"TRIAL_PER_VISITOR": "3", "TRIAL_PER_SERVER": "5"})
        env.start()
        self.addCleanup(env.stop)

    def usage(self):
        return trial_limits.Usage(self.path, clock=lambda: self.now)

    def test_a_visitor_gets_their_daily_messages_then_the_visitor_cap(self):
        u = self.usage()
        self.assertEqual([u.take(V1) for _ in range(4)], [None, None, None, trial_limits.VISITOR])
        self.assertEqual(u.used(V1), 3)
        self.assertIsNone(u.take(V2))                         # someone else still may

    def test_the_server_cap_stops_everyone(self):
        u = self.usage()
        for _ in range(3):
            u.take(V1)
        self.assertEqual([u.take(V2), u.take(V2), u.take(V2)], [None, None, trial_limits.SERVER])

    def test_counts_survive_a_restart(self):
        u = self.usage()
        for _ in range(3):
            u.take(V1)
        self.assertEqual(self.usage().take(V1), trial_limits.VISITOR)   # a new process reads the file

    def test_a_new_utc_day_starts_again(self):
        u = self.usage()
        for _ in range(3):
            u.take(V1)
        self.now = NEXT_DAY
        self.assertIsNone(u.take(V1))
        self.assertIsNone(self.usage().take(V1))

    def test_the_limit_is_said_once_a_day(self):
        u = self.usage()
        self.assertEqual([u.first_notice(V1, "visitor"), u.first_notice(V1, "visitor")], [True, False])
        self.assertTrue(u.first_notice(V1, "server"))
        self.now = NEXT_DAY
        self.assertTrue(u.first_notice(V1, "visitor"))

    def test_a_damaged_file_starts_the_day_at_zero(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[not a dict")
        self.assertIsNone(self.usage().take(V1))


if __name__ == "__main__":
    unittest.main()
