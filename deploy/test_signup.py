"""Open sign-up gated on a confirmed email (signup.py, ADR 0013). Run from deploy/:
python3 -m unittest test_signup"""
import contextlib
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import configure_synapse
import envfile
import signup
from configure_synapse import configure

DATA = Path(__file__).resolve().parent / "testdata"
GENERATED = (DATA / "generated_homeserver.yaml").read_text()   # what `synapse generate` writes
# configure_synapse.py's output for GENERATED before open sign-up existed (made with master 5e62733):
# with no SMTP the server must keep exactly this.
TOKEN_ONLY = (DATA / "token_only_homeserver.yaml").read_text()
BASE = {"POSTGRES_PASSWORD": "ab" * 32, "MATRIX_DOMAIN": "matrix.example.org",
        "SERVER_NAME": "matrix.example.org", "ROOMS_DOMAIN": "rooms.example.org"}
PASSWORD = "s3cr#t-P[ass]w0rd{x}"                            # odd characters the .env rules allow
SMTP = {"SMTP_HOST": "smtp.example.net", "SMTP_PORT": "587", "SMTP_USER": "postmaster@example.org",
        "SMTP_PASS": PASSWORD, "SMTP_FROM": "no-reply@example.org"}
OPEN = {**BASE, **SMTP, "HAGENCY_OPEN_SIGNUP": "1"}
# What a VM's .env holds once envfile.py has merged .env.example's defaults in, before the owner fills SMTP.
EXAMPLE_DEFAULTS = {**BASE, "SMTP_HOST": "", "SMTP_PORT": "587", "SMTP_USER": "", "SMTP_PASS": "",
                    "SMTP_FROM": "no-reply@hagencylabs.xyz", "HAGENCY_OPEN_SIGNUP": "1",
                    "HAGENCY_GOOGLE_CLIENT_ID": "", "HAGENCY_GOOGLE_CLIENT_SECRET": "",
                    "HAGENCY_GITHUB_CLIENT_ID": "", "HAGENCY_GITHUB_CLIENT_SECRET": ""}


def value(text: str, key: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith(f"{key}:")]
    if len(lines) != 1:
        raise AssertionError(f"{key}: {len(lines)} lines")
    return lines[0].split(":", 1)[1].strip()


class OpenSignup(unittest.TestCase):
    def test_smtp_opens_sign_up_but_only_with_a_confirmed_email(self):
        out = configure(GENERATED, OPEN)
        self.assertEqual(value(out, "enable_registration"), "true")
        self.assertEqual(value(out, "registration_requires_token"), "false")
        self.assertEqual(value(out, "registrations_require_3pid"), "[email]")
        self.assertEqual(value(out, "disable_msisdn_registration"), "true")    # no phone numbers
        self.assertEqual(value(out, "allow_guest_access"), "false")
        self.assertNotIn(signup.NO_VERIFICATION, out)

    def test_the_email_block_reaches_synapse_intact(self):
        email = json.loads(value(configure(GENERATED, OPEN), "email"))          # JSON is YAML
        self.assertEqual(email["smtp_host"], "smtp.example.net")
        self.assertEqual(email["smtp_port"], 587)
        self.assertEqual(email["smtp_pass"], PASSWORD)
        self.assertTrue(email["require_transport_security"])                  # STARTTLS or nothing
        self.assertNotIn("force_tls", email)
        self.assertEqual(email["notif_from"], "Hagency Rooms <no-reply@example.org>")
        self.assertEqual(email["app_name"], "Hagency Rooms")
        self.assertEqual(email["client_base_url"], "https://rooms.example.org")
        self.assertFalse(email["enable_notifs"])

    def test_port_465_uses_tls_from_the_start(self):
        email = json.loads(value(configure(GENERATED, {**OPEN, "SMTP_PORT": "465"}), "email"))
        self.assertTrue(email["force_tls"])
        self.assertTrue(email["enable_tls"])

    def test_sign_up_and_email_are_rate_limited_and_rc_login_is_kept(self):
        out = configure(GENERATED, OPEN)
        self.assertEqual(value(out, "rc_registration"), signup.RC_REGISTRATION)
        self.assertEqual(value(out, "rc_3pid_validation"), signup.RC_3PID_VALIDATION)
        self.assertEqual(value(out, "request_token_inhibit_3pid_errors"), "true")   # no "is this email taken?"
        self.assertEqual(value(out, "rc_login"), "{address: {per_second: 0.02, burst_count: 20}}")
        self.assertIn("default (5, then one per ~5 min per address)", Path(configure_synapse.__file__).read_text())

    def test_new_accounts_still_join_the_welcome_room(self):
        out = configure(GENERATED, OPEN)
        self.assertEqual(value(out, "auto_join_rooms"), '["#welcome:matrix.example.org"]')
        self.assertEqual(value(out, "auto_join_mxid_localpart"), "pm")


