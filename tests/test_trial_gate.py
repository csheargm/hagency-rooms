"""The trial build gate's counts and the account-age read (server/trial_gate.py, ADR 0027).
Run: .venv/bin/python -m unittest tests.test_trial_gate"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import trial_gate as tg
from tests.trial_build_kit import FREE


class Counts(unittest.TestCase):
    def test_counted_per_github_account_kept_on_disk_and_a_new_day_resets_only_the_day(self):
        now = [86400 * 100.0]
        path = Path(tempfile.mkdtemp()) / "u.json"
        usage = tg.Usage(path, clock=lambda: now[0])
        usage.take(7), usage.take(7), usage.take(8)
        self.assertEqual(tg.Usage(path, clock=lambda: now[0]).counts(7), (2, 2, 3))   # a restart keeps them
        now[0] += 86400
        self.assertEqual(usage.counts(7), (0, 2, 0))
        self.assertEqual(tg.left_today(usage, 7, FREE), 3)
        self.assertEqual(tg.left_today(usage, 7, (3, 3)), 1)


class Age(unittest.TestCase):
    def test_read_once_a_day_and_an_unknown_age_is_retried(self):
        reads = []

        def read(gid):
            reads.append(gid)
            return None if len(reads) == 1 else 1234.0
        with mock.patch.object(tg, "_ages", {}):
            self.assertIsNone(asyncio.run(tg.account_created(5, 0, read)))
            self.assertIsNone(asyncio.run(tg.account_created(5, 1, read)))           # a failure is kept a while too
            self.assertEqual(asyncio.run(tg.account_created(5, tg.AGE_FAIL_CACHE_S, read)), 1234.0)
            self.assertEqual(asyncio.run(tg.account_created(5, tg.AGE_FAIL_CACHE_S + 1, read)), 1234.0)
        self.assertEqual(reads, [5, 5])

    def test_the_read_needs_no_token_and_a_failure_is_unknown(self):
        with mock.patch.object(tg.urllib.request, "urlopen", side_effect=tg.urllib.error.URLError("down")) as op:
            self.assertIsNone(tg._created_at(5))
        self.assertNotIn("Authorization", op.call_args[0][0].headers)


if __name__ == "__main__":
    unittest.main()
