"""Sign-in with Google and GitHub (sso_login.py, ADR 0013). Run from deploy/: python3 -m unittest test_sso_login"""
import contextlib
import io
import json
import runpy
import sys
import unittest
from unittest import mock

import jinja2

import envfile
import sso_login
from configure_synapse import configure
from test_signup import BASE, GENERATED, OPEN, TOKEN_ONLY, run_main, value

GOOGLE = {"HAGENCY_GOOGLE_CLIENT_ID": "123456789012-abcdefgh12345678.apps.googleusercontent.com",
          "HAGENCY_GOOGLE_CLIENT_SECRET": "GOCSPX-google-secret-value_123"}
GITHUB = {"HAGENCY_GITHUB_CLIENT_ID": "Ov23liAbCdEfGhIjKlMn",
          "HAGENCY_GITHUB_CLIENT_SECRET": "0123456789abcdef0123456789abcdef01234567"}
SECRETS = (GOOGLE["HAGENCY_GOOGLE_CLIENT_SECRET"], GITHUB["HAGENCY_GITHUB_CLIENT_SECRET"])


def providers(text: str) -> dict:
    return {p["idp_id"]: p for p in json.loads(value(text, "oidc_providers"))}


def render(template: str, user: dict) -> str:
    """As Synapse does (handlers/oidc.py: Environment, its localpart_from_email filter, then strip)."""
    env = jinja2.Environment()
    env.filters["localpart_from_email"] = lambda email: email.rsplit("@", 1)[0]
    return env.from_string(template).render(user=user).strip()


class Providers(unittest.TestCase):
    def test_google_only_leaves_password_sign_up_token_only(self):
        out = configure(GENERATED, {**BASE, **GOOGLE})
        self.assertEqual(list(providers(out)), ["google"])
        g = providers(out)["google"]
        self.assertEqual((g["issuer"], g["idp_brand"], g["idp_name"]), ("https://accounts.google.com/", "google", "Google"))
        self.assertEqual(g["scopes"], ["openid", "profile", "email"])
        self.assertEqual(g["client_secret"], SECRETS[0])
        self.assertNotIn("email_template", g["user_mapping_provider"]["config"])   # GPT review: no unchallenged binding
        self.assertEqual(value(out, "registration_requires_token"), "true")
        self.assertNotIn("registrations_require_3pid", out)
        self.assertNotIn("enable_registration_without_verification", out)

    def test_github_only(self):
        gh = providers(configure(GENERATED, {**BASE, **GITHUB}))
        self.assertEqual(list(gh), ["github"])
        p = gh["github"]
        self.assertFalse(p["discover"])
        self.assertEqual(p["authorization_endpoint"], "https://github.com/login/oauth/authorize")
        self.assertEqual(p["token_endpoint"], "https://github.com/login/oauth/access_token")
        self.assertEqual(p["userinfo_endpoint"], "https://api.github.com/user")
        self.assertEqual(p["user_profile_method"], "userinfo_endpoint")
        self.assertEqual(p["user_mapping_provider"]["config"]["subject_claim"], "id")
        self.assertEqual(p["idp_brand"], "github")

    def test_both_with_open_sign_up_too(self):
        out = configure(GENERATED, {**OPEN, **GOOGLE, **GITHUB})
        self.assertEqual(list(providers(out)), ["google", "github"])
        self.assertEqual(value(out, "registrations_require_3pid"), "[email]")
        self.assertEqual(json.loads(value(out, "sso")), {"client_whitelist": ["https://rooms.example.org/"]})

    def test_none_is_byte_identical_to_today(self):
        empty = {k: "" for k in [*GOOGLE, *GITHUB]}
        self.assertEqual(configure(GENERATED, {**BASE, **empty}), TOKEN_ONLY)

    def test_partial_or_bad_keys_stay_off(self):
        for e in ({"HAGENCY_GOOGLE_CLIENT_ID": GOOGLE["HAGENCY_GOOGLE_CLIENT_ID"]},
                  {"HAGENCY_GITHUB_CLIENT_SECRET": GITHUB["HAGENCY_GITHUB_CLIENT_SECRET"]},
                  {**GOOGLE, "HAGENCY_GOOGLE_CLIENT_ID": "not-a-google-id"},
                  {**GITHUB, "HAGENCY_GITHUB_CLIENT_SECRET": "has spaces in it, twenty"}):
            self.assertEqual(configure(GENERATED, {**BASE, **e}), TOKEN_ONLY, list(e))

    def test_removing_the_keys_turns_sign_in_off_again(self):
        on = configure(GENERATED, {**BASE, **GOOGLE, **GITHUB})
        self.assertEqual(configure(on, BASE), TOKEN_ONLY)
        self.assertEqual(list(providers(configure(on, {**BASE, **GITHUB}))), ["github"])

    def test_running_twice_changes_nothing(self):
        for e in ({**BASE, **GOOGLE}, {**BASE, **GITHUB}, {**OPEN, **GOOGLE, **GITHUB}):
            once = configure(GENERATED, e)
            self.assertEqual(configure(once, e), once)


