"""Unit tests: 👍 under @pm's Lobby greeting for someone else (pm_lobby.on_others_greeting). Seen live:
the owner, who has agents, clicked 👍 under a newcomer's greeting and got no answer at all.
Run: .venv/bin/python -m unittest discover -p 'test_*.py' (alone, this module hits an import cycle)."""
import asyncio
from types import SimpleNamespace
from unittest import mock

from aiohttp import ClientError

from common import registry
from server import pm_lobby
from server import project_rooms
from tests.test_projects import ANNA, BETTY, PM, S, Base, project

NEW = f"@newbie1:{S}"


def run(coro):
    return asyncio.run(coro)


def click(sender, key, to):
    return SimpleNamespace(sender=sender, key=key, reacts_to=to, server_timestamp=10**13)


class OthersGreeting(Base):
    def setUp(self):
        super().setUp()
        self.lp.offer_again = mock.AsyncMock(return_value=True)
        self.lobby = project("!l", "Lobby")
        self.lobby.canonical_alias = project_rooms.LOBBY_ALIAS
        self.lp.has_connected_agent = lambda who: True
        self.lp._lobby_greeted = {NEW: {"room": "!l", "event": "$greet"}}   # my greeting for newbie1

    def react(self, who, key="👍", to="$greet", room=None):
        return run(self.lp.on_welcome_reaction(room or self.lobby, click(who, key, to)))

    def test_someone_with_an_agent_is_told_privately_that_they_are_set_up(self):
        """The bug: anna (agent hermes) clicks 👍 under newbie1's greeting and hears nothing."""
        self.assertTrue(self.react(ANNA))
        self.assertEqual(len(self.said), 1)
        where, body = self.said[0]
        self.assertEqual(where, f"!dm-{ANNA}")                             # privately, not in the Lobby
        self.assertIn("already set up", body)
        self.assertIn("**anna-agent**", body)                              # their agent, by name
        self.assertIn('"@pm send me my console link"', body)
        for word in ("bridge", "adapter", "next_task"):
            self.assertNotIn(word, body)
        self.lp.offer_again.assert_not_awaited()
        self.assertNotIn("done", self.lp._lobby_greeted[NEW])              # newbie1's own button still works

    def test_repeat_clicks_are_answered_once_per_greeting(self):
        for _ in range(3):
            self.assertTrue(self.react(ANNA))
        self.assertEqual(len(self.said), 1)
        self.lp._lobby_greeted["@other:" + S] = {"room": "!l", "event": "$greet2"}
        self.react(ANNA, to="$greet2")                                     # another person's greeting: once more
        self.assertEqual(len(self.said), 2)

    def test_no_private_chat_answers_in_the_lobby_without_their_agents_names(self):   # GPT review
        self.lp.private_room = mock.AsyncMock(return_value=None)
        self.lp.say = mock.AsyncMock(return_value="$x")
        self.react(ANNA)
        self.lp.say.assert_awaited_once()
        where, body = self.lp.say.await_args.args
        self.assertEqual(where, "!l")
        self.assertEqual(self.lp.say.await_args.kwargs["mention"], ANNA)
        self.assertNotIn("anna-agent", body)
        self.assertIn("already set up here. That 👍 is for newcomers", body)

    def test_a_failed_send_lets_another_click_retry(self):
        self.lp.say = mock.AsyncMock(side_effect=[RuntimeError("M_LIMIT_EXCEEDED"), "$x"])
        self.react(ANNA)
        self.react(ANNA)
        self.assertEqual(self.lp.say.await_count, 2)

    def test_a_transport_error_doesnt_use_up_the_click(self):   # GPT review
        self.lp.private_room = mock.AsyncMock(side_effect=[ClientError("down"), f"!dm-{ANNA}"])
        self.assertTrue(self.react(ANNA))     # caught here: it never ends my sync loop (GPT review)
        self.react(ANNA)
        self.assertEqual([w for w, _ in self.said], [f"!dm-{ANNA}"])

    def test_a_bring_your_own_agent_that_never_connected_gets_the_offer_again(self):   # GPT review
        registry.PATH.write_text('{"anna": {"agent": "%s"}}' % registry.REMOTE)
        self.lp.has_connected_agent = lambda who: False
        self.react(ANNA)
        self.lp.offer_again.assert_awaited_once_with(self.lobby, ANNA)
        self.assertEqual(self.said, [])

    def test_a_newcomer_clicking_someone_elses_greeting_gets_the_offer(self):
        self.assertTrue(self.react(BETTY))                                 # betty: no agent, never greeted
        self.lp.offer_again.assert_awaited_once_with(self.lobby, BETTY)
        self.react(BETTY)
        self.lp.offer_again.assert_awaited_once()                          # not twice for one greeting

    def test_a_newcomer_with_their_own_greeting_uses_it_up(self):
        self.lp._lobby_greeted[BETTY] = {"room": "!l", "event": "$betty"}
        self.react(BETTY)                                                  # on newbie1's greeting
        self.assertTrue(self.lp._lobby_greeted[BETTY]["done"])
        self.react(BETTY, to="$betty")                                     # then their own: once ever
        self.lp.offer_again.assert_awaited_once_with(self.lobby, BETTY)

    def test_a_newcomer_whose_offer_went_out_is_pointed_to_it(self):   # GPT review: never silence
        self.lp._lobby_greeted[BETTY] = {"room": "!l", "event": "$betty", "done": True}
        self.assertTrue(self.react(BETTY))
        self.lp.offer_again.assert_not_awaited()                           # still one offer, ever
        self.assertEqual(len(self.said), 1)
        self.assertEqual(self.said[0][0], "!l")
        self.assertIn("I've sent you a private message", self.said[0][1])
        self.react(BETTY)
        self.assertEqual(len(self.said), 1)                                # once per greeting

    def test_a_newcomers_offer_that_failed_can_be_clicked_again(self):
        self.lp.offer_again = mock.AsyncMock(side_effect=[RuntimeError("M_LIMIT_EXCEEDED"), True])
        self.react(BETTY)
        self.react(BETTY)
        self.assertEqual(self.lp.offer_again.await_count, 2)

    def test_the_greeted_persons_own_click_is_unchanged(self):
        self.assertTrue(self.react(NEW))
        self.lp.offer_again.assert_awaited_once_with(self.lobby, NEW)
        self.assertTrue(self.lp._lobby_greeted[NEW]["done"])
        self.assertEqual(self.said, [])

    def test_other_emoji_and_other_messages_are_left_alone(self):
        self.lp.on_reaction = mock.AsyncMock()
        self.lp.on_project_reaction = mock.AsyncMock(return_value=False)
        self.assertFalse(run(self.lp.on_lobby_reaction(self.lobby, click(ANNA, "🎉", "$greet"))))
        self.assertFalse(run(self.lp.on_lobby_reaction(self.lobby, click(ANNA, "👍", "$someone-elses-message"))))
        self.assertFalse(run(self.lp.on_lobby_reaction(project("!x", "Elsewhere"), click(ANNA, "👍", "$greet"))))
        self.assertEqual(self.said, [])
        self.lp.offer_again.assert_not_awaited()

    def test_several_agents_are_all_named(self):
        self.assertEqual(pm_lobby.agents_in_words([{"localpart": "a-agent", "label": "main", "display": "Claude"},
                                                   {"localpart": "a-codex", "label": "codex"}]),
                         ": your agents **Claude**, **a-codex**")
        self.assertEqual(pm_lobby.agents_in_words([{"localpart": "a-agent", "label": "main"}]), ": your agent **a-agent**")
        self.assertNotIn(PM, pm_lobby.SET_UP)


