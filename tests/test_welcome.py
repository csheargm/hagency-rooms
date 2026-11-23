"""Unit tests: @pm welcomes a newcomer and offers them an agent. Run: .venv/bin/python -m unittest tests.test_welcome"""
# TODO(R4, 375 lines): split on its classes: Greeting / Buttons (and what follows) into test_welcome_buttons.py.
import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import matrix_calls
from server import pm_welcome
from common import registry
from server import remote_agent
from common import room_state
from server.loop import Loop

S = "bridge.localhost"
NEW, ANNA, PM = f"@newbie1:{S}", f"@anna:{S}", f"@pm:{S}"
FAR = "@newbie1:elsewhere.example"   # from another server: my chat comes as an invite they must accept
WELCOME_ROOM = SimpleNamespace(room_id="!welcome", canonical_alias=pm_welcome.WELCOME_ALIAS, users={})
DM = SimpleNamespace(room_id="!dm", canonical_alias=None, users={})


def member(who, membership="join", prev=None):
    return SimpleNamespace(state_key=who, membership=membership, prev_membership=prev, server_timestamp=10**13,
                           event_id="$m")


def text(sender, body):
    return SimpleNamespace(sender=sender, body=body, source={"content": {"body": body}})


def reaction(sender, key, to="$offer"):
    return SimpleNamespace(sender=sender, key=key, reacts_to=to, server_timestamp=10**13)


class Base(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({"anna": {"agent": "hermes"}}))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.started_ms, self.lp.busy, self.lp._welcomes = PM, 0, set(), {}
        self.lp._add_offers, self.lp._seen = {}, {}   # pm_agents.py, pm_presence.py: never the real data folder
        self.lp._lobby_greeted, self.lp._join_requests = {}, {}   # pm_lobby.py, pm_joining.py: the same
        self.said, self.buttons = [], []

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
            return "$offer" if body.startswith("Welcome") else f"$m{len(self.said)}"

        async def thumb(room_id, event_id, key="👍"):
            self.buttons.append((room_id, event_id, key))
        self.lp.say, self.lp.offer_thumb = say, thumb
        self.lp.private_room = mock.AsyncMock(return_value="!dm")
        self.lp.client = SimpleNamespace(rooms={"!dm": SimpleNamespace(users={PM: None, NEW: None})})   # it opened by itself
        self.admins = set()
        for a in (mock.patch.object(pm_welcome.server_admins, "is_admin", lambda who: who in self.admins),
                  mock.patch.object(matrix_calls, "in_directory", mock.AsyncMock(return_value=True))):   # the Lobby: listed
            a.start()
            self.addCleanup(a.stop)

    def run_(self, coro):
        return asyncio.run(coro)