class NoTakeover(unittest.TestCase):
    def test_existing_accounts_are_never_linked(self):
        for p in providers(configure(GENERATED, {**BASE, **GOOGLE, **GITHUB})).values():
            self.assertIs(p["allow_existing_users"], False)
            self.assertFalse(p["user_mapping_provider"]["config"].get("confirm_localpart", False))

    def test_names_like_agents_or_bots_get_a_suffix(self):
        """A taken name (pm, anna-agent) is skipped by Synapse itself (pm1, ...); a free agent-like name
        must not be taken either, or @pm couldn't make that person's agent later (identity.py)."""
        out = configure(GENERATED, {**BASE, **GOOGLE, **GITHUB})
        gh = providers(out)["github"]["user_mapping_provider"]["config"]["localpart_template"]
        g = providers(out)["google"]["user_mapping_provider"]["config"]["localpart_template"]
        self.assertEqual(render(gh, {"login": "Anna-Agent"}), "anna-agent-gh")
        self.assertEqual(render(gh, {"login": "admin-bot"}), "admin-bot-gh")
        self.assertEqual(render(gh, {"login": "octocat"}), "octocat")
        self.assertEqual(render(g, {"email": "Anna.Lee@gmail.com"}), "anna.lee")
        self.assertEqual(render(g, {"email": "cx-agent@gmail.com"}), "cx-agent-g")
        self.assertEqual(render(g, {"email": "x-bot @gmail.com"}), "x-bot-g")          # trimmed first
        # their agent would be "server-agent" / "trial-agent", read as the shared server agent (ADR 0025)
        self.assertEqual(render(gh, {"login": "Server"}), "server-gh")
        self.assertEqual(render(g, {"email": "trial@gmail.com"}), "trial-g")
        self.assertEqual(render(gh, {"login": "servers"}), "servers")


class ReservedNamesModule(unittest.TestCase):
    def modules(self, e: dict) -> list:
        return [m["module"] for m in json.loads(value(configure(GENERATED, e), "modules"))]

    def test_loaded_with_public_sign_up_only(self):
        guard = "hagency_reserved_names.ReservedNames"
        self.assertIn(guard, self.modules({**BASE, **GOOGLE}))
        self.assertIn(guard, self.modules({**BASE, **GITHUB}))
        self.assertIn(guard, self.modules(OPEN))
        self.assertNotIn(guard, self.modules(BASE))
        self.assertNotIn(guard, self.modules({**OPEN, "HAGENCY_OPEN_SIGNUP": "0"}))

    def test_it_reserves_pm_owner_and_the_agent_and_bot_suffixes(self):
        config = next(m["config"] for m in json.loads(value(configure(GENERATED, OPEN), "modules"))
                      if m["module"].startswith("hagency_reserved_names"))
        self.assertEqual(config, {"names": ["pm", "owner", "server", "trial"], "suffixes": ["-agent", "-bot"]})


class NoSecrets(unittest.TestCase):
    def test_output_names_providers_but_never_a_secret(self):
        full = {**BASE, **GOOGLE, **GITHUB, "PM_UID": "1001", "ADMIN_DOMAIN": "admin.example.org"}
        for values in (full, {**full, "HAGENCY_GITHUB_CLIENT_SECRET": ""}):
            printed = run_main(values)
            self.assertIn("sign-in with google on", printed)
            for secret in SECRETS:
                self.assertNotIn(secret, printed)
        self.assertIn("HAGENCY_GITHUB_CLIENT_SECRET is empty", sso_login.describe({**GITHUB, "HAGENCY_GITHUB_CLIENT_SECRET": ""}))
        self.assertEqual(sso_login.describe({}), "sign-in with google off, github off")

    def test_envfile_get_refuses_the_client_secrets(self):
        for key in ("HAGENCY_GOOGLE_CLIENT_SECRET", "HAGENCY_GITHUB_CLIENT_SECRET"):
            self.assertIn(key, envfile.PRIVATE)
            with mock.patch.object(sys, "argv", ["envfile.py", "get", key]), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    runpy.run_path(envfile.__file__, run_name="__main__")

    def test_the_example_has_the_names_with_empty_values(self):
        example = envfile.EXAMPLE.read_text()
        for key in [*GOOGLE, *GITHUB]:
            self.assertIn(f"\n{key}=\n", example)


if __name__ == "__main__":
    unittest.main()
