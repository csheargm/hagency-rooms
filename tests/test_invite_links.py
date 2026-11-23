"""User-flow tests: an owner makes, sends and cancels one-step invite links, and a person uses one
(pm_invite.py, invite_links.py; docs/adr/0014-one-step-invite-link.md).
Run: .venv/bin/python -m unittest tests.test_invite_links"""
import asyncio
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import RoomInviteError, RoomInviteResponse

from common.saved_dict import SavedDict
from server import console
from server import fed_openid
from server import invite_links
from server import pm_invite
from server import project_rooms
from tests.test_projects import ANNA, BETTY, PM, S, Base, project

NEW = f"@newbie:{S}"
TOKEN_RE = re.compile(r"/invite#([A-Za-z0-9_-]{40,})")


def run(coro):
    return asyncio.run(coro)


def said_to(sender, body):
    return SimpleNamespace(sender=sender, body=body, event_id="$x", server_timestamp=10**13,
                           source={"content": {"body": body, "m.mentions": {"user_ids": [PM]}}})


def fresh_store(test) -> Path:
    path = Path(tempfile.mkdtemp()) / "invite-links.json"
    p = mock.patch.object(invite_links, "_store", SavedDict(path))
    p.start()
    test.addCleanup(p.stop)
    fed_openid._tries.clear()
    return path


