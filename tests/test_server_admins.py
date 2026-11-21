"""Unit tests: who is one of this server's admins (server_admins.py). Run: .venv/bin/python -m unittest tests.test_server_admins"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import server_admins
from common.saved_dict import SavedDict


class Admins(unittest.TestCase):
    def setUp(self):
        server_admins._known["file"] = SavedDict(Path(tempfile.mkdtemp()) / "admins.json")
        self.addCleanup(server_admins._known.clear)

    def test_asked_once_then_remembered(self):
        with mock.patch.object(server_admins.local_signup, "admin_call", return_value={"admin": True}) as call:
            self.assertTrue(server_admins.is_admin("@owner:s"))
            self.assertTrue(server_admins.is_admin("@owner:s"))
        call.assert_called_once()
        with mock.patch.object(server_admins.time, "time", return_value=server_admins.time.time() + 2 * server_admins.REMEMBER_S), \
                mock.patch.object(server_admins.local_signup, "admin_call", return_value={"admin": False}):
            self.assertFalse(server_admins.is_admin("@owner:s"))                    # demoted: seen after a day
        with mock.patch.object(server_admins.local_signup, "admin_call", return_value={"admin": False}):
            self.assertFalse(server_admins.is_admin("@anna:s"))

    def test_when_synapse_cant_say_it_is_asked_again_later(self):
        for failure in (mock.Mock(side_effect=SystemExit("login failed")), mock.Mock(return_value={"_status": 500})):
            with mock.patch.object(server_admins.local_signup, "admin_call", failure):
                self.assertFalse(server_admins.is_admin("@owner:s"))
        self.assertNotIn("@owner:s", server_admins.known())


if __name__ == "__main__":
    unittest.main()