class Greeting(Base):
    def test_a_newcomer_joining_welcome_gets_one_private_offer_with_one_button(self):
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.lp.private_room.assert_awaited_once_with(NEW)
        self.assertEqual(self.said[0][0], "!dm")
        self.assertIn("Get your agent", self.said[0][1])
        self.assertEqual([b[2] for b in self.buttons], ["🔌"])
        self.assertEqual(self.lp._welcomes[NEW], {"dm": "!dm", "offer": "$offer", "done": []})
        self.assertNotIn("!welcome", [r for r, _ in self.said])   # screen test 2026-11-23: no public "Welcome, @…!"

    def test_my_private_chat_is_named_product_manager_and_direct(self):
        """Seen live: it was called "Hagency (private)", which said nothing about who it was with."""
        self.lp.dm_rooms = {}
        self.lp.client = mock.Mock(rooms={}, room_create=mock.AsyncMock(return_value=SimpleNamespace(room_id="!new")))
        self.assertEqual(self.run_(Loop.private_room(self.lp, NEW)), "!new")
        kw = self.lp.client.room_create.call_args.kwargs
        self.assertEqual((kw["name"], kw["is_direct"], kw["invite"]), ("Product Manager", True, [NEW]))
        # GPT review 23: codes and console links go here: only I invite, and newcomers can't read back
        self.assertEqual(kw["power_level_override"], {"invite": 100})
        self.assertEqual(kw["initial_state"][0]["content"], {"history_visibility": "invited"})   # they read my offer

    def test_one_line_in_welcome_points_them_to_the_invite(self):
        """Seen live: Element lists my private chat as an invite, so a newcomer never saw the buttons."""
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR)))
        pointers = [b for r, b in self.said if r == "!welcome"]
        self.assertEqual(len(pointers), 1)
        self.assertIn(FAR, pointers[0])                     # the pill: Element shows their name
        self.assertIn("accept it under Invites", pointers[0])   # another server's Element: an invite
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR, prev="leave")))
        self.assertEqual(len([b for r, b in self.said if r == "!welcome"]), 1)

    def test_a_failed_pointer_is_retried_but_a_sent_one_never_repeats(self):
        """GPT review 23: a send failure (or a restart in between) must not lose the pointer for good."""
        async def failing(room_id, body, *a, **k):
            if room_id == "!welcome":
                raise RuntimeError("M_LIMIT_EXCEEDED")
            self.said.append((room_id, body))
            return "$offer"
        ok, self.lp.say = self.lp.say, failing
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR)))
        self.assertNotIn("pointed", self.lp._welcomes[FAR])
        self.lp.say = ok
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR, prev="leave")))
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR, prev="leave")))
        self.assertEqual([r for r, _ in self.said], ["!dm", "!welcome"])    # one offer, one pointer

    def test_asking_again_never_brings_the_pointer_back(self):
        """GPT review 23 re-check: "I want an agent" re-greets, which must keep "pointed"."""
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR)))
        self.lp.addressed_text = lambda event, room=None: event.body
        self.run_(self.lp.on_welcome_text(WELCOME_ROOM, text(FAR, "I want an agent")))   # answered: a second line
        self.assertTrue(self.lp._welcomes[FAR]["pointed"])
        before = len(self.said)
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR, prev="leave")))
        self.assertEqual(len(self.said), before)                 # a rejoin repeats nothing

    def test_one_of_ours_whose_old_chat_is_gone_gets_the_pointer(self):
        """GPT review: a stale record (they declined my chat, now gone) must not hide the way in."""
        self.lp._welcomes[NEW] = {"dm": "!gone", "offer": "$offer", "done": []}
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.assertEqual([r for r, _ in self.said], ["!welcome"])

    def test_no_pointer_for_someone_who_already_clicked_a_button(self):
        self.lp._welcomes[NEW] = {"dm": "!dm", "offer": "$offer", "done": ["🏠"]}
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.assertEqual(self.said, [])

    def test_an_old_chat_without_the_safeguards_is_replaced_a_safe_one_reused(self):
        """GPT review 23: a "Hagency (private)" chat lets the person invite others, who'd read codes back."""
        levels = lambda invite: SimpleNamespace(defaults=SimpleNamespace(invite=invite))
        old = SimpleNamespace(users={PM: None, NEW: None}, history_visibility="shared", power_levels=levels(0))
        safe = SimpleNamespace(users={PM: None, NEW: None}, history_visibility="invited", power_levels=levels(100))
        self.lp.dm_rooms = {NEW: "!old"}
        self.lp.client = mock.Mock(rooms={"!old": old, "!safe": safe},
                                   room_create=mock.AsyncMock(return_value=SimpleNamespace(room_id="!safe")))
        self.assertEqual(self.run_(Loop.private_room(self.lp, NEW)), "!safe")
        self.assertEqual(self.run_(Loop.private_room(self.lp, NEW)), "!safe")
        self.lp.client.room_create.assert_awaited_once()

    def test_the_pointer_mentions_them(self):
        seen = []

        async def say(room_id, body, mention=None, **k):
            seen.append((room_id, mention))
            return "$offer" if body.startswith("Welcome to") else "$x"
        self.lp.say = say
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(FAR)))
        self.assertIn(("!welcome", FAR), seen)

    def test_the_servers_admins_are_not_welcomed(self):
        """Seen live: the owner's admin account got "Welcome, owner!"."""
        self.admins.add(NEW)
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.assertEqual(self.said, [])
        self.lp.private_room.assert_not_called()

    def test_only_once(self):
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW, prev="leave")))   # left and came back
        self.assertEqual(len([r for r, _ in self.said if r == "!dm"]), 1)

    def test_agents_bots_people_with_agents_and_other_rooms_are_not_greeted(self):
        for room, who in ((WELCOME_ROOM, f"@newbie1-agent:{S}"), (WELCOME_ROOM, f"@admin-bot:{S}"),
                          (WELCOME_ROOM, ANNA), (WELCOME_ROOM, PM)):
            self.run_(self.lp.on_welcome_member(room, member(who)))
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW, prev="join")))   # a profile change
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW, membership="invite")))
        self.assertEqual(self.said, [])

    def test_welcome_room_is_created_by_pm_when_missing(self):
        alias = mock.patch.object(matrix_calls, "room_of_alias", mock.AsyncMock(return_value=None))   # a 404
        alias.start()
        self.addCleanup(alias.stop)
        self.lp.client = mock.Mock(room_create=mock.AsyncMock(return_value=SimpleNamespace(room_id="!w")))
        self.run_(self.lp.ensure_welcome_room())
        kw = self.lp.client.room_create.call_args_list[0].kwargs
        self.assertEqual((kw["alias"], kw["preset"].value), ("welcome", "private_chat"))
        self.assertEqual(kw["topic"], pm_welcome.WELCOME_TOPIC)   # its text: test_panel_home_link
        matrix_calls.room_of_alias.return_value = "!w"
        self.lp.client.room_create.reset_mock()
        self.lp.client.rooms = {}
        self.run_(self.lp.ensure_welcome_room())
        self.lp.client.room_create.assert_not_called()

    def test_people_who_joined_while_i_was_down_are_welcomed_at_start(self):
        """GPT review 21: a join during a restart or cold start is never lost."""
        welcome = SimpleNamespace(users={NEW: None, ANNA: None, PM: None, f"@x:{S}": None}, invited_users={f"@x:{S}": None})
        alias = mock.patch.object(matrix_calls, "room_of_alias", mock.AsyncMock(return_value="!w"))
        alias.start()
        self.addCleanup(alias.stop)
        self.lp.client = mock.Mock(rooms={"!w": welcome, "!dm": SimpleNamespace(users={PM: None, NEW: None})},
                                   room_put_state=mock.AsyncMock())
        self.run_(self.lp.ensure_welcome_room())
        self.lp.private_room.assert_awaited_once_with(NEW)      # not anna (has an agent), not an invitee
        self.run_(self.lp.ensure_welcome_room())
        self.assertEqual([r for r, _ in self.said], ["!dm"])   # once; no public pointer for someone of ours

    def test_a_failed_greeting_is_not_remembered(self):
        self.lp.private_room = mock.AsyncMock(return_value=None)
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.assertNotIn(NEW, self.lp._welcomes)                 # retried later

    def test_agents_never_follow_into_the_welcome_room(self):
        self.lp.client = mock.Mock(room_invite=mock.AsyncMock())
        self.run_(self.lp.bring_agent(SimpleNamespace(**{**vars(WELCOME_ROOM), "users": {ANNA: None}}), ANNA))
        self.lp.client.room_invite.assert_not_called()