class Flow(Base):
    def setUp(self):
        super().setUp()
        self.path = fresh_store(self)
        self.room = project("!a", "Alpha", mods=(ANNA,))
        self.room.user_name = lambda u: {ANNA: "Anna"}.get(u)
        self.lp.client.rooms = {"!a": self.room}
        self.lp.client.room_invite = mock.AsyncMock(return_value=RoomInviteResponse())

    def ask(self, sender, body, room=None):
        return run(self.lp.on_welcome_text(room or self.room, said_to(sender, body)))

    def token(self) -> str:
        dm = [b for r, b in self.said if r == f"!dm-{ANNA}"]
        return TOKEN_RE.search(dm[-1]).group(1)

    def test_an_owner_gets_the_link_privately_and_the_room_only_hears_that(self):
        self.assertTrue(self.ask(ANNA, "@pm invite link"))
        token = self.token()
        room_lines = [b for r, b in self.said if r == "!a"]
        self.assertEqual(room_lines, ["I've sent you an invite link in our private chat, anna."])
        dm = self.said[0][1]
        self.assertIn("**Alpha**", dm)
        self.assertIn("within 7 days", dm)
        self.assertRegex(dm, r'say "cancel invite link [0-9a-f]{8}" here')
        self.assertTrue(dm.count(token) == 1 and "/invite#" in dm)
        self.assertNotIn(token, self.path.read_text())                # kept only as its hash

    def test_only_an_owner_or_maintainer_can_make_one(self):
        self.ask(BETTY, "@pm invite link")
        self.assertEqual(self.said, [("!a", pm_invite.ONLY_MODS)])
        self.assertEqual(dict(invite_links.store()), {})

    def test_never_in_the_welcome_room_the_lobby_or_a_private_chat(self):
        welcome = project("!w", "Welcome", mods=(ANNA,))
        with mock.patch.object(project_rooms, "FRONT_DOOR_IDS", {"!w"}):
            self.ask(ANNA, "@pm invite link", welcome)
        self.lp.dm_rooms[ANNA] = "!dm"
        self.ask(ANNA, "invite link", project("!dm", "pm", mods=(ANNA,)))
        self.assertEqual([b for _, b in self.said], [pm_invite.NOT_HERE, pm_invite.NOT_HERE])

    def test_for_how_many_days(self):
        self.ask(ANNA, "@pm make an invite link for 3 days")
        link = next(iter(invite_links.store().values()))
        self.assertAlmostEqual(link["expires"] - link["made"], 3 * invite_links.DAY_S)
        self.assertEqual(pm_invite.days_in("invite link for 400 days"), invite_links.MAX_DAYS)
        self.assertIsNone(pm_invite.days_in("why did the invite link fail"))   # a question is not a request

    def test_a_newcomer_uses_it_once_and_is_greeted(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "ok", "room": "!a"})
        self.lp.client.room_invite.assert_awaited_once_with("!a", NEW)
        self.assertEqual(self.said[-1], ("!a", f"👋 Welcome, {NEW}! Anna invited you here with a link."))
        self.assertEqual(run(self.lp.redeem_invite(token, BETTY)), {"state": "used"})   # never twice
        self.assertEqual(self.lp.client.room_invite.await_count, 1)

    def test_two_tabs_at_once_get_in_only_once(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()

        async def both():
            return await asyncio.gather(self.lp.redeem_invite(token, NEW), self.lp.redeem_invite(token, f"@other:{S}"))
        states = sorted(r["state"] for r in run(both()))
        self.assertEqual(states, ["ok", "used"])
        self.assertEqual(self.lp.client.room_invite.await_count, 1)

    def test_a_failed_invite_gives_the_link_back(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        self.lp.client.room_invite.return_value = RoomInviteError("banned", "M_FORBIDDEN")
        self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "failed"})
        self.lp.client.room_invite.return_value = RoomInviteResponse()
        self.assertEqual(run(self.lp.redeem_invite(token, NEW))["state"], "ok")

    def test_someone_already_in_goes_there_without_using_it(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        self.assertEqual(run(self.lp.redeem_invite(token, BETTY)), {"state": "member", "room": "!a"})
        self.lp.client.room_invite.assert_not_called()
        self.assertEqual(invite_links.look_up(token)[0], "ok")

    def test_a_link_from_someone_no_longer_a_maintainer_stops_working(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        self.room.power_levels = SimpleNamespace(get_user_level=lambda u: 0)
        self.mods["!a"] = set()
        self.assertEqual(run(self.lp.redeem_invite(token, NEW))["state"], "closed")
        self.lp.client.room_invite.assert_not_called()

    def test_a_cancel_during_the_invite_wins_even_if_the_invite_fails(self):
        """GPT review: the link was taken, the invite was in flight, the owner cancelled, the server said no."""
        self.ask(ANNA, "@pm invite link")
        token, link_id = self.token(), next(iter(invite_links.store().values()))["id"]

        async def refused_after_a_cancel(room_id, user):
            self.assertTrue(invite_links.cancel(link_id, lambda link: True))
            return RoomInviteError("banned", "M_FORBIDDEN")
        self.lp.client.room_invite.side_effect = refused_after_a_cancel
        self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "cancelled"})   # said as it is (GPT re-check)
        self.assertEqual(invite_links.look_up(token)[0], "cancelled")

    def test_no_answer_from_the_server_keeps_the_link_used(self):
        """GPT review: the invite may have gone through, so the link never reopens for someone else."""
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        self.lp.client.room_invite.side_effect = asyncio.TimeoutError()
        self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "unsure"})
        self.lp.client.room_invite.side_effect = None
        self.assertEqual(run(self.lp.redeem_invite(token, BETTY + "x")), {"state": "used"})

    def test_an_error_that_isnt_a_plain_no_keeps_the_link_used(self):
        """GPT re-check: a proxy's 502 page has no Matrix error code; the invite may have gone through."""
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        self.lp.client.room_invite.return_value = RoomInviteError("Bad gateway", None)
        self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "unsure"})
        self.assertEqual(invite_links.look_up(token)[0], "used")
        self.assertTrue(pm_invite.refused(RoomInviteError("banned", "M_FORBIDDEN")))

    def test_a_hung_invite_is_given_up_on_and_kept_used(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()

        async def never(*a):
            await asyncio.sleep(60)
        self.lp.client.room_invite.side_effect = never
        with mock.patch.object(pm_invite, "INVITE_WAIT_S", 0.05):
            self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "unsure"})
        self.assertEqual(invite_links.look_up(token)[0], "used")

    def test_a_link_from_someone_who_left_stops_working(self):
        """GPT review: a power level left behind by a maker who left the room counts for nothing."""
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        del self.room.users[ANNA]                         # still 100 in the power levels
        self.assertEqual(run(self.lp.redeem_invite(token, NEW))["state"], "closed")
        self.lp.client.room_invite.assert_not_called()
        self.assertEqual(invite_links.look_up(token)[0], "ok")

    def test_expired_links_let_nobody_in(self):
        self.ask(ANNA, "@pm invite link")
        token = self.token()
        later = invite_links.time.time() + 8 * invite_links.DAY_S
        with mock.patch.object(invite_links.time, "time", return_value=later):
            self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "expired"})
        self.lp.client.room_invite.assert_not_called()

    def test_the_maker_cancels_one_from_our_chat(self):
        self.ask(ANNA, "@pm invite link")
        token, link_id = self.token(), next(iter(invite_links.store().values()))["id"]
        self.ask(BETTY, f"@pm cancel invite link {link_id}")             # not hers, not a maintainer
        self.assertIn("couldn't find", self.said[-1][1])
        self.ask(ANNA, f"cancel invite link {link_id}", project(f"!dm-{ANNA}", "pm"))
        self.assertIn("Cancelled", self.said[-1][1])
        self.assertEqual(run(self.lp.redeem_invite(token, NEW)), {"state": "cancelled"})

    def test_cancel_all_of_a_rooms_links(self):
        for _ in range(3):
            self.ask(ANNA, "@pm invite link")
        self.ask(BETTY, "@pm cancel invite links")
        self.assertIn("Only the room's owner", self.said[-1][1])
        self.ask(ANNA, "@pm cancel all invite links")
        self.assertEqual(self.said[-1][1], "Cancelled 3 invite links.")

    def test_from_the_panel_button(self):
        self.lp.is_person = lambda room, user: True
        self.assertEqual(run(console.act(self.lp, "!a", ANNA, "invite_link", "")),
                         "I've sent you an invite link in our private chat, anna.")
        self.assertRegex(self.said[0][1], TOKEN_RE)
        self.assertEqual(run(console.act(self.lp, "!a", BETTY, "invite_link", "")), pm_invite.ONLY_MODS)

    def test_the_token_is_never_logged(self):
        with self.assertLogs(level="DEBUG") as logs:
            self.ask(ANNA, "@pm invite link")
            token = self.token()
            run(self.lp.redeem_invite(token, NEW))
            self.lp.client.room_invite.return_value = RoomInviteError("no")
            run(self.lp.redeem_invite(token, BETTY))
        self.assertTrue(logs.output)
        self.assertFalse([line for line in logs.output if token in line])


