"""Unit tests, step 4: several agents per person in @pm's chat and the console (pm_agents.py,
pm_presence.py, pm_follow.py, console.py); task routing is in test_routing.py. Run: .venv/bin/python -m unittest tests.test_more_agents_flows"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomInviteResponse

from server import agent_removal
from server import console
from common import matrix_calls
from server import pm_agents
from server import pm_presence
from common import registry
from server import remote_agent
from common import room_state
from server.loop import Loop

S = "bridge.localhost"
CX, BETTY, PM = f"@cx:{S}", f"@betty:{S}", f"@pm:{S}"
CXA, CXC, BA = f"@cx-agent:{S}", f"@cx-codex-agent:{S}", f"@betty-agent:{S}"


def text(sender, body):
    return SimpleNamespace(sender=sender, body=body, source={"content": {"body": body}})


def click(sender, key, to):
    return SimpleNamespace(sender=sender, key=key, reacts_to=to, server_timestamp=10**13)


class Base(unittest.TestCase):
    people = {CX: {"agent": "remote", "dm": "!a"}, BETTY: {"agent": "remote", "dm": "!b"}}

    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps(self.people))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        d = mock.patch.object(agent_removal, "deactivated", return_value=set())   # Synapse's list: nobody
        d.start()
        self.addCleanup(d.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.started_ms, self.lp.busy = PM, 0, set()
        self.lp._welcomes, self.lp._add_offers, self.lp._seen, self.lp.dm_rooms = {}, {}, {}, {CX: "!dm"}
        self.lp.pending = {}
        self.said, self.buttons = [], []

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
            return f"$m{len(self.said)}"

        async def thumb(room_id, event_id, key="👍"):
            self.buttons.append((room_id, event_id, key))
        self.lp.say, self.lp.offer_thumb = say, thumb
        self.lp.private_room = mock.AsyncMock(return_value="!dm")
        self.lp.only_us = mock.AsyncMock(return_value=True)

    def run_(self, coro):
        async def go():
            out = await coro
            await asyncio.gather(*self.lp.busy)               # the set-up runs in the background
            return out
        return asyncio.run(go())


class Provisioning(Base):
    """remote_agent's set-up faked (the account and the code): what the add flows need (test_agent_picker too)."""
    real_provision = staticmethod(remote_agent.provision_more)          # before setUp fakes it
    def setUp(self):
        super().setUp()
        self.made = []

        def provision_more(person, label, picked=None):
            self.made.append((person, label))
            found = registry.find(person, label)              # as remote_agent: an unfinished one is finished
            return (found if found and not found["first"] else registry.add_agent(person, label, picked=picked))["localpart"]
        p = mock.patch.object(remote_agent, "provision_more", provision_more)
        p.start()
        self.addCleanup(p.stop)
        self.codes = []
        p2 = mock.patch.object(remote_agent.CODES, "issue", lambda person, agent=None: self.codes.append(agent) or "AAAAA-BBBBB")
        p2.start()
        self.addCleanup(p2.stop)