class Buttons(Base):
    def setUp(self):
        super().setUp()
        self.run_(self.lp.on_welcome_member(WELCOME_ROOM, member(NEW)))
        self.said.clear()
        self.provision = mock.patch.object(remote_agent, "provision", return_value="newbie1-agent").start()
        self.addCleanup(mock.patch.stopall)
        remote_agent.CODES.codes = {}
        self.members = [PM, NEW]
        state = lambda room_id: SimpleNamespace(events=[{"type": "m.room.member", "state_key": u,
                                                          "content": {"membership": "join"}} for u in self.members])
        self.lp.client = mock.Mock(room_get_state=mock.AsyncMock(side_effect=state))

    def click(self, key, who=NEW, room=DM, to="$offer"):
        async def go():
            handled = await self.lp.on_welcome_reaction(room, reaction(who, key, to))
            await asyncio.gather(*self.lp.busy)
            return handled
        return self.run_(go())

    def test_the_offer_never_promises_the_server_agent(self):
        """Screen test: the offer listed a 🏠 House agent "with free daily credit", and typing "house
        agent" said it isn't here yet. Not offered until it exists; typing it still gets an answer."""
        self.run_(self.lp.greet(NEW))
        offer = self.said[0][1]
        self.assertNotIn("🏠", offer)
        self.assertNotIn("house agent", offer.lower())
        self.assertNotIn("server agent", offer.lower())
        self.assertNotIn("🏠", [b[2] for b in self.buttons])
        self.run_(self.lp.on_welcome_text(DM, text(NEW, "server agent please")))
        self.assertIn("isn't here yet", self.said[-1][1])

    def test_bring_my_own_wins_over_a_mention_of_a_server_agent(self):
        """GPT review: "bring my own ... on my server agent box" is a choice to bring one, not a question."""
        async def bring():
            await self.lp.on_welcome_text(DM, text(NEW, "bring my own using ollama on my server agent box"))
            await asyncio.gather(*self.lp.busy)
        self.run_(bring())
        self.assertFalse(any("isn't here yet" in s[1] for s in self.said))

    def test_an_old_offers_house_button_still_gets_the_answer(self):
        self.assertTrue(self.click("🏠"))
        self.assertIn("server agent", self.said[0][1])
        self.assertNotIn("house", self.said[0][1].lower())   # its new name only
        self.assertIn("bring my own", self.said[0][1])

    def test_bring_your_own_creates_the_agent_and_sends_one_line_with_a_code(self):
        self.assertTrue(self.click("🔌"))
        self.provision.assert_called_once_with(NEW, None)
        room_id, body = self.said[0]
        self.assertEqual(room_id, "!dm")
        self.assertRegex(body, r"curl -fsSL http://localhost:8650/connect\.sh \| sh -s -- [A-Z2-9]{5}-[A-Z2-9]{5}")
        self.assertEqual(len(remote_agent.CODES.codes), 1)

    def test_bring_your_own_links_the_connect_page_with_the_same_code(self):
        """The Connect page (any OS) first; the code in its fragment, as text, and in the Mac/Linux line."""
        self.click("🔌")
        body = self.said[0][1]
        code = re.search(r"\(http://localhost:8650/connect/start#([A-Z2-9]{5}-[A-Z2-9]{5})\)", body).group(1)
        self.assertIn(f"Your code: `{code}`", body)
        self.assertIn(f"sh -s -- {code}`", body)
        self.assertLess(body.index("/connect/start#"), body.index("curl -fsSL"))

    def test_no_second_agent_offer_before_the_first_one_connected(self):
        """Screen test: a newcomer's very first Connect message ended with "Want another agent too? Click
        ➕ below." Only once one of their agents has connected; then "resend me the code" offers ➕."""
        self.click("🔌")
        self.assertNotIn("Want another agent", self.said[-1][1])
        self.assertNotIn("➕", [b[2] for b in self.buttons])
        registry.update(NEW, None, agent=registry.REMOTE, ready=True)           # what provision writes
        self.lp._seen[registry.agent_of(NEW)] = {"online": False, "ever": True}   # it connected once

        async def resend():
            await self.lp.on_welcome_text(DM, text(NEW, "resend me the code"))
            await asyncio.gather(*self.lp.busy)
        self.run_(resend())
        self.assertIn("Want another agent", self.said[-1][1])
        self.assertEqual(self.buttons[-1][2], "➕")

    def test_each_button_counts_once(self):
        self.click("🔌")
        self.click("🔌")
        self.assertEqual(self.provision.call_count, 1)

    def test_only_that_person_on_that_message_in_that_chat(self):
        self.assertFalse(self.click("🔌", who=ANNA))
        self.assertFalse(self.click("🔌", room=WELCOME_ROOM))
        self.assertFalse(self.click("🔌", to="$other"))
        self.assertTrue(self.click("👍"))             # my message, but not a button: nothing happens
        self.assertEqual(self.said, [])

    def test_typing_works_too(self):
        self.run_(self.lp.on_welcome_text(DM, text(NEW, "house agent please")))   # its old name still works
        self.assertIn("server agent", self.said[-1][1])

        async def bring():
            self.assertTrue(await self.lp.on_welcome_text(DM, text(NEW, "I'll bring my own")))
            await asyncio.gather(*self.lp.busy)
        self.run_(bring())
        self.provision.assert_called_once_with(NEW, None)

    def test_one_set_up_at_a_time_per_person(self):
        async def twice():
            await self.lp.on_welcome_text(DM, text(NEW, "bring my own"))
            await self.lp.on_welcome_text(DM, text(NEW, "bring my own"))
            await asyncio.gather(*self.lp.busy)
        self.run_(twice())
        self.assertEqual(self.provision.call_count, 1)

    def test_no_is_not_a_choice(self):
        self.assertFalse(self.run_(self.lp.on_welcome_text(DM, text(NEW, "I don't want to bring my own"))))
        self.provision.assert_not_called()

    def test_the_code_goes_to_a_chat_that_is_still_private(self):
        self.lp.private_room = mock.AsyncMock(return_value="!fresh")   # the old one gained a member
        self.click("🔌")
        self.assertEqual(self.said[0][0], "!fresh")

    def test_no_code_if_someone_else_is_in_the_chat_right_now(self):
        """GPT review 21 re-check: asked of the server at send time, not my cached view."""
        self.members.append(f"@mallory:{S}")
        self.click("🔌")
        self.assertNotIn("connect.sh", self.said[0][1])
        self.assertIn("couldn't set up your agent", self.said[0][1])
        self.assertEqual(remote_agent.CODES.codes, {})

    def test_a_failure_is_said_plainly(self):
        self.provision.side_effect = remote_agent.AccountError("couldn't create @newbie1-agent: M_FORBIDDEN")
        self.click("🔌")
        self.assertIn("couldn't set up your agent", self.said[0][1])


