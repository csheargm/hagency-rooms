"""The panel popped out of Element signs people in itself (server/panel_signin.py, static/panel.html).
Owner 2026-11-23: Element's pop-out button opened an almost blank page ("send me my console link")."""
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from server import panel
from server import panel_signin
from server import signin_page


class Route(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.made = []

        def reply(data, status=200):
            return web.json_response(data, status=status)

        def new_session(user):
            self.made.append(user)
            return "S-" + user
        app = web.Application()
        app.add_routes([panel_signin.route(lambda request, post: True, reply, new_session)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def ask(self, body):
        r = await self.client.post(panel_signin.PATH, json=body)
        return r.status, await r.json()

    async def test_a_session_of_ours_becomes_a_panel_session(self):
        with mock.patch.object(panel_signin.page_session, "whoami", return_value="@anna:x"):
            self.assertEqual(await self.ask({"access_token": "t" * 20}), (200, {"session": "S-@anna:x"}))

    async def test_anything_else_gets_no_session(self):
        with mock.patch.object(panel_signin.page_session, "whoami", return_value=None):
            self.assertEqual(await self.ask({"access_token": "nope"}), (200, {"error": panel_signin.FAILED}))
            self.assertEqual((await self.ask(["not", "a", "dict"]))[1], {"error": panel_signin.FAILED})
        self.assertEqual(self.made, [])

    async def test_too_many_asks_never_reach_the_homeserver(self):
        with mock.patch.object(signin_page, "may_ask", return_value=False), \
                mock.patch.object(panel_signin.page_session, "whoami") as whoami:
            self.assertEqual((await self.ask({"access_token": "t" * 20}))[0], 429)
        whoami.assert_not_called()


class Page(unittest.TestCase):
    def test_the_page_can_sign_in_with_our_homeserver_when_popped_out(self):
        self.assertIn('<script src="/auth/signin.js"></script>', panel.PAGE)
        self.assertIn(f'<meta name="hagency-homeserver" content="{panel.remote_agent.PUBLIC_HOMESERVER}">', panel.PAGE)
        self.assertIn(f"connect-src 'self' {panel.remote_agent.PUBLIC_HOMESERVER}", panel.HEADERS["Content-Security-Policy"])
        self.assertIn("showSignIn()", panel.PAGE)
        self.assertNotIn("__HAGENCY_", panel.PAGE)


if __name__ == "__main__":
    unittest.main()
