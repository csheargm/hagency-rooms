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

        self.limited = 0                                     # how many sign-ins Synapse holds back first

        def login(user, password, sleep=None):
            if self.limited:
                self.limited -= 1
                sleep(0.01)                                  # try_login's wait: admin_session turns it into a way out
            self.logins.append(user)
            return next(self.tokens)

        def api(method, path, body=None, token=None):
            self.calls.append((path, token))
            if token in self.unknown:
                return {"_status": 401, "errcode": "M_UNKNOWN_TOKEN"}
            if path == "/forbidden":
                return {"_status": 403, "errcode": "M_FORBIDDEN"}
            return {"ok": True}
        for p in (mock.patch.object(admin_session, "FILE", self.file),
                  mock.patch.object(admin_session.setup_accounts, "try_login", login),
                  mock.patch.object(admin_session, "api", api)):
            p.start()
            self.addCleanup(p.stop)

    def test_one_sign_in_serves_many_calls_and_runs(self):
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
        for bad in ("{not json", json.dumps({"token": 5}), json.dumps({"token": "x", "at": None}),
                    json.dumps({"token": "x", "at": "bad"}), json.dumps({"token": "x", "at": True}),
                    '{"token": "x", "at": 1' + "0" * 400 + '}'):
            self.file.write_text(bad)
            self.assertIsNone(admin_session.read(), bad)    # GPT review: a bad "at" never breaks every admin call
        self.file.write_text(json.dumps({"token": "x", "at": time.time() + 999}))
        admin_session.caller("admin-bot", "pw")
        self.assertEqual(self.logins, ["admin-bot"])         # from the future: replaced too

    def test_a_token_replaced_by_another_process_is_used_not_replaced_again(self):
        """GPT review: a caller holding an old token must not sign in over a healthy replacement."""
        call = admin_session.caller("admin-bot", "pw")       # t1
        self.file.write_text(json.dumps({"token": "other", "at": time.time()}))   # another process rotated it
        self.unknown.add("t1")
        call("GET", "/x", None)
        self.assertEqual(self.calls[-1], ("/x", "other"))
        self.assertEqual(self.logins, ["admin-bot"])

    def test_two_callers_at_once_share_one_sign_in(self):
        """GPT review: both saw no session and both signed in, leaving one untracked."""
        import threading
        slow = admin_session.setup_accounts.try_login

        def login(user, password, sleep=None):
            time.sleep(0.2)                                  # the window where the other caller looks
            return slow(user, password, sleep)
        with mock.patch.object(admin_session.setup_accounts, "try_login", login):
            got = []
            threads = [threading.Thread(target=lambda: got.append(admin_session.current("admin-bot", "pw")))
                       for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual((self.logins, got), (["admin-bot"], ["t1", "t1"]))

    def test_a_held_back_sign_in_waits_outside_the_lock_and_uses_a_session_made_meanwhile(self):
        """GPT review: the lock was held through Synapse's sign-in wait (up to 15 minutes)."""
        self.limited = 1
        free = []

        def sleep(pause):
            with admin_session.locked():                     # would deadlock if the wait held the lock
                free.append(pause)
                admin_session.write("made-meanwhile")       # another process signed in
        with mock.patch.object(admin_session.time, "sleep", sleep):
            self.assertEqual(admin_session.current("admin-bot", "pw"), "made-meanwhile")
        self.assertEqual((free, self.logins), ([0.01], []))

    def test_the_wait_is_bounded_and_an_old_session_is_signed_out_once(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps({"token": "old", "at": time.time() - admin_session.MAX_AGE_S - 1}))
        self.limited, slept = 99, []
        with mock.patch.object(admin_session.time, "sleep", slept.append), \
                mock.patch.object(admin_session, "limited", lambda pause: (_ for _ in ()).throw(admin_session.Limited(600))), \
                self.assertRaises(admin_session.setup_accounts.AccountError):
            admin_session.current("admin-bot", "pw")
        self.assertEqual(sum(slept), admin_session.setup_accounts.LOGIN_WAIT_S)   # 600 + 300, not 600 + 600
        self.assertEqual(self.calls.count(("/_matrix/client/v3/logout", "old")), 1)

    def test_a_temp_file_that_cant_be_made_signs_the_session_out(self):
        with mock.patch.object(admin_session.tempfile, "mkstemp", side_effect=OSError("no inodes")), \
                self.assertRaises(OSError):
            admin_session.current("admin-bot", "pw")
        self.assertIn(("/_matrix/client/v3/logout", "t1"), self.calls)

    def test_other_errors_are_returned_not_retried(self):
        call = admin_session.caller("admin-bot", "pw")
        self.assertEqual(call("GET", "/forbidden", None)["errcode"], "M_FORBIDDEN")
        self.assertEqual([p for p, _ in self.calls], ["/forbidden"])
        self.assertEqual(self.logins, ["admin-bot"])

    def test_a_session_that_cant_be_kept_is_signed_out(self):
        with mock.patch.object(admin_session.os, "replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            admin_session.caller("admin-bot", "pw")
        self.assertIn(("/_matrix/client/v3/logout", "t1"), self.calls)
        self.assertEqual([p.name for p in self.file.parent.iterdir() if p.suffix == ".tmp"], [])

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
