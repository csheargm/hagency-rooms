"""admin-bot keeps one sign-in (common/admin_session.py). Overnight live test 2026-11-23: each admin call signed
in afresh and Synapse's per-account limit made invite tokens wait minutes ("waiting 333s")."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from common import admin_session
from common import local_signup


class Kept(unittest.TestCase):
    def setUp(self):
        self.file = Path(tempfile.mkdtemp()) / "data" / ".admin-bot-session.json"
        self.logins, self.calls = [], []
        self.tokens = iter(["t1", "t2", "t3"])
        self.unknown = set()

        def login(user, password):
            self.logins.append(user)
            return next(self.tokens)

        def api(method, path, body=None, token=None):
            self.calls.append((path, token))
            if token in self.unknown:
                return {"_status": 401, "errcode": "M_UNKNOWN_TOKEN"}
            return {"ok": True}
        for p in (mock.patch.object(admin_session, "FILE", self.file),
                  mock.patch.object(admin_session.setup_accounts, "login", login),
                  mock.patch.object(admin_session, "api", api)):
            p.start()
            self.addCleanup(p.stop)

    def test_one_sign_in_serves_many_calls_and_processes(self):
        for _ in range(3):                                   # three separate tool runs (invite.sh token ...)
            call = admin_session.caller("admin-bot", "pw")
            self.assertEqual(call("GET", "/x", None), {"ok": True})
        self.assertEqual(self.logins, ["admin-bot"])
        self.assertNotIn("/_matrix/client/v3/logout", [p for p, _ in self.calls])
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)   # beside the password: this account only
        self.assertEqual(json.loads(self.file.read_text())["token"], "t1")

    def test_an_hour_old_session_is_signed_out_and_replaced(self):
        admin_session.caller("admin-bot", "pw")
        self.file.write_text(json.dumps({"token": "t1", "at": time.time() - admin_session.MAX_AGE_S - 1}))
        call = admin_session.caller("admin-bot", "pw")
        call("GET", "/x", None)
        self.assertIn(("/_matrix/client/v3/logout", "t1"), self.calls)
        self.assertEqual(self.calls[-1], ("/x", "t2"))

    def test_a_session_signed_out_elsewhere_is_replaced_and_the_call_made_again(self):
        call = admin_session.caller("admin-bot", "pw")
        self.unknown.add("t1")
        self.assertEqual(call("GET", "/x", None), {"ok": True})
        self.assertEqual(self.calls[-2:], [("/x", "t1"), ("/x", "t2")])
        self.assertEqual(len(self.logins), 2)

    def test_a_damaged_or_future_dated_file_means_a_fresh_sign_in(self):
        self.file.parent.mkdir(parents=True)
        for bad in ("{not json", json.dumps({"token": 5}), json.dumps({"token": "x", "at": time.time() + 999})):
            self.file.write_text(bad)
            self.assertIsNone(admin_session.saved())

    def test_admin_call_uses_the_kept_session(self):
        with mock.patch.object(local_signup.setup_accounts, "ensure_user", return_value=("pw", False)), \
                mock.patch.object(local_signup.setup_accounts, "shared_secret", return_value="s"), \
                mock.patch.object(local_signup, "load_env", return_value={}):
            local_signup.admin_call("GET", "/a", None)
            local_signup.admin_call("GET", "/b", None)
        self.assertEqual(self.logins, ["admin-bot"])
        self.assertEqual([p for p, _ in self.calls], ["/a", "/b"])


if __name__ == "__main__":
    unittest.main()
