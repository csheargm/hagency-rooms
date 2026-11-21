"""Unit tests: a person from another Matrix server finds @pm (a private chat, or "@pm I want an agent")
and their agent follows them, by full ID (pm_welcome.py, pm_follow.py, remote_people.py).
Run: .venv/bin/python -m unittest tests.test_remote_welcome"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import JoinResponse

from common import identity
from common import registry
from server import remote_people
from server.loop import Loop
from tests.test_remote_people import ALICE, EVE, S, use_registry

PM = f"@pm:{S}"


class Joining(unittest.TestCase):
    """A matrix.org person opens a private chat with @pm: their way in."""
    def setUp(self):
        use_registry(self, {"anna": {"agent": "hermes"}})
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.started_ms, self.lp.busy, self.lp._welcomes = PM, 0, set(), {}
        self.lp._add_offers = {}                      # pm_agents.py: never the real data folder
        self.said = []

        async def say(room_id, body, mention=None, **k):
            self.said.append((room_id, body, mention))
            return "$offer" if body.startswith("Welcome to") else "$x"

        async def thumb(*a, **k):
            return None
        self.lp.say, self.lp.offer_thumb = say, thumb
        self.lp.private_room = mock.AsyncMock(return_value="!mine")
        self.others = []                                                           # who else is in their chat
        members = lambda room_id: SimpleNamespace(events=[
            {"type": "m.room.member", "state_key": u, "content": {"membership": "join"}}
            for u in (PM, self.sender, *self.others)])
        self.lp.client = mock.Mock(join=mock.AsyncMock(return_value=JoinResponse("!theirs")), room_leave=mock.AsyncMock(),
                                   room_get_state=mock.AsyncMock(side_effect=members))

    def invite(self, sender=ALICE, direct=True, to=PM):
        self.sender = sender
        event = SimpleNamespace(sender=sender, state_key=to, membership="invite", content={"is_direct": direct})
        asyncio.run(self.lp.on_invite(SimpleNamespace(room_id="!theirs"), event))

    def test_pm_joins_and_offers_in_its_own_chat_and_says_where(self):
        self.invite()
        self.lp.client.join.assert_awaited_once_with("!theirs")
        self.lp.private_room.assert_awaited_once_with(ALICE)
        self.assertEqual(self.said[0][0], "!mine")
        self.assertIn("Get your agent", self.said[0][1])
        self.assertEqual(self.said[1][0], "!theirs")
        self.assertIn("Invites", self.said[1][1])
        self.assertEqual(self.said[1][2], ALICE)                                   # a mention of them
        self.assertEqual(self.lp._welcomes[ALICE]["dm"], "!mine")
        self.lp.client.room_leave.assert_awaited_once_with("!theirs")             # GPT review 23: theirs can grow

    def test_only_private_chats_only_to_me_only_people(self):
        self.invite(direct=False)                                                  # a group room from elsewhere
        self.invite(to="@anna:" + S)
        self.invite(sender="@spam-bot:matrix.org")
        self.invite(sender="@alice-agent:matrix.org")
        self.lp.client.join.assert_not_called()

    def test_a_group_room_that_says_direct_is_left(self):
        """GPT review 23: "is_direct" is only the sender's word; the room is asked after joining."""
        self.others = ["@bob:matrix.org"]
        self.invite()
        self.lp.client.room_leave.assert_awaited_once_with("!theirs")
        self.assertEqual(self.said, [])
        self.lp.private_room.assert_not_called()

    def test_one_person_cant_use_up_the_hour(self):
        for _ in range(5):
            self.invite()
        self.assertEqual(self.lp.client.join.await_count, remote_people.DM_JOINS_PER_PERSON)
        self.invite(sender="@bob:matrix.org")
        self.assertEqual(self.lp.client.join.await_count, remote_people.DM_JOINS_PER_PERSON + 1)

    def test_not_when_the_server_turned_it_off(self):
        with mock.patch.dict("os.environ", {"HAGENCY_ALLOW_REMOTE": "off"}):
            self.invite()
        self.lp.client.join.assert_not_called()

    def test_at_most_so_many_chats_an_hour(self):
        self.lp._dm_joins = remote_people.DmJoins(limit=2)
        for i in range(4):
            self.invite(sender=f"@p{i}:matrix.org")
        self.assertEqual(self.lp.client.join.await_count, 2)

    def test_someone_with_an_agent_is_told_so(self):
        use_registry(self, {ALICE: {"agent": "claude"}})                           # e.g. set up by an operator
        self.invite()
        self.assertIn("already have an agent", self.said[0][1])

    def test_asking_in_a_shared_room_works_for_them_too(self):
        self.lp.addressed_text = lambda event, room=None: event.body
        room = SimpleNamespace(room_id="!project", canonical_alias=None)
        event = SimpleNamespace(sender=ALICE, body="I want an agent", source={"content": {"body": "x"}})
        self.assertTrue(asyncio.run(self.lp.on_welcome_text(room, event)))
        self.lp.private_room.assert_awaited_once_with(ALICE)
        with mock.patch.dict("os.environ", {"HAGENCY_ALLOW_REMOTE": "0"}):
            asyncio.run(self.lp.on_welcome_text(room, SimpleNamespace(sender=EVE, body="I want an agent",
                                                                       source={"content": {}})))
        self.assertIn(remote_people.OFF, self.said[-1][1])


