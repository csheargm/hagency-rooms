"""Unit tests: the Synapse module that keeps our own names out of public sign-up (ADR 0013).
Run from deploy/: python3 -m unittest test_reserved_names"""
import asyncio
import enum
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "synapse_modules"))
import hagency_reserved_names as m  # noqa: E402

CONFIG = {"names": ["pm", "owner"], "suffixes": ["-agent", "-bot"]}
EMAIL = {"medium": "email", "address": "a@example.org"}


class Behaviour(enum.Enum):                     # synapse.spam_checker_api.RegistrationBehaviour's values
    ALLOW = "allow"
    SHADOW_BAN = "shadow_ban"
    DENY = "deny"


class FakeApi:
    def register_spam_checker_callbacks(self, **callbacks):
        self.callbacks = callbacks


def check(email, username, provider=None):
    api = FakeApi()
    module = m.ReservedNames(m.ReservedNames.parse_config(CONFIG), api)
    callback = api.callbacks["check_registration_for_spam"]
    assert callback == module.check_registration_for_spam
    with mock.patch.object(m, "RegistrationBehaviour", Behaviour):
        return asyncio.run(callback(email, username, [], provider))


class ReservedNames(unittest.TestCase):
    def test_open_sign_up_and_sso_cannot_take_our_names(self):
        for name in ("pm", "PM", "owner", "anna-agent", "cx-laptop-agent", "admin-bot", "x-bot "):
            self.assertIs(check(EMAIL, name), Behaviour.DENY, name)                    # email sign-up
            self.assertIs(check(None, name, "oidc-github"), Behaviour.DENY, name)      # Google / GitHub

    def test_people_names_pass(self):
        for name in ("anna", "pm1", "agent", "pmx", "robot", "anna-agentx", None):
            self.assertIs(check(EMAIL, name), Behaviour.ALLOW, name)
            self.assertIs(check(None, name, "oidc-google"), Behaviour.ALLOW, name)

    def test_trusted_provisioning_is_left_alone(self):
        """The admin API (@pm making agents, admin-bot, owner) has no email and no provider."""
        for name in ("pm", "anna-agent", "admin-bot", "owner"):
            self.assertIs(check(None, name), Behaviour.ALLOW, name)

    def test_a_bad_config_stops_synapse(self):
        for bad in (None, {}, {"names": [], "suffixes": ["-agent"]}, {"names": "pm", "suffixes": ["-bot"]}):
            with self.assertRaises(ValueError):
                m.ReservedNames.parse_config(bad)


if __name__ == "__main__":
    unittest.main()
