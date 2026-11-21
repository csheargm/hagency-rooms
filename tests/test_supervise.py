"""Unit tests for supervise.py lifecycle ownership. Run: .venv/bin/python -m unittest tests.test_supervise"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from client import supervise


class Ownership(unittest.TestCase):
    def test_save_and_crash_in_the_same_tick_start_one_copy(self):
        kid = supervise.Child("pm", ["true"])
        kid.proc = SimpleNamespace(returncode=1)          # exited, and a code save arrives too
        self.assertTrue(kid.claim())                      # the restart claims it first...
        self.assertFalse(kid.claim())                     # ...so the crash revive does not

    def test_claim_is_released_after_revive_spawns(self):
        kid = supervise.Child("pm", ["true"])
        kid.proc = SimpleNamespace(returncode=1)
        kid.start = mock.AsyncMock()
        kid.claim()
        with mock.patch.object(supervise, "BACKOFF_S", (0,)):
            asyncio.run(supervise.revive(kid))
        kid.start.assert_awaited_once()
        self.assertTrue(kid.claim())

    def test_no_respawn_while_shutting_down(self):
        kid = supervise.Child("pm", ["true"])
        kid.closing = True
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            asyncio.run(kid.start())
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
