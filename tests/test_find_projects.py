"""User flow: "@pm find a project", then 🙋 under a listed room, then its owner's 👍 (pm_find.py,
pm_joining.py). Screen test: one block of raw room-ID pills, and Join failed with 403 "You are not
invited to this room". Run: .venv/bin/python -m unittest tests.test_find_projects"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomInviteResponse, RoomKickResponse

from server import pm_find
from server import project_rooms
from tests.test_projects import ANNA, PM, S, Base, project

NEW = f"@newbie1:{S}"


def run(coro):
    return asyncio.run(coro)


def click(sender, key, to):
    return SimpleNamespace(sender=sender, key=key, reacts_to=to, server_timestamp=10**13)


class FindAndAsk(Base):
    def setUp(self):
        super().setUp()
        self.lobby = project("!l", "Lobby")
        self.final = project("!fin", "final project")                  # listed, no repo
        self.final.topic = "Our   class's\nfinal project"
        self.pub, self.sec = project("!pub", "Pub"), project("!sec", "Sec")
        self.pub.canonical_alias = f"#pub:{S}"
        self.lp.client.rooms = {"!l": self.lobby, "!fin": self.final, "!pub": self.pub, "!sec": self.sec,
                                "!hid": project("!hid", "Hidden")}
        self.bind("!pub", "a/public-one")
        self.bind("!sec", "a/secret-one")
        for rid, vis in (("!fin", "listed"), ("!pub", "open"), ("!sec", "listed")):
            self.state.setdefault(rid, {})[project_rooms.VISIBILITY_EVENT] = ({"visibility": vis}, PM)
            self.members[(rid, ANNA)], self.mods[rid] = "join", {ANNA}
        self.lp.addressed_text = lambda event, room=None: event.body
        self.lp.client.room_invite = mock.AsyncMock(return_value=RoomInviteResponse())
        p = mock.patch("server.project_rooms.repo_is_private", lambda repo: asyncio.sleep(0, repo == "a/secret-one"))
        p.start()
        self.addCleanup(p.stop)

    def find(self, who=NEW):
        async def ask():
            await self.lp.on_project_text(self.lobby, SimpleNamespace(sender=who, body="find a project"))
            await asyncio.gather(*self.lp.busy)
        run(ask())
        return self.find_said()

    def line(self, name):
        return next((f"$e{e}", b) for e, b in self.find_said() if b.startswith(f"**{name}**"))

    def find_said(self):
        return [(e, b) for e, (r, b) in enumerate(self.said, 1) if r == "!l"]

    def test_one_plain_message_per_project_and_never_a_raw_id_or_a_private_repo(self):
        said = [b for _, b in self.find()]
        self.assertEqual(said[0], pm_find.HEADER)
        self.assertEqual(len(said), 4)                                    # header + three listed or open rooms
        self.assertIn("**final project**: Our class's final project. Click 🙋 below to ask to join", said[1])
        self.assertIn("**Pub** · a/public-one. Anyone can join: [join it](https://matrix.to/#/%23pub%3A", said[2])
        self.assertTrue(said[3].startswith("**Sec**. Click 🙋"), said[3])
        self.assertNotIn("secret-one", "".join(said))
        self.assertNotIn("Hidden", "".join(said))
        self.assertNotIn("!fin", "".join(said))                            # no raw room ID shown
        self.assertEqual([(r, k) for r, _, k in self.buttons], [("!l", "🙋"), ("!l", "🙋")])   # listed only

    def test_a_declined_knock_also_settles_their_click_so_no_later_thumbs_up_invites(self):
        """GPT review: a knock and 🙋 for the same room were two decisions; 👎 on one, 👍 on the other."""
        self.lp.client.room_kick = mock.AsyncMock(return_value=RoomKickResponse())
        self.find()
        event, _ = self.line("final project")
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))
        found_q = next(iter(self.lp._join_requests))
        self.members[("!fin", NEW)], self.member_ids[("!fin", NEW)] = "knock", "$k"
        run(self.lp.on_knock(self.final, NEW, "$k"))
        knock_q, req = next((e, r) for e, r in self.lp._join_requests.items() if r["knock"] == "$k")
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=req["dm"]), click(ANNA, "👎", knock_q)))
        self.lp.client.room_kick.assert_awaited_once()
        self.members[("!fin", NEW)] = "leave"
        self.assertEqual(self.lp._join_requests, {})
        run(self.lp.on_join_reaction(SimpleNamespace(room_id=req["dm"]), click(ANNA, "👍", found_q)))
        self.lp.client.room_invite.assert_not_called()

    def test_their_click_asks_the_owners_and_a_thumbs_up_lets_them_in(self):
        """A room with no repo: asked by room, without Element's knock UI."""
        self.find()
        event, _ = self.line("final project")
        self.assertTrue(run(self.lp.on_project_reaction(self.lobby, click(ANNA, "🙋", event))))   # not theirs
        self.assertEqual(self.lp._join_requests, {})
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))
        self.assertIn("I've asked the owners of **final project**", self.said[-1][1])
        q, req = next(iter(self.lp._join_requests.items()))
        self.assertEqual((req["room"], req["person"], req["moderator"]), ("!fin", NEW, ANNA))
        self.assertIn(f"{NEW} asks to join **final project**", dict(self.said)[req["dm"]])
        self.assertEqual(self.buttons[-2:], [(req["dm"], q, "👍"), (req["dm"], q, "👎")])
        asked = len(self.said)
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))                   # once
        self.assertEqual(len(self.said), asked)
        run(self.lp.on_project_reaction(SimpleNamespace(room_id=req["dm"]), click(ANNA, "👍", q)))
        self.lp.client.room_invite.assert_awaited_once_with("!fin", NEW)

    def test_a_room_made_private_since_is_never_asked_about(self):
        self.find()
        event, _ = self.line("final project")
        self.state["!fin"][project_rooms.VISIBILITY_EVENT] = ({"visibility": "private"}, PM)
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))
        self.assertEqual(self.said[-1][1], pm_find.GONE)
        self.assertEqual(self.lp._join_requests, {})

    def test_an_unreadable_room_keeps_the_button_and_never_reads_as_unlisted(self):
        """GPT review: a failed state read looked private, so the click was used up."""
        self.find()
        event, _ = self.line("final project")
        real = self.lp.client.room_get_state
        self.lp.client.room_get_state = mock.AsyncMock(return_value=SimpleNamespace(message="timeout"))
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))
        self.assertIn("click 🙋 again in a minute", self.said[-1][1])
        self.lp.client.room_get_state = real
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))
        self.assertIn("I've asked the owners", self.said[-1][1])

    def test_a_room_made_private_while_listing_is_left_out(self):
        real = self.lp.find_projects

        async def then_private(person):
            found = await real(person)
            self.state["!sec"][project_rooms.VISIBILITY_EVENT] = ({"visibility": "private"}, PM)
            return found
        self.lp.find_projects = then_private
        self.lp._find_offers = {"$old": {"room": "!fin", "person": NEW, "chat": "!l", "at": 0}}
        said = [b for _, b in self.find()]
        self.assertFalse(any(b.startswith("**Sec**") for b in said), said)
        self.assertNotIn("$old", self.lp._find_offers)                                     # expired: pruned

    def test_the_reply_names_the_room_as_it_was_shown_and_nothing_listed_says_so(self):
        """GPT re-check: a rename during the ask never reaches the chat; no header for nothing."""
        self.find()
        event, _ = self.line("final project")
        self.final.display_name = "renamed in secret"
        run(self.lp.on_project_reaction(self.lobby, click(NEW, "🙋", event)))
        self.assertNotIn("renamed", "".join(b for r, b in self.said if r == "!l"))   # the moderators may see it
        for rid in ("!fin", "!pub", "!sec"):
            self.state[rid][project_rooms.VISIBILITY_EVENT] = ({"visibility": "private"}, PM)
        self.lp._finds = {}
        self.assertEqual([b for _, b in self.find()][-1], pm_find.NONE_YET)

    def test_members_see_they_are_in_and_listing_is_rate_limited(self):
        said = [b for _, b in self.find(ANNA)]
        self.assertTrue(all("You're in it already" in b for b in said[1:]), said)
        self.assertEqual(self.buttons, [])
        for _ in range(pm_find.FINDS_PER_HOUR - 1):
            self.find(ANNA)
        self.assertIn("I just listed them", self.find(ANNA)[-1][1])

    def test_nothing_listed_says_how_to_make_one(self):
        self.lp.client.rooms = {"!l": self.lobby}
        self.assertEqual([b for _, b in self.find()], [pm_find.NONE_YET])


if __name__ == "__main__":
    unittest.main()
