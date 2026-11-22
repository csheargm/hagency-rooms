"""Unit tests: asking to join a project room, the Lobby's one-line greeting, and a chat someone opens
with @pm themselves (pm_joining.py, pm_lobby.py, pm_follow.py). Run: .venv/bin/python -m unittest tests.test_joining"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomInviteError, RoomInviteResponse, RoomKickResponse

from common import matrix_calls
from server import pm_joining
from server import pm_lobby
from server import pm_welcome
from server import project_rooms
from tests.test_projects import ANNA, BETTY, PM, REPO, S, Base, project

NEW = f"@newbie1:{S}"


def run(coro):
    return asyncio.run(coro)


def click(sender, key, to):
    return SimpleNamespace(sender=sender, key=key, reacts_to=to, server_timestamp=10**13)


def member(who, membership, prev=None):
    return SimpleNamespace(state_key=who, membership=membership, prev_membership=prev, server_timestamp=10**13, event_id="$m")


class AskToJoin(Base):
    def setUp(self):
        super().setUp()
        self.room = project("!a", "Alpha", mods=(ANNA, BETTY))
        self.lp.client.rooms = {"!a": self.room}
        self.lp.client.room_invite = mock.AsyncMock(return_value=RoomInviteResponse())
        self.lp.client.room_kick = mock.AsyncMock(return_value=RoomKickResponse())
        self.members.update({("!a", ANNA): "join", ("!a", BETTY): "join"})
        self.mods["!a"] = {ANNA, BETTY}

    def asks(self):
        return [(r, b) for r, b in self.said if "asks to join" in b]

    def test_the_person_is_named_once(self):
        """Screen test: "finaltest2 (<pill: finaltest2>) asks to join…": Element turns the full ID into a
        pill that shows the name, so the plain name before it said it twice."""
        run(self.lp.ask_to_join("!a", NEW))
        body = self.asks()[0][1]
        self.assertTrue(body.startswith(f"{NEW} asks to join "), body)
        self.assertEqual(body.count(NEW.split(":")[0].lstrip("@")), 1)

    def test_every_moderator_is_asked_with_two_buttons_and_only_theirs_counts(self):
        self.assertTrue(run(self.lp.ask_to_join("!a", NEW)))
        self.assertEqual([r for r, _ in self.asks()], [f"!dm-{ANNA}", f"!dm-{BETTY}"])
        self.assertIn(f"{NEW} asks to join **Alpha**", self.asks()[0][1])     # the full ID, never just a name
        self.assertEqual([k for _, _, k in self.buttons], ["👍", "👎", "👍", "👎"])
        anna_q = next(e for e, r in self.lp._join_requests.items() if r["moderator"] == ANNA)
        dm = SimpleNamespace(room_id=f"!dm-{ANNA}")
        self.assertTrue(run(self.lp.on_join_reaction(dm, click(NEW, "👍", anna_q))))      # the asker can't let themself in
        self.lp.client.room_invite.assert_not_called()
        self.assertTrue(run(self.lp.on_join_reaction(dm, click(ANNA, "👍", anna_q))))
        self.lp.client.room_invite.assert_awaited_once_with("!a", NEW)
        self.assertEqual(self.lp._join_requests, {})                                       # settled for every moderator

    def test_a_moderator_who_lost_the_role_cant_let_anyone_in(self):
        run(self.lp.ask_to_join("!a", NEW))
        q = next(e for e, r in self.lp._join_requests.items() if r["moderator"] == BETTY)
        self.mods["!a"] = {ANNA}                  # demoted on the server; my cached room still says 100
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=f"!dm-{BETTY}"), click(BETTY, "👍", q)))
        self.lp.client.room_invite.assert_not_called()
        self.assertIn("no longer an owner", self.said[-1][1])

    def test_a_declined_knock_is_turned_down(self):
        self.members[("!a", NEW)] = "knock"
        run(self.lp.on_welcome_member(self.room, member(NEW, "knock", "leave")))          # Element's "Ask to join"
        q, req = next(iter(self.lp._join_requests.items()))
        self.assertEqual(req["knock"], "$m")
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=req["dm"]), click(req["moderator"], "👎", q)))
        self.lp.client.room_kick.assert_awaited_once()
        self.lp.client.room_invite.assert_not_called()

    def test_a_withdrawn_knock_or_someone_already_in_is_never_acted_on(self):
        """GPT review: a stale 👍 must not invite, a stale 👎 must not kick."""
        self.members[("!a", NEW)], self.member_ids[("!a", NEW)] = "knock", "$k"
        run(self.lp.on_knock(self.room, NEW, "$k"))
        q, req = next(iter(self.lp._join_requests.items()))
        self.member_ids[("!a", NEW)] = "$k2"                                              # withdrawn, knocked again
        self.assertTrue(run(self.lp.ask_to_join("!a", NEW, knock="$k2")))                  # new buttons for the new knock
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=req["dm"]), click(req["moderator"], "👎", q)))
        self.lp.client.room_kick.assert_not_called()
        self.assertIn("no longer stands", self.said[-1][1])
        knocks = [r["knock"] for r in self.lp._join_requests.values()]
        self.assertEqual(sorted(knocks), ["$k2", "$k2"])            # GPT re-check: the old knock settles, the renewed one stays
        self.assertNotIn(q, self.lp._join_requests)
        run(self.lp.ask_to_join("!b", NEW))                                                # (no such room: nothing)
        self.members[("!a", f"@carl:{S}")] = "join"                                         # already in
        self.lp._ask_limits = {"pairs": {}, "people": {}, "all": []}
        self.lp.client.rooms["!a"].users.pop(f"@carl:{S}", None)
        run(self.lp.ask_to_join("!a", f"@carl:{S}"))
        q = next(e for e, r in self.lp._join_requests.items() if r["person"] == f"@carl:{S}")
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=f"!dm-{ANNA}"), click(ANNA, "👍", q)))
        self.lp.client.room_invite.assert_not_called()

    def test_an_unreadable_room_is_never_decided_on_a_guess(self):
        run(self.lp.ask_to_join("!a", NEW))
        q = next(e for e, r in self.lp._join_requests.items() if r["moderator"] == ANNA)
        self.lp.client.room_get_state = mock.AsyncMock(return_value=SimpleNamespace())   # an error response
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=f"!dm-{ANNA}"), click(ANNA, "👍", q)))
        self.lp.client.room_invite.assert_not_called()
        self.assertIn("couldn't check that room", self.said[-1][1])
        self.assertEqual(len(self.lp._join_requests), 2)

    def test_a_moderator_who_left_the_room_cant_decide(self):
        run(self.lp.ask_to_join("!a", NEW))
        q = next(e for e, r in self.lp._join_requests.items() if r["moderator"] == ANNA)
        self.members[("!a", ANNA)] = "leave"                                              # power kept, membership gone
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=f"!dm-{ANNA}"), click(ANNA, "👍", q)))
        self.lp.client.room_invite.assert_not_called()

    def test_a_failed_invite_leaves_the_request_open(self):
        run(self.lp.ask_to_join("!a", NEW))
        self.lp.client.room_invite = mock.AsyncMock(return_value=RoomInviteError("busy"))
        q = next(e for e, r in self.lp._join_requests.items() if r["moderator"] == ANNA)
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=f"!dm-{ANNA}"), click(ANNA, "👍", q)))
        self.assertIn("didn't work", self.said[-1][1])
        self.assertEqual(len(self.lp._join_requests), 2)                                    # another click can still do it

    def test_asking_is_rate_limited(self):
        self.assertTrue(run(self.lp.ask_to_join("!a", NEW)))
        self.lp._join_requests.clear()                                                     # settled: still a day apart
        self.assertFalse(run(self.lp.ask_to_join("!a", NEW)))                              # same room: once a day
        rooms = {f"!r{i}": project(f"!r{i}", f"R{i}") for i in range(pm_joining.ASKS_PER_HOUR + 1)}
        self.lp.client.rooms.update(rooms)
        self.assertTrue(run(self.lp.ask_to_join("!a", NEW, knock="$renewed")))           # a renewed knock: asked again
        results = [run(self.lp.ask_to_join(rid, f"@carl:{S}")) for rid in rooms]
        self.assertEqual(results, [True] * pm_joining.ASKS_PER_HOUR + [False])         # a few an hour, all rooms

    def test_members_are_never_asked_about(self):
        self.assertFalse(run(self.lp.ask_to_join("!a", BETTY)))
        self.assertEqual(self.said, [])

    def test_asking_by_repo_name_says_the_same_whether_or_not_a_room_exists(self):
        self.lp.addressed_text = lambda event, room=None: event.body
        lobby = project("!l", "Lobby")
        self.bind("!a")
        async def both():
            await self.lp.on_project_text(lobby, SimpleNamespace(sender=NEW, body=f"ask to join {REPO}"))
            self.assertEqual(self.said, [("!l", pm_joining.ASKED)])                        # answered before any lookup
            await self.lp.on_project_text(lobby, SimpleNamespace(sender=NEW, body="ask to join someone/elses-private"))
            await asyncio.gather(*self.lp.busy)
        run(both())
        replies = [b for r, b in self.said if r == "!l"]
        self.assertEqual(replies, [pm_joining.ASKED, pm_joining.ASKED])
        self.assertEqual(len(self.asks()), 2)                                              # anna and betty, for Alpha only


# TODO(R4): over 300 lines; split on its classes (AskToJoin / LobbyGreeting / private chats) into their own files.
class LobbyGreeting(Base):
    def setUp(self):
        super().setUp()
        self.lp.offer_again = mock.AsyncMock()
        a = mock.patch.object(pm_lobby.server_admins, "is_admin", lambda who: False)
        a.start()
        self.addCleanup(a.stop)
        self.lobby = project("!l", "Lobby")
        self.lobby.canonical_alias = project_rooms.LOBBY_ALIAS

    def test_one_line_once_ever_with_a_button_that_answers_anyone(self):   # others: tests/test_lobby_clicks.py
        run(self.lp.on_welcome_member(self.lobby, member(NEW, "join", "leave")))
        run(self.lp.on_welcome_member(self.lobby, member(NEW, "join", "leave")))
        self.assertEqual(len(self.said), 1)
        self.assertIn(NEW, self.said[0][1])
        self.assertIn("Click 👍 below to get your agent", self.said[0][1])
        self.assertEqual(self.buttons, [("!l", "$e1", "👍")])
        self.assertTrue(run(self.lp.on_lobby_reaction(self.lobby, click(NEW, "👍", "$e1"))))
        run(self.lp.on_lobby_reaction(self.lobby, click(NEW, "👍", "$e1")))
        self.lp.offer_again.assert_awaited_once_with(self.lobby, NEW)                       # once

    def test_a_failed_offer_doesnt_use_up_the_button(self):
        run(self.lp.on_welcome_member(self.lobby, member(NEW, "join", "leave")))
        self.lp.offer_again = mock.AsyncMock(side_effect=[RuntimeError("M_LIMIT_EXCEEDED"), False, True])
        for _ in range(3):                    # a send that failed, a greeting that couldn't go out, then one that did
            run(self.lp.on_lobby_reaction(self.lobby, click(NEW, "👍", "$e1")))
        self.assertEqual(self.lp.offer_again.await_count, 3)
        self.assertTrue(self.lp._lobby_greeted[NEW]["done"])

    def test_people_with_agents_agents_and_my_private_chats_get_nothing(self):
        dm = project("!dm", "Product Manager")
        dm.users = {PM: None, NEW: None}
        self.lp.dm_rooms = {NEW: "!dm"}
        for room, who in ((self.lobby, ANNA), (self.lobby, f"@newbie1-agent:{S}"), (self.lobby, f"@x-bot:{S}"), (dm, NEW)):
            run(self.lp.on_welcome_member(room, member(who, "join", "invite")))
        self.assertEqual(self.said, [])

    def test_the_lobby_is_created_public_and_listed_and_welcome_points_at_my_chat(self):
        lobby = mock.patch.object(matrix_calls, "room_of_alias", mock.AsyncMock(return_value=None))   # a 404
        lobby.start()
        self.addCleanup(lobby.stop)
        self.lp.client.room_create = mock.AsyncMock(return_value=SimpleNamespace(room_id="!l"))
        run(self.lp.ensure_lobby())
        kw = self.lp.client.room_create.call_args.kwargs
        self.assertEqual((kw["alias"], kw["preset"].value, kw["visibility"].value), ("lobby", "public_chat", "public"))
        matrix_calls.room_of_alias.return_value = "!l"
        self.lp.client.room_create.reset_mock()
        with mock.patch.object(matrix_calls, "in_directory", mock.AsyncMock(return_value=True)):
            run(self.lp.ensure_lobby())
        self.lp.client.room_create.assert_not_called()
        matrix_calls.room_of_alias.side_effect = RuntimeError("HTTP 500")   # unknown: never a second Lobby
        with self.assertLogs("loop", "WARNING"):
            run(self.lp.ensure_lobby())
        self.lp.client.room_create.assert_not_called()
        self.lp.client.rooms = {"!w": SimpleNamespace(topic="Everyone starts here.", name=None)}
        run(self.lp.ensure_welcome_topic("!w"))
        self.assertEqual(self.state["!w"]["m.room.topic"][0], {"topic": pm_lobby.WELCOME_TOPIC})
        self.assertEqual(self.state["!w"]["m.room.name"][0], {"name": "Welcome"})   # not its raw #welcome:… address

    def test_an_existing_lobby_missing_from_the_directory_is_listed_at_start(self):
        """Screen test: a Lobby made while Synapse still refused publishing wasn't in Explore rooms."""
        for p in (mock.patch.object(matrix_calls, "room_of_alias", mock.AsyncMock(return_value="!l")),
                  mock.patch.object(matrix_calls, "in_directory", mock.AsyncMock(return_value=False))):
            p.start()
            self.addCleanup(p.stop)
        self.lp.client.room_create = mock.AsyncMock()
        self.state["!l"] = {"m.room.create": ({}, PM), "m.room.join_rules": ({"join_rule": "public"}, PM)}
        run(self.lp.ensure_lobby())
        self.lp.client.room_create.assert_not_called()                                     # never a second Lobby
        method, path, body = self.lp.client.send.call_args.args[:3]
        self.assertEqual((method, path, json.loads(body)),
                         ("PUT", "/_matrix/client/v3/directory/list/room/%21l", {"visibility": "public"}))
        self.lp.client.send.reset_mock()
        matrix_calls.in_directory.return_value = True                                      # listed: nothing to do
        run(self.lp.ensure_lobby())
        self.lp.client.send.assert_not_called()
        matrix_calls.in_directory.return_value = False
        self.lp.client.send.return_value = SimpleNamespace(status=403, release=lambda: None)
        with self.assertLogs("loop", "WARNING") as logs:                                   # refused: said, retried next start
            run(self.lp.ensure_lobby())
        self.assertIn("refused to list", logs.output[0])

    def test_a_lobby_address_on_someone_elses_or_a_closed_room_is_never_listed(self):
        """GPT review: #lobby resolving is no proof it is my public Lobby."""
        for p in (mock.patch.object(matrix_calls, "room_of_alias", mock.AsyncMock(return_value="!l")),
                  mock.patch.object(matrix_calls, "in_directory", mock.AsyncMock(return_value=False))):
            p.start()
            self.addCleanup(p.stop)
        for create_by, rule in ((ANNA, "public"), (PM, "invite")):
            self.state["!l"] = {"m.room.create": ({}, create_by), "m.room.join_rules": ({"join_rule": rule}, PM)}
            with self.assertLogs("loop", "WARNING") as logs:
                run(self.lp.ensure_lobby())
            self.assertIn("isn't a public room I made", logs.output[0])
        self.lp.client.send.assert_not_called()

    def test_a_named_welcome_room_is_left_alone(self):
        self.lp.client.rooms = {"!w": SimpleNamespace(topic=pm_lobby.WELCOME_TOPIC, name="Welcome")}
        run(self.lp.ensure_welcome_topic("!w"))
        self.assertNotIn("!w", self.state)

    def test_the_pointer_names_the_next_step_for_each_server(self):
        self.assertIn("open on the left as **Product Manager**", pm_welcome.pointer(NEW))
        self.assertIn("accept it under Invites", pm_welcome.pointer("@newbie1:matrix.org"))
        for text in (pm_welcome.pointer(NEW), pm_lobby.GREET, pm_welcome.WELCOME):
            self.assertRegex(text, r"[Cc]lick")                                            # every message: the next step