class Asking(Base):
    def test_i_want_an_agent_anywhere_opens_the_offer_privately(self):
        room = SimpleNamespace(room_id="!project", canonical_alias=None)
        self.assertTrue(self.run_(self.lp.on_welcome_text(room, text(NEW, "@pm I want an agent"))))
        self.assertIn("Get your agent", self.said[0][1])
        self.assertEqual(self.said[1][0], "!project")
        self.assertIn("open on the left", self.said[1][1])

    def test_someone_with_an_agent_is_told(self):
        room = SimpleNamespace(room_id="!project", canonical_alias=None)
        self.run_(self.lp.on_welcome_text(room, text(ANNA, "@pm can I get an agent?")))
        self.assertEqual(len(self.said), 1)
        self.assertTrue(self.said[0][1].startswith("You already have an agent here: anna-agent."))
        self.assertEqual(self.buttons, [("!project", "$m1", "➕")])          # step 4: add another one

    def test_a_same_named_person_from_another_server_gets_their_own_agent_never_ours(self):
        """GPT review 21: @newbie1:evil.org must never get a code for our newbie1's agent. Since
        review 23 they may get an agent of their own, named from their full ID."""
        registry.PATH.write_text(json.dumps({"newbie1": {"agent": "remote"}}))
        room = SimpleNamespace(room_id="!project", canonical_alias=None)
        evil = "@newbie1:evil.org"
        self.run_(self.lp.on_welcome_text(room, text(evil, "@pm I want an agent")))
        self.lp.private_room.assert_awaited_once_with(evil)
        self.assertNotIn("newbie1-agent", " ".join(b for _, b in self.said))
        provision = mock.patch.object(remote_agent, "provision", return_value="x").start()
        self.addCleanup(mock.patch.stopall)
        self.lp.client = mock.Mock(room_get_state=mock.AsyncMock(return_value=SimpleNamespace(events=[])))

        async def bring():
            await self.lp.choose("!dm", evil, "🔌")
            await asyncio.gather(*self.lp.busy)
        self.run_(bring())
        provision.assert_called_once_with(evil, None)                # the full ID: never our newbie1
        self.assertNotIn("newbie1", [v["person"] for v in remote_agent.CODES.codes.values()])

    def test_project_requests_that_mention_an_agent_go_on_to_the_usual_chat(self):
        room = SimpleNamespace(room_id="!project", canonical_alias=None)
        for said in ("@pm I need an agent to review #7", "@pm can I get an agent's opinion on the plan?"):
            self.assertFalse(self.run_(self.lp.on_welcome_text(room, text(ANNA, said))), said)
        self.assertTrue(self.run_(self.lp.on_welcome_text(room, text(NEW, "@pm please, I'd like an agent!"))))

    def test_other_messages_go_on_to_the_usual_chat(self):
        room = SimpleNamespace(room_id="!project", canonical_alias=None)
        self.assertFalse(self.run_(self.lp.on_welcome_text(room, text(NEW, "@pm where are we?"))))
        self.assertFalse(self.run_(self.lp.on_welcome_text(room, text(NEW, "bring my own"))))   # not to me, not our chat


class TickWhileJoining(unittest.TestCase):
    def test_a_room_joined_during_a_tick_doesnt_crash_it(self):
        """Regression: welcoming someone opens a DM mid-tick (dictionary changed size during iteration)."""
        lp = Loop.__new__(Loop)
        lp.client = SimpleNamespace(rooms={"!a": None})
        lp.room_repos, lp.open_issues, lp.state = {}, {}, {}

        async def repo_of(client, room_id):
            client.rooms["!new"] = None
            return None
        with mock.patch.object(room_state, "repo_of", repo_of):
            asyncio.run(lp.tick())


if __name__ == "__main__":
    unittest.main()