class StaysClosed(unittest.TestCase):
    def test_without_smtp_the_config_is_byte_identical_to_token_only(self):
        for e in (BASE, EXAMPLE_DEFAULTS, {**BASE, "HAGENCY_OPEN_SIGNUP": "1"}):
            self.assertEqual(configure(GENERATED, e), TOKEN_ONLY)

    def test_partial_smtp_stays_closed(self):
        for key in signup.SMTP_KEYS:
            e = {**OPEN, key: ""}
            self.assertEqual(configure(GENERATED, e), TOKEN_ONLY, key)
            self.assertIn(f"{key} is empty", signup.describe(e))

    def test_a_value_that_does_not_validate_stays_closed(self):
        for key, bad in (("SMTP_HOST", "smtp host"), ("SMTP_PORT", "2526"), ("SMTP_PASS", "short"),
                         ("SMTP_PASS", "has$dollar"), ("SMTP_FROM", "Name <a@b.org>")):
            self.assertEqual(configure(GENERATED, {**OPEN, key: bad}), TOKEN_ONLY, key)

    def test_a_bad_value_never_stops_the_deploy_that_closes_sign_up(self):
        """GPT review: envfile.py check failing would leave the old, open config running."""
        opened = configure(GENERATED, OPEN)
        closing = {**OPEN, "HAGENCY_OPEN_SIGNUP": "0", "SMTP_PASS": "typo with spaces"}
        full = {**closing, "PM_UID": "1001", "ADMIN_DOMAIN": "admin.example.org"}
        self.assertEqual(envfile.validate(full), [])                      # bootstrap goes on
        self.assertEqual(configure(opened, closing), TOKEN_ONLY)
        self.assertIn("SMTP_PASS doesn't look right", run_main(full))

    def test_signup_mode_for_the_health_check(self):
        for e, want in ((BASE, "token"), (OPEN, "open"), ({**OPEN, "HAGENCY_OPEN_SIGNUP": "0"}, "token")):
            with mock.patch.object(envfile, "read", return_value=e), mock.patch.object(sys, "argv", ["signup.py", "mode"]), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                runpy.run_path(signup.__file__, run_name="__main__")
            self.assertEqual(out.getvalue(), want + "\n")

    def test_the_switch_off_stays_closed_but_keeps_email_for_password_reset(self):
        for switch in ("0", "", "yes", None):
            e = {**OPEN, "HAGENCY_OPEN_SIGNUP": switch} if switch is not None else {**BASE, **SMTP}
            out = configure(GENERATED, e)
            self.assertEqual(value(out, "registration_requires_token"), "true", switch)
            self.assertNotIn("registrations_require_3pid", out)
            self.assertNotIn("rc_registration", out)
            self.assertIn("\nemail: {", out)                               # password reset by email

    def test_closing_again_removes_what_opened_it(self):
        opened = configure(GENERATED, OPEN)
        self.assertEqual(configure(opened, BASE), TOKEN_ONLY)              # SMTP removed: as before
        closed = configure(opened, {**OPEN, "HAGENCY_OPEN_SIGNUP": "0"})
        self.assertEqual(value(closed, "registration_requires_token"), "true")
        self.assertNotIn("registrations_require_3pid", closed)

    def test_registration_without_verification_is_never_kept(self):
        planted = GENERATED + f"{signup.NO_VERIFICATION}: true\n"
        for e in (BASE, OPEN, {**OPEN, "HAGENCY_OPEN_SIGNUP": "0"}):
            self.assertNotIn(signup.NO_VERIFICATION, configure(planted, e))

    def test_open_never_comes_without_a_required_email(self):
        for e in (BASE, EXAMPLE_DEFAULTS, OPEN, {**OPEN, "SMTP_PASS": ""}, {**OPEN, "HAGENCY_OPEN_SIGNUP": "0"}):
            out = configure(GENERATED, e)
            if value(out, "registration_requires_token") == "false":
                self.assertEqual(value(out, "registrations_require_3pid"), "[email]")


