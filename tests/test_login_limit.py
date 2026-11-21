"""Regression tests: Synapse's per-account sign-in limit (429) is waited out, never read as a wrong
password. Seen live (2026-11-21): several deploys in an hour used up @pm's sign-ins; setup_accounts
read the 429 as "login failed", tried to re-create @pm (M_USER_IN_USE) and the container
crash-looped, each restart signing in again. Run: .venv/bin/python -m unittest tests.test_login_limit"""
import asyncio
import unittest
from unittest import mock

from nio import LoginError, LoginResponse

from common import lifecycle
from common import setup_accounts

LIMITED = {"_status": 429, "errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 2000}


class SignInLimit(unittest.TestCase):
    def test_a_429_is_waited_out_then_the_sign_in_works(self):
        waits = []
        answers = iter([LIMITED, LIMITED, {"access_token": "t"}])
        with mock.patch.object(setup_accounts, "api", side_effect=lambda *a, **k: next(answers)), \
                mock.patch("builtins.print"):
            self.assertEqual(setup_accounts.try_login("pm", "pw", sleep=waits.append), "t")
        self.assertEqual(waits, [2.0, 2.0])

    def test_a_wrong_password_is_still_none_at_once(self):
        with mock.patch.object(setup_accounts, "api", return_value={"_status": 403, "errcode": "M_FORBIDDEN"}):
            self.assertIsNone(setup_accounts.try_login("pm", "bad", sleep=self.fail))

    def test_the_wait_has_an_end_and_is_never_a_wrong_password(self):
        """GPT review 23: a None after the wait sent ensure_user on to re-register the account."""
        waits = []
        with mock.patch.object(setup_accounts, "api", return_value={**LIMITED, "retry_after_ms": 10**9}), \
                mock.patch("builtins.print"), self.assertRaises(setup_accounts.AccountError):
            setup_accounts.try_login("pm", "pw", sleep=waits.append)
        self.assertEqual(sum(waits), setup_accounts.LOGIN_WAIT_S)
        with mock.patch.object(setup_accounts, "api", return_value={**LIMITED, "retry_after_ms": 10**9}), \
                mock.patch.object(setup_accounts, "register") as register, mock.patch("builtins.print"), \
                mock.patch.object(setup_accounts.time, "sleep"), \
                self.assertRaises(setup_accounts.AccountError):
            setup_accounts.ensure_user({"PW_PM": "pw"}, "pm", "s", verify=True)
        register.assert_not_called()

    def test_the_matrix_client_waits_too(self):
        waits = []
        answers = iter([LoginError("Too Many Requests", "M_LIMIT_EXCEEDED", retry_after_ms=3000),
                        LoginResponse("@pm:s", "D", "t")])
        client = mock.Mock(user_id="@pm:s", login=mock.AsyncMock(side_effect=lambda *a, **k: next(answers)))

        async def sleep(s):
            waits.append(s)
        resp = asyncio.run(lifecycle.login(client, "pw", "pm", sleep=sleep))
        self.assertIsInstance(resp, LoginResponse)
        self.assertEqual(waits, [3.0])
        client.login = mock.AsyncMock(return_value=LoginError("Invalid password", "M_FORBIDDEN"))
        self.assertEqual(asyncio.run(lifecycle.login(client, "pw", "pm", sleep=sleep)).status_code, "M_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
