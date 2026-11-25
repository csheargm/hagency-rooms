"""My Hagency: the Hagency panel at the top of a person's private chat with Product Manager, with only
their personal cards (console_personal.py). Owner request: the agents and GitHub settings in one place
that is theirs. Run: .venv/bin/python -m unittest tests.test_my_hagency"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import agent_kinds
from server import console
from server import console_personal
from server import panel
from server import pm_settings
from common import registry
from server.loop import Loop

S = "bridge.localhost"
PM, ANNA, BETTY = f"@pm:{S}", f"@anna:{S}", f"@betty:{S}"


def chat(rid, *people, safe=True):
    levels = SimpleNamespace(defaults=SimpleNamespace(invite=pm_settings.PM_ONLY if safe else 0))
    return SimpleNamespace(room_id=rid, users={PM: 0, **{p: 0 for p in people}}, invited_users={}, display_name="Product Manager", name="Product Manager",
                           history_visibility=pm_settings.HISTORY if safe else "shared", power_levels=levels, canonical_alias=None)


class MyHagency(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({ANNA: {"agent": "remote", "ready": True}}))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.dm_rooms, self.lp.pending, self.said = PM, {ANNA: "!dm"}, {}, []
        self.lp._welcomes, self.lp._direct_chats, self.lp._personal_chats = {}, {}, {}
        self.lp.agent_online, self.lp.ever_connected = (lambda a: True), (lambda a: True)
        self.lp.client = SimpleNamespace(rooms={"!dm": chat("!dm", ANNA), "!old": chat("!old", ANNA, safe=False)}, user_id=PM)

        async def say(rid, body, *a, **k):
            self.said.append((rid, body))
        self.lp.say = say

    def test_their_chat_with_pm_shows_only_their_own_cards(self):
        view = asyncio.run(console.view(self.lp, "!dm", ANNA))
        self.assertTrue(view["personal"])
        self.assertEqual(view["room"], console_personal.TITLE)
        self.assertEqual([a["name"] for a in view["agents"]], ["anna-agent"])
        for room_only in ("mode", "visibility", "runs_in"):
            self.assertNotIn(room_only, view)

    def test_only_personal_actions_there(self):
        self.assertEqual(asyncio.run(console.act(self.lp, "!dm", ANNA, "mode", "autonomous")), console_personal.ROOM_ONLY)
        self.assertEqual(asyncio.run(console.act(self.lp, "!dm", ANNA, "repo", "a/b")), console_personal.ROOM_ONLY)
        with mock.patch.object(Loop, "rename_agent", mock.AsyncMock(return_value="renamed")) as rename:
            self.assertEqual(asyncio.run(console.act(self.lp, "!dm", ANNA, "rename", "Helper", "anna-agent")), "renamed")
        rename.assert_awaited_once()

    def test_a_chat_that_stopped_being_private_is_refused_never_a_project_room(self):
        """GPT review: someone joined, and the panel there served project settings to them."""
        self.lp.client.rooms["!dm"].users[BETTY] = 0
        self.assertEqual(asyncio.run(console.act(self.lp, "!dm", BETTY, "mode", "off")), console_personal.NOT_PRIVATE)
        self.assertEqual(asyncio.run(console.view(self.lp, "!dm", BETTY)), {"error": console_personal.NOT_PRIVATE})

    def test_a_chat_pm_replaced_since_is_still_refused(self):
        """GPT re-check: @pm makes a newer chat when the old one stopped being safe; the old one's panel
        must not become a project panel then."""
        self.lp.boards, self.lp.board_file = {}, Path(tempfile.mkdtemp()) / "b.json"
        with mock.patch.object(panel, "add_to_room", mock.AsyncMock(return_value=True)), \
                mock.patch("common.room_state.is_moderator", mock.AsyncMock(return_value=True)):
            asyncio.run(self.lp.ensure_panel("!dm"))
        self.lp.dm_rooms[ANNA] = "!newer"
        self.lp.client.rooms["!dm"].users[BETTY] = 0
        self.assertEqual(asyncio.run(console.act(self.lp, "!dm", BETTY, "mode", "off")), console_personal.NOT_PRIVATE)

    def test_a_chat_with_anyone_else_in_it_is_never_personal(self):
        self.lp.client.rooms["!dm"].users[BETTY] = 0
        self.assertIsNone(console_personal.owner_of(self.lp, self.lp.client.rooms["!dm"]))
        self.assertFalse(console_personal.is_personal_chat(self.lp, self.lp.client.rooms["!old"]))   # an older, unsafe one

    def test_pm_puts_the_panel_on_top_of_that_chat_once(self):
        self.lp.boards, self.lp.board_file = {}, Path(tempfile.mkdtemp()) / "b.json"
        added = []

        async def add(client, rid):
            added.append(rid)
            return True
        with mock.patch.object(panel, "add_to_room", add), \
                mock.patch("common.room_state.is_moderator", mock.AsyncMock(return_value=True)):
            for rid in ("!dm", "!old", "!dm"):
                asyncio.run(self.lp.ensure_panel(rid))
        self.assertEqual(added, ["!dm"])                                     # never the older chat; once


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PersonalPage(unittest.TestCase):
    def test_the_page_shows_my_agents_and_github_only(self):
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", '["https://e"]'), re.S).group(1)
        cards = "\n".join(p.read_text() for p in panel.SCRIPTS.values())
        view = {"personal": True, "room": "My Hagency", "user": "anna", "can_add": True, "kinds": agent_kinds.kinds(),
                "agents": [{"name": "anna-agent", "label": "main", "first": True, "follow": "always", "remote": True, "online": True}],
                "github": {"login": None, "code": None, "url": None}, "html": ""}
        harness = """
const vm = require("vm"); const els = {}; const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: ""});
const ctx = {console, URLSearchParams, JSON, String, Promise, Date, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: "?room=%21dm", hash: "", pathname: "/panel"}, history: {replaceState() {}},
  document: {getElementById: el, addEventListener() {}, activeElement: null},
  sessionStorage: {getItem: () => "s", setItem() {}, removeItem() {}}, setTimeout: () => 0, clearTimeout() {}, setInterval() {},
  fetch: async () => ({status: 200, json: async () => (__VIEW__)})};
ctx.window = ctx; ctx.parent = ctx; ctx.addEventListener = () => {};   // a window has it (panel_cards.js)
vm.createContext(ctx); vm.runInContext(__CARDS__, ctx); vm.runInContext(__PAGE__, ctx);
setTimeout(() => {}, 0); setImmediate(() => setImmediate(() => setImmediate(() => process.stdout.write(els.title.textContent + "|" + els.controls.innerHTML))));
"""
        source = harness.replace("__VIEW__", json.dumps(view)).replace("__PAGE__", json.dumps(page)).replace("__CARDS__", json.dumps(cards))
        done = subprocess.run([shutil.which("node"), "-e", source], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        title, html = done.stdout.split("|", 1)
        self.assertEqual(title, "My Hagency")
        self.assertIn("My agents · 1", html)
        self.assertIn('>Claude Code</option>', html)     # the AI picker
        self.assertIn("<h2>GitHub</h2>", html)
        for room_card in ("<h2>Mode</h2>", "Project repo", "Who can join"):
            self.assertNotIn(room_card, html)


if __name__ == "__main__":
    unittest.main()
