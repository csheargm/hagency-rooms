"""Unit tests: the Hagency console (settings in the side panel / a page). Run: .venv/bin/python -m unittest tests.test_console"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import console
from common import registry
from common import room_state

S = "bridge.localhost"
ANNA, BETTY, MALLORY = f"@anna:{S}", f"@betty:{S}", f"@mallory:{S}"


class Auth(unittest.TestCase):
    def test_a_link_works_once_expires_and_is_for_one_room(self):
        code = console.new_link(ANNA, "!r")
        self.assertEqual(console.redeem(code), (ANNA, "!r"))
        self.assertIsNone(console.redeem(code))                       # single use
        old = console.new_link(ANNA, "!r")
        console._links[old] = (ANNA, time.time() - 1, "!r")
        self.assertIsNone(console.redeem(old))

    def test_a_link_session_works_only_in_its_room(self):
        token = console.new_session(ANNA, "!r")
        req = SimpleNamespace(headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(console.session_of(req), (ANNA, "!r"))
        self.assertIsNone(console.session_of(SimpleNamespace(headers={"Authorization": token})))   # Bearer required

    def test_only_json_from_our_own_origin(self):
        ok = SimpleNamespace(host="localhost:8650", content_type="application/json", headers={"Origin": "http://localhost:8650"})
        self.assertTrue(console.allowed(ok, True))
        for bad in (SimpleNamespace(host="evil.example", content_type="application/json", headers={}),
                    SimpleNamespace(host="localhost:8650", content_type="text/plain", headers={}),
                    SimpleNamespace(host="localhost:8650", content_type="application/json", headers={"Origin": "https://evil.example"})):
            self.assertFalse(console.allowed(bad, True))

    def test_the_public_name_is_allowed_only_when_it_is_this_servers(self):
        """The public server: Caddy forwards Host rooms.example.org; other names stay refused."""
        public = SimpleNamespace(host="rooms.example.org", content_type="application/json",
                                 headers={"Origin": "https://rooms.example.org"})
        self.assertFalse(console.allowed(public, True))
        with mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": "https://rooms.example.org"}):
            self.assertTrue(console.allowed(public, True))
            self.assertTrue(console.allowed(SimpleNamespace(host="localhost:8650", headers={}), False))
            self.assertFalse(console.allowed(SimpleNamespace(host="rooms.example.org.evil.example", headers={}), False))
            self.assertEqual(console.public_url(), "https://rooms.example.org")
            for origin in ("http://rooms.example.org", "https://rooms.example.org:8443", "http://[bad", ""):   # exact only
                self.assertFalse(console.allowed(SimpleNamespace(host="rooms.example.org", content_type="application/json",
                                                                 headers={"Origin": origin}), True), origin)

    def test_openid_tokens_are_verified_with_the_homeserver_and_only_local_users_count(self):
        def opener(sub):
            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"sub": sub}).encode()
            return lambda url, timeout=None: R()
        def build(sub, fail=False):
            def opener(*handlers):
                return SimpleNamespace(open=(lambda url, timeout=None: (_ for _ in ()).throw(OSError("401"))) if fail
                                       else (lambda url, timeout=None: Resp(sub)))
            return opener

        class Resp:
            def __init__(self, sub):
                self.sub = sub

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"sub": self.sub} if self.sub != "notadict" else ["x"]).encode()
        for sub, want in ((ANNA, ANNA), ("@anna:evil.example", None), ("@anna:evil.bridge.localhost", None),
                          ("notadict", None)):
            with mock.patch.object(console.urllib.request, "build_opener", build(sub)):
                self.assertEqual(console.openid_user("tok"), want, sub)
        with mock.patch.object(console.urllib.request, "build_opener", build(None, fail=True)):
            self.assertIsNone(console.openid_user("bogus"))

    def test_sessions(self):
        token = console.new_session(ANNA)
        self.assertEqual(console.session_of(SimpleNamespace(headers={"Authorization": f"Bearer {token}"})), (ANNA, None))
        self.assertIsNone(console.session_of(SimpleNamespace(headers={"Authorization": "Bearer nope"})))
        self.assertIsNone(console.session_of(SimpleNamespace(headers={})))


class Actions(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({"anna": {}, "betty": {}}))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        room = SimpleNamespace(users={ANNA: 0, BETTY: 0}, invited_users={}, display_name="Hello")
        self.calls = []

        async def change_level(rid, level):
            self.calls.append(("mode", level))

        async def set_follow(r, user, value, agent=None):
            self.calls.append(("follow", user, value))

        async def say(*a, **k):
            pass

        async def confirm(r, user):
            return False
        self.loop = SimpleNamespace(client=SimpleNamespace(rooms={"!r": room}), change_level=change_level, say=say,
                                    is_person=lambda r, u: u in r.users and not u.split(":")[0].endswith("-agent"),
                                    set_follow=set_follow, confirm=confirm, pending={},
                                    level=lambda rid: asyncio.sleep(0, "off"),
                                    agent_online=lambda agent: True if agent == "alice-at-matrix-org-a5829e99c7-agent" else None,
                                    ever_connected=lambda agent: agent == "alice-at-matrix-org-a5829e99c7-agent",
                                    user_id="@pm:" + BETTY.partition(":")[2])
        for name, fn in (("is_moderator", lambda c, r, u: asyncio.sleep(0, u == ANNA)),
                         ("repo_of", lambda c, rid: asyncio.sleep(0, "your-name/hagency-hello")),
                         ("discussion_of", lambda c, rid: asyncio.sleep(0, False))):
            p2 = mock.patch.object(room_state, name, fn)
            p2.start()
            self.addCleanup(p2.stop)
        v = mock.patch("server.project_rooms.visibility", lambda c, rid: asyncio.sleep(0, "private"))   # pm_projects.py
        v.start()
        self.addCleanup(v.stop)

    def act(self, user, action, value):
        return asyncio.run(console.act(self.loop, "!r", user, action, value))

    def test_go_back_to_the_official_version(self):
        """bridge_custom.py: works from the console even when someone's own version is broken."""
        with mock.patch.object(console.customize, "go_back", return_value=True) as back:
            self.assertEqual(self.act(BETTY, "goback", ""), "ok")
        back.assert_called_once_with("betty")

    def test_someone_from_another_server_cant_send_anyone_back(self):
        """GPT review 17: a remote @betty must never map to this server's betty."""
        remote = "@betty:elsewhere.example"
        self.loop.client.rooms["!r"].users[remote] = 0
        with mock.patch.object(console.customize, "go_back") as back:
            self.assertIn("only agents this server runs", self.act(remote, "goback", ""))
        back.assert_not_called()

    def test_owner_changes_the_mode_contributor_cant(self):
        self.assertEqual(self.act(ANNA, "mode", "autonomous"), "ok")
        self.assertIn("only the room owner", self.act(BETTY, "mode", "autonomous"))
        self.assertEqual(self.act(BETTY, "mode", "off"), "ok")         # anyone may stop
        self.assertEqual(self.calls, [("mode", "autonomous"), ("mode", "off")])

    def test_everyone_sets_their_own_agent(self):
        self.assertEqual(self.act(BETTY, "follow", "never"), "ok")
        self.assertEqual(self.calls, [("follow", BETTY, "never")])

    def test_the_retired_web_switch_is_gone(self):
        """ADR 0006: the owner's agent always has the web when they ask it; no per-room toggle."""
        self.assertEqual(self.act(BETTY, "web", "on"), "unknown action")
        self.assertEqual(self.calls, [])
        self.assertNotIn("web", asyncio.run(console.view(self.loop, "!r", BETTY)))

    def test_people_outside_the_room_and_odd_values_do_nothing(self):
        self.assertIn("not in that room", self.act(MALLORY, "mode", "off"))
        self.assertEqual(self.act(ANNA, "mode", "turbo"), "unknown action")
        self.assertEqual(self.calls, [])

    def test_approve_only_what_you_saw(self):
        self.loop.pending["!r"] = {"kind": "plan", "event_id": "$now", "by": ANNA}
        self.assertIn("no longer what's waiting", self.act(ANNA, "approve", "$earlier"))

    def test_agents_cant_use_the_console(self):
        self.loop.client.rooms["!r"].users[f"@anna-agent:{S}"] = 0
        self.assertIn("not in that room", self.act(f"@anna-agent:{S}", "mode", "off"))

    def test_view_shows_my_role_and_my_agent(self):
        v = asyncio.run(console.view(self.loop, "!r", BETTY))
        self.assertEqual((v["user"], v["moderator"], v["agent"], v["follow"]),
                         ("betty", False, "betty-agent", "always"))

    def test_add_an_agent_waits_until_the_first_one_connected(self):
        """GPT review: the chat offers ➕ only once an agent has connected; the panel must agree."""
        registry.PATH.write_text(json.dumps({"betty": {"agent": "remote"}}))
        never = [{"name": "betty-agent", "remote": True, "connected": False}]
        self.assertFalse(console.can_add("betty", never))
        self.assertTrue(console.can_add("betty", [{**never[0], "connected": True}]))

    def test_someone_from_another_server_sees_their_own_agent_never_a_namesakes(self):
        """GPT review 23: people from other servers sign in with a link and see their own agent."""
        alice, namesake = "@alice:matrix.org", "@betty:elsewhere.example"
        registry.PATH.write_text(json.dumps({"betty": {}, alice: {"agent": "remote", "follow": "ask"}}))
        self.loop.client.rooms["!r"].users.update({alice: 0, namesake: 0})
        v = asyncio.run(console.view(self.loop, "!r", alice))
        self.assertEqual((v["agent"], v["follow"]), (registry.agent_of(alice), "ask"))
        self.assertTrue(v["agent"].startswith("alice-at-matrix-org-"))
        self.assertEqual((v["remote"], v["online"]), (True, True))             # "My agent": Online
        self.assertEqual((v["agents"][0]["connected"], v["agents"][0]["invited"]), (True, False))   # My agents' card
        self.assertIsNone(asyncio.run(console.view(self.loop, "!r", namesake))["agent"])


if __name__ == "__main__":
    unittest.main()
