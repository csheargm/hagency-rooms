"""Unit tests: the side panel. Run: .venv/bin/python -m unittest tests.test_panel"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import panel
from common import registry
from tests import ROOT

S = "bridge.localhost"


class Render(unittest.TestCase):
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({"anna": {}, "betty": {"follow": "ask"}}))
        p = mock.patch.object(registry, "PATH", path)
        p.start()
        self.addCleanup(p.stop)
        room = SimpleNamespace(users={f"@anna:{S}": 0, f"@anna-agent:{S}": 0, f"@betty-agent:{S}": 0}, invited_users={})
        self.loop = SimpleNamespace(client=SimpleNamespace(rooms={"!r": room}), room_repos={},
                                    boards={"!r": {"text": "📋 **Project status** — loop: **assist**\n\n**Now**\n\n- #7 building"}},
                                    pending={"!r": {"kind": "plan", "by": f"@anna:{S}", "until": time.time() + 60}},
                                    last_plans={"!r": {"items": [{"title": "<script>alert(1)</script>", "criteria": ["x"]}]}})

    def test_shows_status_waiting_plan_and_whose_agent_is_whose(self):
        page = panel.render(self.loop, "!r")
        self.assertIn("<strong>Now</strong>", page)
        self.assertNotIn("<strong>Project status</strong>", page)     # the board's title line repeats the header
        self.assertIn("a plan · anna clicks 👍", page)
        self.assertIn("<b>anna-agent</b> <span class=muted>· anna's agent</span>", page)
        self.assertIn("<span class=chip>comes along: always</span></div>", page)
        self.assertNotIn("uses:", page)                                # ADR 0006: no per-room web grant
        self.assertIn("<b>betty-agent</b> <span class=muted>· betty's agent <span class=muted>(owner not here: idle)", page)

    def test_an_agent_only_invited_is_marked_so(self):
        """Screen test: "Agents in this room" listed an agent that was only invited (never connected)
        as if it were there."""
        self.loop.client.rooms["!r"].invited_users = {f"@anna-agent:{S}": 0}
        page = panel.render(self.loop, "!r")
        self.assertIn("anna's agent <span class=chip>invited — not connected yet</span>", page)
        self.assertEqual(page.count("not connected yet"), 1)              # betty-agent has joined
        self.loop.agent_online = lambda agent: agent == "anna-agent"       # GPT review: online, its join pending
        self.assertIn("anna's agent <span class=chip>invited — joining</span>", panel.render(self.loop, "!r"))

    def test_owner_names_are_shown_as_written(self):
        """Seen live: the owner "cx" showed as "Cx". A display name is used as set, escaped."""
        room = self.loop.client.rooms["!r"]
        registry.PATH.write_text(json.dumps({"anna": {}, "betty": {"follow": "ask"}, "cx": {}}))
        room.users[f"@cx:{S}"], room.users[f"@cx-agent:{S}"] = 0, 0
        self.assertIn("· cx's agent", panel.render(self.loop, "!r"))
        room.users[f"@anna:{S}"] = SimpleNamespace(display_name="Anna <b>B</b>")
        self.assertIn("· Anna &lt;b&gt;B&lt;/b&gt;'s agent", panel.render(self.loop, "!r"))

    def test_everything_people_wrote_is_escaped(self):
        self.assertNotIn("<script>", panel.render(self.loop, "!r"))
        self.assertIn("&lt;script&gt;", panel.render(self.loop, "!r"))

    def test_a_stored_board_cannot_carry_html_and_issues_link(self):
        """GPT review 16: board markdown went to innerHTML with raw HTML allowed; #7 never linked."""
        self.loop.boards["!r"]["text"] = "**Now**\n\n- #7 building · <img src=x onerror=alert(1)>"
        self.loop.room_repos = {"!r": "your-name/hagency-hello"}
        page = panel.render(self.loop, "!r")
        self.assertNotIn("<img", page)
        self.assertIn('href="https://github.com/your-name/hagency-hello/issues/7"', page)

    def test_unknown_room(self):
        self.assertIn("isn't in this room", panel.render(self.loop, "!other"))


class PageScript(unittest.TestCase):
    """Seen live: an apostrophe in a button label ("it\\'s") lost its escape in the Python string and
    broke the whole console's script. Unit tests never ran the JavaScript; node checks it here."""
    def test_the_page_script_parses(self):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        page = panel.PAGE.replace("__EMBEDDERS__", json.dumps([panel.ELEMENT_ORIGIN]))
        script = re.search(r"<script>(.*)</script>", page, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(script)
        done = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])


class CardScripts(unittest.TestCase):
    """GPT review (batch 5+6): Caddy didn't route /panel-github.js to @pm, so Element's 404 page came
    back, githubCard was undefined and the whole panel (mode, repo, Remove) stopped rendering."""
    def test_every_card_script_the_page_loads_is_routed_to_pm_by_caddy(self):
        import re
        caddy = (ROOT / "deploy" / "Caddyfile").read_text()
        routed = re.search(r"@pm path ([^\n]+)", caddy).group(1).split()
        loaded = re.findall(r'<script src="([^"]+)"></script>', panel.PAGE)
        self.assertEqual(sorted(loaded), sorted(panel.SCRIPTS))
        for name in loaded:
            self.assertIn(f"/{name}", routed, f"{name} isn't in the Caddyfile's @pm route")
            self.assertTrue(panel.SCRIPTS[name].is_file())

    def test_a_card_whose_script_is_missing_degrades_and_the_rest_renders(self):
        import re
        import shutil
        import subprocess
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        card = re.search(r"^const card = .*?</section>`;$", panel.PAGE, re.S | re.M).group(0)
        script = ("const window = {projectCard: s => 'P' + s.n};\n" + card +
                  "\nprocess.stdout.write(card('projectCard', {n: 1}) + '|' + card('githubCard', {}));")
        done = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        shown, missing = done.stdout.split("|")
        self.assertEqual(shown, "P1")
        self.assertIn("didn't load", missing)


class Widget(unittest.TestCase):
    def test_pinned_at_the_top_where_people_see_it(self):
        import asyncio
        from nio import RoomPutStateResponse
        calls = []

        async def put(room_id, kind, content, state_key=""):
            calls.append((kind, content, state_key))
            return RoomPutStateResponse("$e", room_id)
        self.assertTrue(asyncio.run(panel.add_to_room(SimpleNamespace(room_put_state=put), "!r")))
        (kind, content, key), (layout_kind, layout, _) = calls
        self.assertEqual((kind, key, content["url"]), ("im.vector.modular.widgets", "hagency", panel.widget_url()))
        self.assertEqual(panel.widget_url(), "http://localhost:8650/panel?room=$matrix_room_id")
        with mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": "https://rooms.example.org"}):
            self.assertEqual(panel.widget_url(), "https://rooms.example.org/panel?room=$matrix_room_id")
        self.assertEqual(layout["widgets"]["hagency"]["container"], "top")

    def test_element_pre_approves_exactly_the_url_pm_adds(self):
        """deploy/element_home.py trusts one widget URL: it must be the one @pm puts in each room."""
        import sys
        sys.path.insert(0, str(ROOT / "deploy"))
        self.addCleanup(sys.path.remove, sys.path[0])
        import element_home
        with mock.patch.dict("os.environ", {"HAGENCY_PUBLIC_CONSOLE": "https://rooms.example.org"}):
            self.assertEqual(panel.widget_url(), element_home.panel_url({"ROOMS_DOMAIN": "rooms.example.org"}))


if __name__ == "__main__":
    unittest.main()