class AddInChat(Provisioning):
    def test_plus_asks_one_question_then_sends_that_agents_connect_page(self):
        self.lp._add_offers = {"$plus": {"person": CX, "dm": "!dm"}}
        self.lp._seen = {"cx-agent": {"online": True, "ever": True}}   # ➕ is only offered once one connected
        room = SimpleNamespace(room_id="!dm")
        self.assertTrue(self.run_(self.lp.on_welcome_reaction(room, click(CX, "➕", "$plus"))))
        self.assertEqual(self.said[-1], ("!dm", pm_agents.ASK_NAME))
        self.assertTrue(self.run_(self.lp.on_welcome_text(room, text(CX, "Codex"))))
        self.assertEqual(self.made, [(CX, "codex")])
        self.assertEqual(self.codes, ["cx-codex-agent"])                                 # a code for THAT agent
        page = self.said[-1][1]
        self.assertIn("**cx-codex-agent**", page)
        self.assertIn("/connect/start#AAAAA-BBBBB", page)
        self.assertEqual(self.buttons[-1][2], "➕")                                      # and another ➕

    def test_typed_with_a_name_needs_no_question(self):
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(CX, "add another agent called laptop")))
        self.assertEqual(self.made, [(CX, "laptop")])
        self.assertNotIn(pm_agents.ASK_NAME, [b for _, b in self.said])

    def test_anywhere_else_it_goes_to_the_private_chat(self):
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!project"), text(CX, "@pm add another agent")))
        self.assertEqual(self.said, [("!dm", pm_agents.ASK_NAME)])

    def test_only_the_owners_click_in_that_chat_and_cancel_and_bad_names(self):
        self.lp._add_offers = {"$plus": {"person": CX, "dm": "!dm"}}
        self.assertFalse(self.run_(self.lp.on_add_reaction(SimpleNamespace(room_id="!dm"), click(BETTY, "➕", "$plus"))))
        self.assertFalse(self.run_(self.lp.on_add_reaction(SimpleNamespace(room_id="!x"), click(CX, "➕", "$plus"))))
        room = SimpleNamespace(room_id="!dm")
        self.run_(self.lp.on_welcome_text(room, text(CX, "add another agent")))
        self.run_(self.lp.on_welcome_text(room, text(CX, "main")))                        # the first one's name
        self.assertIn("already have an agent called main", self.said[-1][1])
        self.run_(self.lp.on_welcome_text(room, text(CX, "cancel")))
        self.assertEqual(self.said[-1][1], "OK, no new agent.")
        self.assertFalse(self.run_(self.lp.on_add_text(room, text(CX, "codex"))))       # no question open now
        self.assertEqual(self.made, [])

    def test_no_first_agent_or_at_the_cap(self):
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!x"), text("@new:" + S, "@pm add another agent")))
        self.assertIn("don't have an agent", self.said[-1][1])
        registry.add_agent(CX, "one")
        registry.add_agent(CX, "two")
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(CX, "add another agent called three")))
        self.assertIn("the most one person can have", self.said[-1][1])
        self.assertEqual(self.made, [])

    def test_a_plus_in_a_project_room_asks_in_the_private_chat(self):
        """GPT 24: the answer is only taken in our private chat, so the question must be there."""
        self.lp._add_offers = {"$plus": {"person": CX, "dm": "!project"}}
        self.run_(self.lp.on_add_reaction(SimpleNamespace(room_id="!project"), click(CX, "➕", "$plus")))
        self.assertEqual(self.said, [("!dm", pm_agents.ASK_NAME)])

    def test_a_failed_set_up_can_be_finished_even_at_the_cap_and_names_are_exact(self):
        registry.add_agent(CX, "one")
        registry.add_agent(CX, "two")                                                   # reserved, never finished
        self.run_(self.lp.start_add("!dm", CX, "two"))
        self.assertEqual(self.made, [(CX, "two")])                                       # GPT 24: not stranded
        self.run_(self.lp.start_add("!dm", CX, "co-dex"))
        self.assertIn("the most one person", self.said[-1][1])
        registry.update(CX, "two", dm="!d")
        self.made.clear()
        with mock.patch.dict("os.environ", {"HAGENCY_AGENTS_PER_PERSON": "5"}):
            for typed in ("co-dex", "codex-"):
                self.run_(self.lp.start_add("!dm", CX, pm_agents.NAME_ANSWER_RE.search(typed).group("label")))
                self.assertIn("doesn't work as a name", self.said[-1][1])              # never silently "codex"
            self.run_(self.lp.start_add("!dm", CX, "main"))
            self.assertIn("already have an agent called main", self.said[-1][1])
            self.run_(self.lp.start_add("!dm", CX, "two"))                              # GPT 24 re-check: its
            self.assertIn("**cx-two-agent**", self.said[-1][1])                          # page again, not refused
        self.assertEqual(self.made, [(CX, "two")])

    def test_from_the_console(self):
        loop = self.lp
        loop.client = SimpleNamespace(rooms={"!r": SimpleNamespace(users={CX: 0}, invited_users={})})
        loop.is_person = lambda r, u: True
        answer = self.run_(console.act(loop, "!r", CX, "add", "codex"))
        self.assertIn("Setting up codex:", answer)                    # its name; the account name comes with the code
        self.assertEqual(self.made, [(CX, "codex")])


