"""User-flow tests of the invite page's server side: open the link, check it, sign in, join
(invite_page.py; docs/adr/0014-one-step-invite-link.md). The browser's part (static/invite.js) talks
to the homeserver; here the homeserver's answer to "whose OpenID token is this" is stubbed.
Run: .venv/bin/python -m unittest tests.test_invite_page"""
import re
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nio import RoomInviteResponse

from server import console
from server import invite_links
from server import invite_page
from server import remote_agent
from server.loop import Loop
from tests.test_invite_links import NEW, fresh_store
from tests.test_projects import ANNA, PM, project

PUBLIC = "https://rooms.example.org"
HS = "https://matrix.example.org"


class Page(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = fresh_store(self)
        for p in (mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": PUBLIC}),
                  mock.patch.object(remote_agent, "PUBLIC_HOMESERVER", HS),
                  mock.patch.object(invite_page, "ELEMENT_ORIGIN", PUBLIC),
                  mock.patch.object(console, "openid_user", side_effect=lambda t: NEW if t == "good-openid" else None)):
            p.start()
            self.addCleanup(p.stop)
        self.room = project("!a", "Alpha <b>team</b>", mods=(ANNA,))
        self.room.user_name = lambda u: {ANNA: "Anna"}.get(u)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.dm_rooms, self.said = PM, {}, []
        self.lp.client = SimpleNamespace(rooms={"!a": self.room},
                                         room_invite=mock.AsyncMock(return_value=RoomInviteResponse()))

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
        self.lp.say = say
        self.mods = mock.patch("common.room_state.is_moderator", mock.AsyncMock(return_value=True))
        self.mods.start()
        self.addCleanup(self.mods.stop)
        app = web.Application()
        app.add_routes(invite_page.routes(self.lp))
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.token, _ = invite_links.create("!a", ANNA)

    async def post(self, path, body, **headers):
        r = await self.client.post(f"/invite/{path}", json=body, headers={"Origin": PUBLIC, **headers})
        return r.status, await r.json()

    async def test_the_page_allows_requests_only_here_and_to_our_homeserver(self):
        r = await self.client.get("/invite")
        page = await r.text()
        csp = r.headers["Content-Security-Policy"]
        self.assertEqual(re.findall(r"connect-src [^;]*", csp), [f"connect-src 'self' {HS}"])
        self.assertIn("form-action 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual((r.headers["Referrer-Policy"], r.headers["Cache-Control"]), ("no-referrer", "no-store"))
        self.assertIn(f'<meta name="hagency-homeserver" content="{HS}">', page)
        self.assertNotIn("__HAGENCY", page)
        for path in invite_page.ASSETS:
            self.assertEqual((await self.client.get(path)).status, 200)

    async def test_the_page_never_reads_the_request(self):
        plain = await (await self.client.get("/invite")).text()
        self.assertEqual(plain, await (await self.client.get(f"/invite?t={self.token}")).text())
        self.assertNotIn("Alpha", plain)                     # the room is named only to a valid link's check

    async def test_a_valid_link_names_the_room_and_who_invited(self):
        self.assertEqual(await self.post("check", {"token": self.token}),
                         (200, {"state": "ok", "room": "Alpha <b>team</b>", "by": "Anna"}))   # shown as text by the page

    async def test_a_link_whose_maker_left_names_no_room(self):
        del self.room.users[ANNA]
        self.assertEqual(await self.post("check", {"token": self.token}), (200, {"state": "closed"}))

    async def test_a_newcomer_signs_in_and_is_invited_once(self):
        status, body = await self.post("redeem", {"token": self.token, "openid": "good-openid"})
        self.assertEqual((status, body), (200, {"state": "ok", "room": "!a"}))
        self.lp.client.room_invite.assert_awaited_once_with("!a", NEW)
        self.assertIn(("!a", f"👋 Welcome, {NEW}! Anna invited you here with a link."), self.said)
        self.assertEqual(await self.post("check", {"token": self.token}), (200, {"state": "used"}))
        self.assertEqual((await self.post("redeem", {"token": self.token, "openid": "good-openid"}))[1], {"state": "used"})

    async def test_no_sign_in_no_join_and_the_link_stays(self):
        status, body = await self.post("redeem", {"token": self.token, "openid": "made-up"})
        self.assertEqual((status, body), (401, {"state": "not_signed_in"}))
        self.lp.client.room_invite.assert_not_called()
        self.assertEqual((await self.post("check", {"token": self.token}))[1]["state"], "ok")

    async def test_used_expired_or_cancelled_links_say_so_and_name_no_room(self):
        expired, _ = invite_links.create("!a", ANNA, days=1, now=invite_links.time.time() - 2 * invite_links.DAY_S)
        cancelled, link_id = invite_links.create("!a", ANNA)
        invite_links.cancel(link_id, lambda link: True)
        for token, state in ((expired, "expired"), (cancelled, "cancelled"), ("x" * 43, "unknown")):
            status, body = await self.post("check", {"token": token})
            self.assertEqual((status, body), (200, {"state": state}))
            self.assertEqual((await self.post("redeem", {"token": token, "openid": "good-openid"}))[1], {"state": state})
        self.lp.client.room_invite.assert_not_called()

    async def test_lookups_are_rate_limited_per_address(self):
        states = [(await self.post("check", {"token": "x" * 43}))[0] for _ in range(invite_links.LOOKUPS_PER_CLIENT + 1)]
        self.assertEqual(states[-1], 429)
        self.assertEqual((await self.post("redeem", {"token": self.token, "openid": "good-openid"}))[0], 429)
        self.lp.client.room_invite.assert_not_called()

    async def test_only_json_from_our_own_page(self):
        r = await self.client.post("/invite/check", json={"token": self.token}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/invite/check", data=f"token={self.token}", headers={"Origin": PUBLIC})
        self.assertEqual(r.status, 403)

    async def test_the_token_is_never_logged_nor_stored(self):
        with self.assertLogs(level="DEBUG") as logs:
            await self.post("check", {"token": self.token})
            await self.post("redeem", {"token": self.token, "openid": "good-openid"})
            await self.post("redeem", {"token": self.token, "openid": "good-openid"})
        self.assertFalse([line for line in logs.output if self.token in line])
        self.assertNotIn(self.token, self.path.read_text())


class Proxy(IsolatedAsyncioTestCase):
    def test_nothing_logs_request_addresses(self):
        """GPT review: Google/GitHub sign-in comes back to /invite?loginToken=..., a one-time login.
        Neither Caddy (no log directive) nor @pm (access_log=None) writes request addresses down."""
        from common import paths
        caddy = (paths.ROOT / "deploy" / "Caddyfile").read_text()
        self.assertFalse(re.search(r"^\s*log\b", caddy, re.M))
        self.assertIn("access_log=None", (paths.ROOT / "server" / "panel.py").read_text())

    def test_caddy_sends_every_invite_path_to_pm(self):
        """The page, its script and style and both calls reach @pm, on the public server's name."""
        from common import paths
        line = next(x for x in (paths.ROOT / "deploy" / "Caddyfile").read_text().splitlines() if x.strip().startswith("@pm path"))
        matchers = line.split()[2:]
        lp = SimpleNamespace(client=SimpleNamespace(rooms={}))
        for route in invite_page.routes(lp):
            self.assertTrue(route.path in matchers or any(m.endswith("/*") and route.path.startswith(m[:-1]) for m in matchers),
                            route.path)
