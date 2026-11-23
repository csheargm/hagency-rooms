"""Screen test: once a room was open, nobody could get back to the welcome steps (Element's home page;
its Home icon keeps the room). The panel's "Welcome steps" link opens them in place inside our own
Element (same origin: a link clicked in Element's own document), else in a new tab; the Welcome room,
which has no panel, links them in its topic. The page's own script runs in node against a stand-in
for Element's window. Run: .venv/bin/python -m unittest tests.test_panel_home_link"""
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from common.bridge_config import ELEMENT_HOME, ELEMENT_ORIGIN
from server import panel
from server import pm_lobby

NODE = shutil.which("node")
HARNESS = r"""
const vm = require("vm");
const [page, scenario, modifier] = process.argv.slice(2);
const els = {}, made = [];
let clickers = [];
const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: ""});
const elementDoc = {                                   // Element's own document (same origin)
  createElement: tag => { const a = {tag, href: "", clicked: 0, removed: false, click() { this.clicked++; },
                                     remove() { this.removed = true; }}; made.push(a); return a; },
  body: {appendChild: a => { a.attached = true; }},
};
const top = scenario === "other_origin"
  ? {get document() { throw new Error("SecurityError: cross-origin frame"); }}
  : {document: elementDoc};
const ctx = {
  console, URLSearchParams, JSON, String, Promise, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: scenario === "not_embedded" ? "?room=%21r" : "?room=%21r&widgetId=hagency", hash: "", pathname: "/panel"},
  history: {replaceState() {}},
  document: {getElementById: el, addEventListener: (t, f) => clickers.push(f), activeElement: null},
  sessionStorage: {getItem: () => null, setItem() {}, removeItem() {}},
  setTimeout: () => 0, clearTimeout() {}, setInterval() {},
  fetch: async () => ({status: 200, json: async () => ({})}),
  addEventListener() {}, top,
};
ctx.parent = {postMessage() {}};
ctx.window = ctx;
vm.createContext(ctx); vm.runInContext(page, ctx);
let prevented = false;
const link = {closest: sel => sel.includes("data-home") ? link : null};
const ev = {target: link, button: 0, preventDefault: () => { prevented = true; }};
if (modifier) ev[modifier] = modifier === "button" ? 1 : true;
clickers.forEach(f => f(ev));
process.stdout.write(JSON.stringify({prevented, made}));
"""


class Link(unittest.TestCase):
    def test_the_panel_links_elements_home_page(self):
        tag = re.search(r'<a [^>]*data-home="1"[^>]*>([^<]*)</a>', panel.PAGE)
        self.assertIsNotNone(tag, "no Welcome steps link in the panel")
        self.assertIn(f'href="{html.escape(ELEMENT_HOME, quote=True)}"', tag.group(0))
        self.assertIn("Welcome steps", tag.group(1))
        self.assertNotIn("__ELEMENT_HOME__", panel.PAGE)

    def test_home_is_elements_own_home_page(self):
        self.assertEqual(ELEMENT_HOME, f"{ELEMENT_ORIGIN}/#/home")

    def test_the_welcome_topic_links_it_too(self):
        self.assertIn(ELEMENT_HOME, pm_lobby.WELCOME_TOPIC)
        self.assertTrue(pm_lobby.WELCOME_TOPIC.startswith("Start here 👉 open 'Product Manager' on the left"))


@unittest.skipIf(NODE is None, "node not installed")
class Click(unittest.TestCase):
    def click(self, scenario: str, modifier: str = "") -> dict:
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", "[]"), re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h:
            h.write(HARNESS)
        try:
            done = subprocess.run([NODE, h.name, page, scenario, modifier], capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(h.name)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout)

    def test_inside_our_element_it_opens_in_place(self):
        r = self.click("same_origin")
        self.assertTrue(r["prevented"], "the iframe's own new tab would open a second Element")
        self.assertEqual([(a["tag"], a["href"], a["attached"], a["clicked"], a["removed"]) for a in r["made"]],
                         [("a", "#/home", True, 1, True)])

    def test_a_click_asking_for_a_new_tab_gets_one(self):
        for modifier in ("ctrlKey", "metaKey", "shiftKey", "altKey", "button"):
            with self.subTest(modifier):
                r = self.click("same_origin", modifier)
                self.assertFalse(r["prevented"])
                self.assertEqual(r["made"], [])

    def test_inside_another_origins_element_the_link_opens_a_new_tab(self):
        r = self.click("other_origin")
        self.assertFalse(r["prevented"])
        self.assertEqual(r["made"], [])

    def test_outside_element_it_is_a_plain_link(self):
        r = self.click("not_embedded")
        self.assertFalse(r["prevented"])
        self.assertEqual(r["made"], [])


if __name__ == "__main__":
    unittest.main()