class PresenceAndDisconnect(Base):
    people = {CX: {"agent": "remote", "dm": "!a", "more": [{"label": "codex", "agent": "remote"}]}}

    def test_each_agent_is_watched_and_the_notice_names_it(self):
        status = {f"@cx-agent:{S}": "online", f"@cx-codex-agent:{S}": "online"}

        async def presence(client, user_id):
            return status[user_id] == "online"
        p = mock.patch.object(matrix_calls, "presence", presence)
        p.start()
        self.addCleanup(p.stop)
        self.lp.client = mock.Mock()
        at = lambda t: (setattr(self.lp, "_checked", 0), asyncio.run(self.lp.check_agents(now=t)))
        at(1000)
        status[f"@cx-codex-agent:{S}"] = "offline"
        at(1001)
        at(1001 + 600)
        self.assertEqual(len(self.said), 1)
        self.assertIn("**cx-codex-agent** is offline", self.said[0][1])
        self.assertEqual((self.lp.agent_online("cx-agent"), self.lp.agent_online("cx-codex-agent")), (True, False))

    def test_the_offline_button_resends_that_agents_page(self):
        self.lp._seen = {"cx-codex-agent": {"owner": CX, "offer": "$off", "dm": "!dm"}}
        self.lp.send_code = mock.AsyncMock()
        self.assertTrue(asyncio.run(self.lp.on_offline_reaction(SimpleNamespace(room_id="!dm"), click(CX, "🔁", "$off"))))
        self.lp.send_code.assert_awaited_once_with(CX, "cx-codex-agent")

    def test_disconnect_asks_which_then_signs_out_only_that_one(self):
        with mock.patch.object(remote_agent, "disconnect") as d, mock.patch.object(remote_agent.CODES, "revoke") as rv:
            self.assertFalse(asyncio.run(self.lp.disconnect("!dm", CX)))
            self.assertIn("Which one?", self.said[-1][1])
            self.assertTrue(asyncio.run(self.lp.disconnect("!dm", CX, "codex")))
        d.assert_called_once_with(CX, "cx-codex-agent")
        rv.assert_called_once_with(CX, "cx-codex-agent")
        self.assertIn("cx-codex-agent can't sign in", self.said[-1][1])
        self.assertEqual(self.buttons[-1][2], "🔁")                                     # a way back
        m = pm_presence.DISCONNECT_RE.search("disconnect my codex agent")
        self.assertEqual(m.group("label"), "codex")


class FollowAndConsole(Base):
    people = {CX: {"agent": "remote", "more": [{"label": "codex", "agent": "remote"}]}, BETTY: {"agent": "remote"}}

    def setUp(self):
        super().setUp()
        self.invited, self.kicked = [], []

        async def invite(room_id, user):
            self.invited.append(user)
            return RoomInviteResponse()

        async def kick(room_id, user, reason=None):
            self.kicked.append(user)
        self.lp.client = SimpleNamespace(room_invite=invite, room_kick=kick, rooms={})
        self.lp.is_welcome_room = lambda r: False

    def room(self, *users):
        return SimpleNamespace(room_id="!r", users={u: 0 for u in users}, invited_users={}, name="Project", display_name="Project")

    def test_only_the_first_comes_along_by_default_each_by_its_own_setting(self):
        asyncio.run(self.lp.bring_agent(self.room(CX), CX))
        self.assertEqual(self.invited, [CXA])
        registry.set_follow(CX, "always", "cx-codex-agent")
        self.invited.clear()
        asyncio.run(self.lp.bring_agent(self.room(CX), CX))
        self.assertEqual(self.invited, [CXA, CXC])
        asyncio.run(self.lp.remove_agent(self.room(CX, CXA, CXC), CX))
        self.assertEqual(self.kicked, [CXA, CXC])

    def test_the_console_lists_every_agent_with_its_own_settings(self):
        self.lp.client.rooms = {"!r": self.room(CX, CXC)}
        self.lp.is_person = lambda r, u: u == CX
        self.lp.level = mock.AsyncMock(return_value="off")
        self.lp.pending = {}
        self.lp._seen = {"cx-codex-agent": {"online": True}}
        with mock.patch.object(room_state, "repo_of", mock.AsyncMock(return_value=None)), \
                mock.patch.object(room_state, "discussion_of", mock.AsyncMock(return_value=False)), \
                mock.patch("server.project_rooms.visibility", mock.AsyncMock(return_value="private")), \
                mock.patch.object(room_state, "is_moderator", mock.AsyncMock(return_value=False)):
            v = asyncio.run(console.view(self.lp, "!r", CX))
        mine = {a["name"]: a for a in v["agents"]}
        self.assertEqual(list(mine), ["cx-agent", "cx-codex-agent"])
        self.assertEqual((mine["cx-agent"]["follow"], mine["cx-codex-agent"]["follow"]), ("always", "never"))
        self.assertEqual((mine["cx-codex-agent"]["online"], mine["cx-codex-agent"]["here"]), (True, True))
        self.assertTrue(v["can_add"])

    def test_console_settings_reach_the_named_agent_only_if_theirs(self):
        self.lp.client.rooms = {"!r": self.room(CX, BETTY)}
        self.lp.is_person = lambda r, u: True
        asyncio.run(console.act(self.lp, "!r", CX, "follow", "ask", "cx-codex-agent"))
        self.assertEqual((registry.follow(CX), registry.follow(CX, "cx-codex-agent")), ("always", "ask"))
        self.assertIn("isn't one of your agents",
                      asyncio.run(console.act(self.lp, "!r", BETTY, "follow", "never", "cx-codex-agent")))
        self.assertEqual(registry.follow(CX, "cx-codex-agent"), "ask")                  # never someone else's
        self.assertIn("don't have that agent", self.said[-1][1])


if __name__ == "__main__":
    unittest.main()