class Store(Base):
    def setUp(self):
        super().setUp()
        fresh_store(self)

    def test_limits_per_room_and_per_person(self):
        for i in range(invite_links.PER_ROOM_OPEN):
            invite_links.create("!a", f"@m{i}:{S}")
        self.assertIn("Cancel some first", invite_links.refusal("!a", ANNA))
        for _ in range(invite_links.PER_PERSON_DAY):
            invite_links.create("!b", ANNA)
        self.assertIn("tomorrow", invite_links.refusal("!c", ANNA))

    def test_anything_that_isnt_a_token_is_unknown(self):
        for junk in (None, "", 5, "a" * 500, "../../etc", "ü" * 10, {"$ne": 1}):
            self.assertEqual(invite_links.look_up(junk), ("unknown", None))

    def test_old_links_are_dropped(self):
        token, _ = invite_links.create("!a", ANNA, now=0)
        invite_links.create("!a", ANNA, now=40 * invite_links.DAY_S)
        self.assertEqual(len(invite_links.store()), 1)
        self.assertEqual(invite_links.look_up(token)[0], "unknown")

    def test_lookups_are_limited_per_address(self):
        allowed = sum(invite_links.may_look("198.51.100.7") for _ in range(invite_links.LOOKUPS_PER_CLIENT + 5))
        self.assertEqual(allowed, invite_links.LOOKUPS_PER_CLIENT)
        self.assertTrue(invite_links.may_look("198.51.100.8"))
