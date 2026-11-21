"""Unit tests for who the bridge answers. Run: .venv/bin/python -m unittest tests.test_rules"""
import unittest
from types import SimpleNamespace

from adapters.agents.bridge import Bridge

ANNA, BETTY, MALLORY = "@anna:bridge.localhost", "@betty:bridge.localhost", "@mallory:bridge.localhost"
AGENT, OTHER_AGENT = "@anna-agent:bridge.localhost", "@betty-agent:bridge.localhost"


def room(members, invited=()):
    users = {m: None for m in [*members, *invited]}   # nio's room.users includes invitees
    return SimpleNamespace(users=users, invited_users={m: None for m in invited},
                           joined_count=len(members), invited_count=len(invited))


def msg(sender, body, mentions=None):
    content = {"body": body, **({"m.mentions": {"user_ids": mentions}} if mentions is not None else {})}
    return SimpleNamespace(sender=sender, body=body, source={"content": content})


class Rules(unittest.TestCase):
    def setUp(self):
        self.b = Bridge("anna", "codex")
        self.lounge = room([ANNA, BETTY, AGENT, OTHER_AGENT])

    def test_mention_pill_addresses_agent(self):
        self.assertTrue(self.b.is_addressed(self.lounge, msg(BETTY, "hey", [AGENT])))

    def test_plain_name_addresses_agent(self):
        self.assertTrue(self.b.is_addressed(self.lounge, msg(ANNA, "anna-agent: hi")))

    def test_other_agent_name_does_not_address(self):
        self.assertFalse(self.b.is_addressed(self.lounge, msg(ANNA, "betty-agent: hi", [OTHER_AGENT])))

    def test_dm_always_addresses(self):
        self.assertTrue(self.b.is_addressed(room([ANNA, AGENT]), msg(ANNA, "hello")))

    def test_non_member_refused(self):
        self.assertIsNotNone(self.b.should_ignore(self.lounge, msg(MALLORY, "anna-agent: rm -rf")))

    def test_other_agent_refused_loop_guard(self):
        self.assertIsNotNone(self.b.should_ignore(self.lounge, msg(OTHER_AGENT, "anna-agent: hi")))

    def test_own_message_refused(self):
        self.assertIsNotNone(self.b.should_ignore(self.lounge, msg(AGENT, "anna-agent")))

    def test_owner_accepted(self):
        self.assertIsNone(self.b.should_ignore(self.lounge, msg(ANNA, "anna-agent: hi")))

    def test_other_member_refused_by_default(self):
        self.assertIsNotNone(self.b.should_ignore(self.lounge, msg(BETTY, "anna-agent: cat ~/.ssh/id_rsa")))

    def test_other_member_accepted_with_allow_members(self):
        b = Bridge("anna", "codex", allow_members=True)
        self.assertIsNone(b.should_ignore(self.lounge, msg(BETTY, "anna-agent: hi")))

    def test_invited_but_not_joined_refused(self):
        r = room([ANNA, AGENT], invited=[MALLORY])
        self.assertIsNotNone(Bridge("anna", "codex", True).should_ignore(r, msg(MALLORY, "anna-agent: hi")))

    def test_two_joined_plus_invitee_is_not_a_dm(self):
        self.assertFalse(self.b.is_addressed(room([ANNA, AGENT], invited=[BETTY]), msg(ANNA, "hello")))

    def test_malformed_mentions_do_not_crash(self):
        for bad in ("x", {"user_ids": "@anna-agent:bridge.localhost"}, {"user_ids": None}):
            m = msg(ANNA, "hello")
            m.source["content"]["m.mentions"] = bad
            self.assertFalse(self.b.is_addressed(self.lounge, m))

    def test_workdir_distinct_for_ids_that_sanitize_alike(self):
        self.assertNotEqual(self.b.workdir("!a:b.c"), self.b.workdir("!a_b:c"))


if __name__ == "__main__":
    unittest.main()