class JoinGreeting(Base):
    """Live test 2026-11-23: someone joining by an invite link got four welcomes; one of ours, whose private
    offer (with 🔌) is already open, gets no extra "Click 👍 to get your agent" line in the room."""
    def setUp(self):
        super().setUp()
        self.lp.is_private_chat = lambda room: False
        self.lp.offer_thumb = mock.AsyncMock()
        self.room = project("!p", "night test")
        self.lp._lobby_greeted = {}

    def greet(self, who):
        with mock.patch.object(pm_lobby.server_admins, "is_admin", return_value=False):
            run(self.lp.on_room_join(self.room, who))

    def test_one_of_ours_with_my_private_offer_gets_no_room_greeting(self):
        self.lp._welcomes = {NEW: {"dm": "!dm", "offer": "$o", "done": []}}
        self.lp.client.rooms["!dm"] = SimpleNamespace(users={PM: None, NEW: None})
        self.greet(NEW)
        self.assertEqual(self.said, [])

    def test_one_who_left_my_private_chat_is_still_greeted(self):
        """GPT review: a stored welcome isn't enough; they may have left the chat, or never joined it."""
        self.lp._welcomes = {NEW: {"dm": "!dm", "offer": "$o", "done": []}}
        self.lp.client.rooms["!dm"] = SimpleNamespace(users={PM: None})
        self.greet(NEW)
        self.assertEqual(len(self.said), 1)

    def test_without_my_private_offer_the_greeting_still_comes(self):
        self.lp._welcomes = {}
        self.greet(NEW)
        self.assertEqual(len(self.said), 1)
        self.assertIn("Click 👍", self.said[0][1])