class Idempotent(unittest.TestCase):
    def test_running_twice_changes_nothing_in_every_mode(self):
        for e in (BASE, EXAMPLE_DEFAULTS, OPEN, {**OPEN, "SMTP_PORT": "465"}, {**OPEN, "HAGENCY_OPEN_SIGNUP": "0"}):
            once = configure(GENERATED, e)
            self.assertEqual(configure(once, e), once)
            self.assertEqual(once.count("\nemail:"), 1 if signup.settings(e) else 0)


def run_main(values: dict) -> str:
    """configure_synapse.py as bootstrap.sh runs it, on a temporary .env and homeserver.yaml: all it prints."""
    with tempfile.TemporaryDirectory() as d:
        hs = Path(d) / "homeserver.yaml"
        hs.write_text(GENERATED)
        env = Path(d) / ".env"
        env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        out = io.StringIO()
        with mock.patch.object(envfile, "ENV", env), mock.patch.object(sys, "argv", ["configure_synapse.py", str(hs)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            runpy.run_path(configure_synapse.__file__, run_name="__main__")
        if os.stat(hs).st_mode & 0o777 != 0o600:                           # it holds the SMTP password
            raise AssertionError("homeserver.yaml isn't mode 600")
        return out.getvalue()


class NoSecretOutput(unittest.TestCase):
    def test_bootstrap_output_names_the_mode_but_never_a_value(self):
        full = {**OPEN, "PM_UID": "1001", "ADMIN_DOMAIN": "admin.example.org"}
        for values in (full, {**full, "SMTP_PASS": ""}, {**full, "HAGENCY_OPEN_SIGNUP": "0"}):
            printed = run_main(values)
            self.assertIn("sign-up", printed)
            for secret in (PASSWORD, "postmaster@example.org", "ab" * 32):
                self.assertNotIn(secret, printed)
        self.assertIn("SMTP_PASS is empty", run_main({**full, "SMTP_PASS": ""}))

    def test_describe_and_validation_never_carry_a_value(self):
        bad = {**OPEN, "SMTP_PASS": "x y z $ no"}
        for text in [signup.describe(OPEN), signup.describe(bad), *envfile.validate(bad)]:
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn("x y z", text)

    def test_envfile_get_refuses_smtp_credentials(self):
        for key in ("SMTP_PASS", "SMTP_USER", "SMTP_HOST"):
            with mock.patch.object(sys, "argv", ["envfile.py", "get", key]), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as stop:
                    runpy.run_path(envfile.__file__, run_name="__main__")
            self.assertEqual(stop.exception.code, 2)
            self.assertNotIn(PASSWORD, out.getvalue())

    def test_the_example_holds_names_and_placeholders_only(self):
        example = envfile.EXAMPLE.read_text()
        for key in signup.SMTP_KEYS + (signup.SWITCH,):
            self.assertIn(f"\n{key}=", example)
        self.assertIn("\nSMTP_PASS=\n", example)
        self.assertIn("\nSMTP_USER=\n", example)
        self.assertEqual(envfile.validate(EXAMPLE_DEFAULTS | {"PM_UID": "1001", "ADMIN_DOMAIN": "a.example.org"}), [])


if __name__ == "__main__":
    unittest.main()
