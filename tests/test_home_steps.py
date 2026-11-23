"""User flow (R6, docs/adr/0015-one-click-home-steps.md): Element's home page step ① Get your agent,
with no detour through the chat with @pm. The person opens /connect/new, signs in (the homeserver's
answer to "whose OpenID token is this" is stubbed), chooses the AI, gets a one-time code bound to
them, and runs the Connect page's line: POST /connect answers with their first agent's sign-in and
that AI, and connect.py runs it. Only the network edges are faked, as in test_first_agent_ai.
Run: .venv/bin/python -m unittest tests.test_home_steps"""
import asyncio
import re
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common import registry
from server import agent_page
from server import console
from server import fed_openid
from server import pm_first
from server import remote_agent
from server import signin_page
from tests import test_kind_flow as kind_flow
from tests.test_more_agents_flows import S

NEWB = f"@newb:{S}"
PUBLIC, HS = "https://rooms.example.org", "https://matrix.example.org"
OLLAMA = {"name": "", "agent_kind": "ollama", "model": "qwen3:8b", "base_url": ""}


class GetYourAgent(kind_flow.KindFlow):
    def setUp(self):
        super().setUp()
        fakes = ((remote_agent, "open_dm", lambda agent, password, person: "!agentdm"),
                 (remote_agent.setup_accounts, "ensure_user", mock.Mock(return_value=("agent-pw", False))),
                 (remote_agent.setup_accounts, "shared_secret", lambda: "s"),
                 (remote_agent, "PUBLIC_HOMESERVER", HS),
                 (console, "openid_user", lambda t: NEWB if t == "newb-openid" else None),
                 (fed_openid, "_tries", {}),
                 (kind_flow.on_computer.connect_kinds, "problem", mock.Mock(return_value=None)),
                 (kind_flow.on_computer.connect_kinds.subprocess, "run", mock.Mock(return_value=mock.Mock(returncode=0))))
        for target, name, value in fakes:
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": PUBLIC})
        p.start()
        self.addCleanup(p.stop)
        self.lp.client = SimpleNamespace(rooms={})

    def call(self, *requests):
        """[(status, JSON or text)] for (method, path, body) through @pm's real routes."""
        async def go():
            app = web.Application()
            app.add_routes([*agent_page.routes(self.lp), *signin_page.routes()])
            out = []
            async with TestClient(TestServer(app)) as client:
                for method, path, body in requests:
                    r = await (client.get(path) if method == "GET" else
                               client.post(path, json=body, headers={"Origin": PUBLIC}))
                    out.append((r.status, await (r.text() if method == "GET" else r.json()), r.headers))
            return out
        return asyncio.run(go())

    def ask(self, path, body):
        status, data, _ = self.call(("POST", path, body))[0]
        return status, data

    def test_the_page_signs_in_only_with_our_homeserver_and_lists_every_kind(self):
        (status, page, headers), (js, _, _) = self.call(("GET", "/connect/new", None), ("GET", "/connect/new/page.js", None))
        self.assertEqual((status, js), (200, 200))
        self.assertEqual(re.findall(r"connect-src [^;]*", headers["Content-Security-Policy"]), [f"connect-src 'self' {HS}"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual((headers["Cache-Control"], headers["Referrer-Policy"]), ("no-store", "no-referrer"))
        self.assertNotIn("__HAGENCY", page)
        self.assertIn('"kind": "hermes"', page)
        self.assertIn('<script src="/auth/signin.js" defer></script>', page)
        self.assertEqual(self.call(("GET", signin_page.SCRIPT_PATH, None))[0][0], 200)

    def test_a_newcomer_signs_in_chooses_ollama_and_connects_from_the_page(self):
        self.assertEqual(self.ask("/connect/new/me", {"openid": "newb-openid"}),
                         (200, {"state": "ok", "agent": "newb-agent", "connected": False, "pick": True, "runs": "",
                                "picked": {}}))
        with self.assertLogs("loop", "INFO") as logs:
            status, got = self.ask("/connect/new/code", {"openid": "newb-openid", "pick": OLLAMA})
        self.assertEqual((status, got["state"], got["kind"]), (200, "ok", "ollama"))
        self.assertFalse([line for line in logs.output if got["code"] in line])   # the code is in the reply only
        self.assertEqual(registry.find(NEWB, None)["agent_kind"], "ollama")        # recorded, as the picker does
        answer, install, saved, _ = self.run_on_computer(f"`{got['code']}`")       # the Connect page's line
        self.assertEqual(answer["user_id"], f"@newb-agent:{S}")                     # bound to who signed in
        self.assertEqual((answer["agent_kind"], answer["model"]), ("ollama", "qwen3:8b"))
        self.assertEqual((install.args[2], saved["model"]), ("ollama", "qwen3:8b"))

    def test_a_new_code_replaces_the_old_one_and_works_once(self):
        first = self.ask("/connect/new/code", {"openid": "newb-openid", "pick": None})[1]["code"]
        second = self.ask("/connect/new/code", {"openid": "newb-openid", "pick": None})[1]["code"]
        self.assertIsNone(remote_agent.CODES.redeem(first))
        self.assertEqual(remote_agent.CODES.redeem(second), NEWB)
        self.assertIsNone(remote_agent.CODES.redeem(second))

    def test_without_a_real_sign_in_nothing_is_made(self):
        self.assertEqual(self.ask("/connect/new/code", {"openid": "made-up", "pick": OLLAMA}), (401, {"state": "not_signed_in"}))
        self.assertEqual(self.ask("/connect/new/me", {}), (401, {"state": "not_signed_in"}))
        self.assertIsNone(registry.find(NEWB, None))

    def test_only_json_from_our_own_page(self):
        async def go():
            app = web.Application()
            app.add_routes(agent_page.routes(self.lp))
            async with TestClient(TestServer(app)) as client:
                evil = await client.post("/connect/new/code", json={"openid": "newb-openid"}, headers={"Origin": "https://evil.example"})
                form = await client.post("/connect/new/code", data="openid=newb-openid", headers={"Origin": PUBLIC})
                return evil.status, form.status
        self.assertEqual(asyncio.run(go()), (403, 403))
        self.assertIsNone(registry.find(NEWB, None))

    def test_once_connected_it_keeps_its_ai_and_another_computer_gets_a_new_code(self):
        self.ask("/connect/new/code", {"openid": "newb-openid", "pick": OLLAMA})
        self.lp._seen = {"newb-agent": {"online": True, "ever": True}}
        status, me = self.ask("/connect/new/me", {"openid": "newb-openid"})
        self.assertEqual((me["connected"], me["pick"]), (True, False))
        self.assertEqual(self.ask("/connect/new/code", {"openid": "newb-openid", "pick": {**OLLAMA, "agent_kind": "claude",
                                                                                          "model": ""}})[1],
                         {"state": "bad_pick", "error": pm_first.CONNECTED})
        got = self.ask("/connect/new/code", {"openid": "newb-openid", "pick": None})[1]
        self.assertEqual((got["state"], got["kind"]), ("ok", "ollama"))

    def test_a_bad_choice_says_why_and_an_api_key_is_never_taken(self):
        bad = self.ask("/connect/new/code", {"openid": "newb-openid", "pick": {**OLLAMA, "agent_kind": "remote"}})[1]
        self.assertEqual(bad["state"], "bad_pick")
        self.assertIn("base URL", bad["error"])
        key = self.ask("/connect/new/code", {"openid": "newb-openid", "pick": {**OLLAMA, "api_key": "sk-" + "a" * 30}})[1]
        self.assertEqual(key["state"], "bad_pick")
        self.assertIsNone(registry.find(NEWB, None))

    def test_someone_who_removed_their_first_agent_is_sent_to_my_agents(self):
        registry.update(NEWB, None, agent=registry.REMOTE)
        with mock.patch.object(registry, "first_removed", return_value=True):
            self.assertEqual(self.ask("/connect/new/me", {"openid": "newb-openid"})[1],
                             {"state": "refused", "error": agent_page.REMOVED})

    def test_one_set_up_at_a_time(self):
        self.lp._provisioning = {NEWB}
        self.assertEqual(self.ask("/connect/new/code", {"openid": "newb-openid", "pick": None})[1],
                         {"state": "busy", "error": agent_page.BUSY})

    def test_asks_are_rate_limited_per_address(self):
        states = [self.ask("/connect/new/me", {"openid": "made-up"})[0] for _ in range(signin_page.TRIES_PER_CLIENT + 1)]
        self.assertEqual(states[-1], 429)
        self.assertEqual(self.ask("/connect/new/code", {"openid": "newb-openid", "pick": None})[0], 429)
        self.assertIsNone(registry.find(NEWB, None))


if __name__ == "__main__":
    import unittest
    unittest.main()
