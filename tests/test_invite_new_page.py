"""User flow (R6, docs/adr/0015-one-click-home-steps.md): Element's home page step ③. A room's owner
opens /invite/new, signs in (the homeserver's answer to "whose OpenID token is this" is stubbed),
sees the project rooms where they may make links, makes one, and a newcomer uses it on the invite
page (ADR 0014) and is invited. Run: .venv/bin/python -m unittest tests.test_invite_new_page"""
import re
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nio import RoomInviteResponse

from server import console
from server import invite_links
from server import invite_new_page
from server import invite_page
from server import pm_invite
from server import project_rooms
from server import remote_agent
from server.loop import Loop
from tests.test_invite_links import NEW, fresh_store
from tests.test_projects import ANNA, BETTY, PM, project

PUBLIC, HS = "https://rooms.example.org", "https://matrix.example.org"
WHO = {"anna-openid": ANNA, "betty-openid": BETTY, "new-openid": NEW}


class MakeALink(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = fresh_store(self)
        for p in (mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": PUBLIC}),
                  mock.patch.object(remote_agent, "PUBLIC_HOMESERVER", HS),
                  mock.patch.object(console, "openid_user", side_effect=WHO.get),
                  mock.patch.object(project_rooms, "FRONT_DOOR_IDS", {"!welcome"})):
            p.start()
            self.addCleanup(p.stop)
        rooms = {"!b": project("!b", "Beta"), "!a": project("!a", "Alpha <b>team</b>"),
                 "!c": project("!c", "Betty's", mods=(BETTY,)), "!welcome": project("!welcome", "Welcome")}
        rooms["!d"] = project("!d", "Invited only")
        rooms["!d"].invited_users = {ANNA: None}
        for room in rooms.values():
            room.user_name = lambda u: {ANNA: "Anna"}.get(u)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.dm_rooms, self.said = PM, {}, []
        self.lp.client = SimpleNamespace(rooms=rooms, room_get_state=mock.AsyncMock(return_value=None),
                                         room_invite=mock.AsyncMock(return_value=RoomInviteResponse()))

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
        self.lp.say = say
        app = web.Application()
        app.add_routes([*invite_new_page.routes(self.lp), *invite_page.routes(self.lp)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, path, body):
        r = await self.client.post(path, json=body, headers={"Origin": PUBLIC})
        return r.status, await r.json()

    async def test_the_page_signs_in_only_with_our_homeserver(self):
        r = await self.client.get("/invite/new")
        self.assertEqual(re.findall(r"connect-src [^;]*", r.headers["Content-Security-Policy"]), [f"connect-src 'self' {HS}"])
        page = await r.text()
        self.assertNotIn("__HAGENCY", page)
        self.assertIn("/auth/signin.js", page)
        self.assertEqual((await self.client.get("/invite/new/page.js")).status, 200)

    async def test_an_owner_makes_a_link_on_the_page_and_a_newcomer_joins_with_it(self):
        status, got = await self.post("/invite/new/rooms", {"openid": "anna-openid"})
        self.assertEqual((status, got), (200, {"state": "ok", "rooms": [{"id": "!a", "name": "Alpha <b>team</b>"},
                                                                      {"id": "!b", "name": "Beta"}]}))   # by name
        with self.assertLogs("loop", "INFO") as logs:
            status, made = await self.post("/invite/new/make", {"openid": "anna-openid", "room": "!a", "days": 3})
        self.assertEqual((status, made["state"], made["days"]), (200, "ok", 3))
        token = made["link"].split("/invite#", 1)[1]
        self.assertTrue(made["link"].startswith(f"{PUBLIC}/invite#"))
        self.assertFalse([line for line in logs.output if token in line])        # its id only
        self.assertEqual(self.said, [])                                            # shown on the page, in no chat
        self.assertEqual(await self.post("/invite/check", {"token": token}),
                         (200, {"state": "ok", "room": "Alpha <b>team</b>", "by": "Anna"}))
        self.assertEqual((await self.post("/invite/redeem", {"token": token, "openid": "new-openid"}))[1],
                         {"state": "ok", "room": "!a"})
        self.lp.client.room_invite.assert_awaited_once_with("!a", NEW)
        link, = invite_links.store().values()
        self.assertLess(abs(link["expires"] - link["made"] - 3 * invite_links.DAY_S), 1)

    async def test_only_rooms_where_they_are_owner_or_maintainer(self):
        self.assertEqual((await self.post("/invite/new/rooms", {"openid": "betty-openid"}))[1],
                         {"state": "ok", "rooms": [{"id": "!c", "name": "Betty's"}]})
        for room in ("!a", "!welcome", "!nowhere", ["!a"]):
            status, got = await self.post("/invite/new/make", {"openid": "betty-openid", "room": room})
            self.assertEqual((status, got["state"]), (200, "refused"), room)
        self.assertEqual((await self.post("/invite/new/make", {"openid": "anna-openid", "room": "!d"}))[1]["state"], "refused")
        self.assertEqual((await self.post("/invite/new/make", {"openid": "anna-openid", "room": "!welcome"}))[1],
                         {"state": "refused", "error": pm_invite.NOT_HERE})
        self.assertEqual(len(invite_links.store()), 0)

    async def test_the_same_limits_as_in_the_chat(self):
        for _ in range(invite_links.PER_PERSON_DAY):
            self.assertEqual((await self.post("/invite/new/make", {"openid": "anna-openid", "room": "!b"}))[1]["state"], "ok")
        got = (await self.post("/invite/new/make", {"openid": "anna-openid", "room": "!b"}))[1]
        self.assertEqual(got, {"state": "refused", "error": invite_links.refusal("!b", ANNA)})
        self.assertIn("today", got["error"])

    async def test_days_are_one_of_the_choices_else_the_default(self):
        for days, kept in ((30, 30), (1, 1), (999, 7), ("3", 7), (True, 7)):
            self.assertEqual((await self.post("/invite/new/make", {"openid": "anna-openid", "room": "!a", "days": days}))[1]["days"], kept)

    async def test_no_sign_in_no_rooms_and_no_link(self):
        self.assertEqual(await self.post("/invite/new/rooms", {"openid": "made-up"}), (401, {"state": "not_signed_in"}))
        self.assertEqual(await self.post("/invite/new/make", {"room": "!a"}), (401, {"state": "not_signed_in"}))
        r = await self.client.post("/invite/new/make", json={"openid": "anna-openid", "room": "!a"},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertEqual(len(invite_links.store()), 0)
