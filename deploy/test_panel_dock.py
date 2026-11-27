"""The "Hagency panel" button Element shows when our panel is minimised (element_page/panel-dock.js, ADR 0026).
Owner: once minimised, the panel left no trace above the chat, and Element's way back (Room info → Extensions →
the pin) wasn't found. The flow test runs the real script in headless Chrome against a stand-in for Element's
own stores (the same calls Element v1.12.35 makes for its pin, checked live), and clicks it as a person would.
Run: .venv/bin/python -m unittest discover -s deploy -p test_panel_dock.py"""
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import element_home
import health

HERE = Path(__file__).parent
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("HAGENCY_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
          or shutil.which("chromium-browser") or (MAC_CHROME if Path(MAC_CHROME).exists() else None))

# Element's stores, as panel-dock.js uses them (WidgetLayoutStore: getContainerWidgets, canAddToContainer,
# moveToContainer; RoomViewStore.getRoomId; the client's getRoom). SCENARIO picks the room's state.
FAKE = r"""<!doctype html><meta charset="utf-8"><div class="mx_RoomView"><div class="mx_RoomHeader"
style="height:40px;width:600px"></div><div class="mx_PinnedMessageBanner" style="height:30px"></div></div>
<pre id="result"></pre><script>
const SCENARIO = __SCENARIO__, moves = [], errors = [];
window.addEventListener("error", e => errors.push(String(e.message)));
const room = {roomId: "!r"};
const mine = {id: "hagency", url: location.origin + "/panel?room=$matrix_room_id"};
const theirs = {id: "hagency", url: "https://evil.example/panel?room=$matrix_room_id"};
let right = SCENARIO === "foreign" ? [theirs] : [mine];
if (SCENARIO !== "no_element") window.mxSdkContext = {client: {getRoom: id => id === "!r" ? room : null},
  roomViewStore: {getRoomId: () => "!r"},
  widgetLayoutStore: {getContainerWidgets: (r, c) => c === "right" ? right : [],
    canAddToContainer: () => SCENARIO !== "full",
    moveToContainer: (r, app, c) => { moves.push([r.roomId, app.id, c]); right = right.filter(w => w !== app); }}};
const b = () => document.getElementById("hagency-dock");
const seen = () => !!b() && !b().hidden;
setTimeout(() => {
  const out = {before: seen(), title: b() ? b().title : "", top: b() ? b().style.top : ""};
  if (b()) b().click();
  setTimeout(() => {
    Object.assign(out, {moves, after: seen(), errors});
    document.getElementById("result").textContent = JSON.stringify(out);
  }, 1500);
}, 1500);
</script><script src="panel-dock.js"></script>"""


class Wiring(unittest.TestCase):
    def test_element_loads_it_from_our_folder_and_deploy_checks_it(self):
        conf = (HERE / "element-nginx.conf").read_text()
        self.assertIn(f'<script src="{element_home.PREFIX}/{element_home.DOCK_JS}" defer></script></head>', conf)
        self.assertIn(element_home.DOCK_JS, element_home.STATIC)              # written out, and Caddy routes STATIC
        self.assertIn("moveToContainer", element_home.files({"ROOMS_DOMAIN": "r", "MATRIX_DOMAIN": "m",
                                                             "SERVER_NAME": "m"})[element_home.DOCK_JS])
        names = [n for n, _ in health.checks("https://r", "https://m", "https://r", "m", "m", public=True)]
        self.assertIn("Element loads the Hagency panel button (ADR 0026)", names)

    def test_it_only_ever_moves_our_own_panel_to_the_top(self):
        src = (element_home.SOURCE / element_home.DOCK_JS).read_text()
        self.assertEqual(re.findall(r'moveToContainer\([^)]*\)', src), ['moveToContainer(now.room, now.app, "top")'])
        self.assertIn('u.origin === location.origin && u.pathname === PANEL_PATH', src)
        self.assertNotIn("innerHTML", src)                                     # nothing from a room is drawn as HTML


@unittest.skipIf(CHROME is None, "Chrome not installed")
class InABrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        shutil.copy(element_home.SOURCE / element_home.DOCK_JS, cls.folder.name)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=cls.folder.name)
        handler.log_message = lambda *a: None
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.folder.cleanup()

    def run_page(self, scenario: str) -> dict:
        name = f"{scenario}.html"
        (Path(self.folder.name) / name).write_text(FAKE.replace("__SCENARIO__", json.dumps(scenario)))
        with tempfile.TemporaryDirectory() as profile:
            done = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                                   "--virtual-time-budget=6000", "--dump-dom",
                                   f"http://127.0.0.1:{self.server.server_port}/{name}"],
                                  capture_output=True, text=True, timeout=90)
        found = re.search(r'<pre id="result">(.*?)</pre>', done.stdout, re.S)
        if not found or not found.group(1):
            raise AssertionError("the page didn't finish: " + done.stderr[-800:])   # empty is a failure, not a pass
        return json.loads(found.group(1).replace("&quot;", '"').replace("&amp;", "&"))

    def test_a_minimised_panel_gets_a_button_that_puts_it_back(self):
        out = self.run_page("minimised")
        self.assertTrue(out["before"])
        self.assertEqual(out["title"], "Put the Hagency panel back above the chat (it was minimised).")
        self.assertEqual(out["top"], "84px")           # under the header and pinned banner: 8 + 40 + 30, then 6
        self.assertEqual(out["moves"], [["!r", "hagency", "top"]])
        self.assertFalse(out["after"])                                         # back on top: the button goes
        self.assertEqual(out["errors"], [])

    def test_another_page_under_our_widget_id_gets_no_button(self):
        out = self.run_page("foreign")
        self.assertEqual((out["before"], out["moves"], out["errors"]), (False, [], []))

    def test_without_elements_stores_nothing_shows_and_nothing_breaks(self):
        out = self.run_page("no_element")
        self.assertEqual((out["before"], out["moves"], out["errors"]), (False, [], []))

    def test_a_full_top_area_says_what_to_do_and_moves_nothing(self):
        out = self.run_page("full")
        self.assertTrue(out["before"])
        self.assertIn("top area is full", out["title"])
        self.assertEqual(out["moves"], [])


if __name__ == "__main__":
    unittest.main()
