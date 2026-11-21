"""Unit tests: whose agent is whose, and agents following their people. Run: .venv/bin/python -m unittest tests.test_follow"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomInviteResponse

from server import ownership
from common import registry
from server.loop import Loop

S = "bridge.localhost"
ANNA, BETTY, CARL = f"@anna:{S}", f"@betty:{S}", f"@carl:{S}"
ANNA_A, BETTY_A, STRANGER_A = f"@anna-agent:{S}", f"@betty-agent:{S}", f"@mallory-agent:{S}"


def room(*members, invited=()):
    return SimpleNamespace(room_id="!r", users={m: None for m in [*members, *invited]},
                           invited_users={m: None for m in invited},
                           power_levels=SimpleNamespace(get_user_level=lambda u: 100 if u == ANNA else 0))


class Registry(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({"anna": {"agent": "hermes"}, "betty": {"agent": "claude", "follow": "ask"},
                                    "carl": {"agent_user": "carl-claude"}}))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)


class Ownership(Registry):
    def test_an_agent_counts_only_with_its_owner_present(self):
        self.assertEqual(ownership.agents_in(room(ANNA, ANNA_A, BETTY_A)), [ANNA_A])

    def test_unregistered_agents_never_count(self):
        self.assertEqual(ownership.agents_in(room(ANNA, ANNA_A, STRANGER_A, "@mallory:" + S)), [ANNA_A])

    def test_a_differently_named_agent_works_through_the_registry(self):
        self.assertEqual(ownership.owner_id(f"@carl-claude:{S}"), CARL)
        self.assertEqual(ownership.agent_id(CARL), f"@carl-claude:{S}")

    def test_an_invited_owner_doesnt_count_yet(self):
        self.assertEqual(ownership.agents_in(room(ANNA_A, BETTY, BETTY_A, invited=[ANNA])), [BETTY_A])

    def test_a_damaged_registry_line_doesnt_crash(self):
        registry.PATH.write_text(json.dumps({"anna": None, "betty": {"follow": "ask"}}))
        self.assertEqual(registry.agent_of("anna"), "anna-agent")
        self.assertEqual(registry.follow("betty"), "ask")

    def test_settings(self):
        self.assertEqual((registry.follow("anna"), registry.follow("betty")), ("always", "ask"))
        registry.set_follow("anna", "never")
        self.assertEqual(registry.follow("anna"), "never")
        with self.assertRaises(ValueError):
            registry.set_follow("anna", "sometimes")


class FollowMe(Registry):
    def setUp(self):
        super().setUp()
        self.lp = Loop.__new__(Loop)
        self.lp.room_repos = {}
        self.lp.user_id, self.lp.pending, self.lp.started_ms = f"@pm:{S}", {}, 0
        self.lp.sync_point = SimpleNamespace(first_time=lambda e: True)
        self.said, self.invited, self.kicked = [], [], []

        async def say(room_id, body, *a, **k):
            self.said.append(body)
            return f"$m{len(self.said)}"

        async def invite(room_id, user):
            self.invited.append(user)
            return RoomInviteResponse()

        async def kick(room_id, user, reason=None):
            self.kicked.append(user)
        self.lp.say = say
        self.lp.client = SimpleNamespace(room_invite=invite, room_kick=kick)

    def member(self, r, who, membership, prev):
        ev = SimpleNamespace(state_key=who, membership=membership, prev_membership=prev, event_id="$e",
                             server_timestamp=int(time.time() * 1000))
        asyncio.run(self.lp.on_member(r, ev))

    def test_always_brings_the_agent_along(self):
        self.member(room(ANNA), ANNA, "join", "invite")
        self.assertEqual(self.invited, [ANNA_A])
        self.assertIn("anna joined, so their agent came along", self.said[-1])

    def test_an_agent_that_never_connected_is_only_invited(self):
        """Screen test: "<name> joined, so their agent came along" for an agent whose computer had never
        connected: it was only invited. Say so; "came along" once it's online."""
        registry.update("dora", None, agent=registry.REMOTE, ready=True)
        dora = f"@dora:{S}"
        self.lp._seen = {}                                       # pm_presence.py: never seen online
        self.member(room(dora), dora, "join", "invite")
        self.assertEqual(self.invited, [f"@dora-agent:{S}"])
        self.assertIn("I invited dora's agent; it joins once it's connected on their computer", self.said[-1])
        self.assertNotIn("came along", self.said[-1])
        self.lp._seen = {"dora-agent": {"online": True, "ever": True}}
        self.member(room(dora), dora, "join", "invite")
        self.assertIn("dora joined, so their agent came along", self.said[-1])

    def test_ask_asks_first_and_only_the_owner_confirms(self):
        r = room(ANNA, BETTY)
        self.member(r, BETTY, "join", "invite")
        self.assertEqual(self.invited, [])
        self.assertIn("Bring your agent (betty-agent)", self.said[-1])
        asyncio.run(self.lp.on_reaction(r, SimpleNamespace(sender=ANNA, key="✅", reacts_to="$m1")))
        self.assertEqual(self.invited, [])                       # not anna's agent to bring
        asyncio.run(self.lp.on_reaction(r, SimpleNamespace(sender=BETTY, key="✅", reacts_to="$m1")))
        self.assertEqual(self.invited, [BETTY_A])

    def test_never_brings_nothing(self):
        registry.set_follow("anna", "never")
        self.member(room(ANNA), ANNA, "join", "invite")
        self.assertEqual(self.invited, [])

    def test_the_agent_leaves_with_its_owner(self):
        self.member(room(ANNA_A, BETTY), ANNA, "leave", "join")
        self.assertEqual(self.kicked, [ANNA_A])

    def test_people_without_an_agent_and_agents_themselves_are_ignored(self):
        self.member(room(), "@dave:" + S, "join", "invite")
        self.member(room(), ANNA_A, "join", "invite")
        self.assertEqual(self.invited, [])

    def test_changing_my_own_setting_in_words(self):
        from server import pm_brain
        self.lp.generation, self.lp.busy, self.lp.boards = {}, set(), {}
        with mock.patch.object(pm_brain, "BRAIN", "off"):
            ev = SimpleNamespace(sender=BETTY, body="@pm always bring my agent",
                                 source={"content": {"m.mentions": {"user_ids": [f"@pm:{S}"]}}})
            asyncio.run(self.lp.on_human(room(BETTY), ev))
        self.assertEqual(registry.follow("betty"), "always")
        self.assertIn("I'll bring your agent along", self.said[-1])


if __name__ == "__main__":
    unittest.main()