class Following(unittest.TestCase):
    """A matrix.org person's agent follows them into rooms and takes their settings, never a namesake's."""
    def setUp(self):
        use_registry(self, {ALICE: {"agent": "remote"}})
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.pending = PM, {}
        self.said = []

        async def say(room_id, body, *a, **k):
            self.said.append(body)
            return "$x"
        self.lp.say = say
        self.lp.is_welcome_room = lambda room: False
        self.lp.client = mock.Mock(room_invite=mock.AsyncMock())

    def test_their_agent_comes_along(self):
        room = SimpleNamespace(room_id="!r", users={ALICE: None}, invited_users={})
        asyncio.run(self.lp.bring_agent(room, ALICE))
        self.lp.client.room_invite.assert_awaited_once_with("!r", f"@{identity.agent_localpart(ALICE, S)}:{S}")
        asyncio.run(self.lp.bring_agent(room, EVE))
        self.assertEqual(self.lp.client.room_invite.await_count, 1)

    def test_only_their_own_agent_leaves_with_them_and_plans_for_them(self):
        """GPT review 23: a bad record (an agent claimed twice) never lets a namesake kick or use it."""
        agent = f"@{identity.agent_localpart(ALICE, S)}:{S}"
        room = SimpleNamespace(room_id="!r", users={ALICE: None, agent: None}, invited_users={})
        self.lp.client.room_kick = mock.AsyncMock()
        asyncio.run(self.lp.remove_agent(room, EVE))
        self.lp.client.room_kick.assert_not_called()
        self.lp.agents_in = lambda r: [agent]
        self.assertEqual(self.lp.own_agent(room, ALICE), agent)
        self.assertIsNone(self.lp.own_agent(room, EVE))
        use_registry(self, {ALICE: {"agent": "remote"}, "carl": {"agent_user": agent.split(":")[0][1:]}})
        self.assertIsNone(self.lp.own_agent(room, ALICE))
        asyncio.run(self.lp.remove_agent(room, ALICE))
        self.lp.client.room_kick.assert_not_called()

    def test_no_agent_follows_into_my_private_chat(self):
        """GPT review 23: follow-me invited the owner's agent into @pm's chat, so it stopped being private."""
        asyncio.run(self.lp.bring_agent(SimpleNamespace(room_id="!old", users={ALICE: None, PM: None}, name="Hagency (private)",
                                                        invited_users={}), ALICE))   # any chat I made with them
        self.lp.client.room_invite.assert_not_called()

    def test_settings_by_full_id(self):
        room = SimpleNamespace(room_id="!r")
        asyncio.run(self.lp.set_follow(room, EVE, "never"))
        self.assertEqual(registry.follow(ALICE), "always")
        asyncio.run(self.lp.set_follow(room, ALICE, "ask"))
        self.assertEqual(registry.follow(ALICE), "ask")


if __name__ == "__main__":
    unittest.main()