class TheirOwnChatWithMe(Base):
    def test_a_newcomer_opening_a_chat_gets_the_offer_not_a_moderator_request(self):
        self.lp.client.join = mock.AsyncMock()
        self.lp.client.room_get_state_event = mock.AsyncMock()
        self.lp.only_us = mock.AsyncMock(return_value=True)
        self.lp.offer_again = mock.AsyncMock()
        room = project("!theirs", "")
        invite = SimpleNamespace(sender=NEW, state_key=PM, membership="invite", content={"is_direct": True})
        run(self.lp.on_invite(room, invite))
        self.lp.offer_again.assert_awaited_once_with(room, NEW)
        self.lp.client.room_get_state_event.assert_not_called()                             # no "make me a moderator"
        invite.sender = ANNA                                                                # has an agent
        run(self.lp.on_invite(room, invite))
        self.assertIn("@pm help", self.said[-1][1])


class MyPrivateChat(Base):
    """Seen live: in @pm's own chat, "resend me the code" (and "send me my console link") without
    "@pm" got no answer. Every message in a one-to-one chat with @pm is for @pm."""
    def setUp(self):
        super().setUp()
        self.dm = project("!dm", "Product Manager")
        self.dm.users = {PM: None, NEW: None}
        self.lp.dm_rooms = {NEW: "!dm"}
        self.lp.choose = mock.AsyncMock()
        self.lp.private_room = mock.AsyncMock(return_value="!dm")   # my chat with them is this one
        self.lp._welcomes, self.lp._add_offers = {}, {}

    def say_in_dm(self, body, who=NEW, room=None):
        event = SimpleNamespace(sender=who, body=body, source={"content": {"body": body}})
        return run(self.lp.on_welcome_text(room or self.dm, event))

    def test_resend_me_the_code_without_mentioning_pm(self):
        for words in ("resend me the code", "new code", "connect my agent", "can you send the connect page again?"):
            self.assertTrue(self.say_in_dm(words), words)
        self.assertEqual(self.lp.choose.await_count, 4)              # the same Connect message 🔌 sends
        self.lp.choose.assert_awaited_with("!dm", NEW, "🔌")
        self.assertEqual(self.said, [])                               # in this chat: no pointer to itself

    def test_codes_are_a_few_an_hour(self):
        for _ in range(pm_welcome.CODES_PER_HOUR + 1):
            self.say_in_dm("resend me the code")
        self.assertEqual(self.lp.choose.await_count, pm_welcome.CODES_PER_HOUR)
        self.assertIn("newest one still works", self.said[-1][1])

    def test_an_agent_on_this_server_has_no_code(self):
        self.dm.users = {PM: None, ANNA: None}
        self.lp.dm_rooms = {ANNA: "!dm"}
        self.say_in_dm("resend me the code", ANNA)
        self.lp.choose.assert_not_called()
        self.assertIn("no code", self.said[-1][1])

    def test_console_link_without_mentioning_pm(self):
        event = SimpleNamespace(sender=NEW, body="send me my console link", source={"content": {"body": "send me my console link"}})
        self.assertEqual(self.lp.addressed_text(event, self.dm), "send me my console link")
        self.assertEqual(self.lp.addressed_text(event, self.dm.__class__(room_id="!x", users={PM: None, NEW: None, ANNA: None},
                                                                          invited_users={})), None)   # three people: say @pm

    def test_a_room_named_like_my_chat_is_not_my_chat(self):
        """GPT review: a room's name is anyone's choice; only my own records count."""
        room = project("!p", "Product Manager")
        room.users = {PM: None, NEW: None}
        event = SimpleNamespace(sender=NEW, body="stop", source={"content": {"body": "stop"}})
        self.assertIsNone(self.lp.addressed_text(event, room))
        self.lp._direct_chats["!p"] = ANNA                                                 # recorded for someone else
        self.assertIsNone(self.lp.addressed_text(event, room))

    def test_a_project_room_with_a_repo_is_not_a_private_chat(self):
        room = project("!p", "Proj")
        room.users = {PM: None, NEW: None}
        self.lp.room_repos["!p"] = REPO
        event = SimpleNamespace(sender=NEW, body="stop", source={"content": {"body": "stop"}})
        self.assertIsNone(self.lp.addressed_text(event, room))                             # thinking aloud stays theirs

    def test_a_fresh_project_room_with_two_people_is_not_a_private_chat(self):
        """GPT review: plain "stop" in a new project room must not act."""
        room = project("!p", "Proj")
        room.users = {PM: None, NEW: None}                                                  # no repo yet, nobody else
        event = SimpleNamespace(sender=NEW, body="stop", source={"content": {"body": "stop"}})
        self.assertIsNone(self.lp.addressed_text(event, room))

    def test_a_chat_they_opened_with_me_counts_but_not_once_someone_else_is_invited(self):
        room = project("!theirs", "")
        room.users = {PM: None, NEW: None}
        self.lp._direct_chats["!theirs"] = NEW
        event = SimpleNamespace(sender=NEW, body="new code", source={"content": {"body": "new code"}})
        self.assertEqual(self.lp.addressed_text(event, room), "new code")
        room.users[ANNA] = None                                                             # invited: no longer just us
        self.assertIsNone(self.lp.addressed_text(event, room))


if __name__ == "__main__":
    unittest.main()
