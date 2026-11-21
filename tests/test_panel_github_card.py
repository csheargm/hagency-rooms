"""The panel's GitHub card as a person uses it, the page's own scripts run in node (panel.html and
static/panel_github.js): after Connect GitHub the line "Your code is on the GitHub card…" goes once the
code has (screen test: it stayed after the link connected), and the code has a Copy button.
Run: .venv/bin/python -m unittest tests.test_panel_github_card"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest

from server import panel
from server import pm_github

NODE = shutil.which("node")
# The page runs signed in (a session from before), against a server that answers each view in turn.
HARNESS = r"""
const vm = require("vm");
const [page, card, views, result] = [process.argv[2], process.argv[3], JSON.parse(process.argv[4]), process.argv[5]];
const els = {};
const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: ""});
let shown = 0;
const ctx = {
  console, URLSearchParams, JSON, String, Promise, Date, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: "?room=%21r", hash: "", pathname: "/panel"}, history: {replaceState() {}},
  document: {getElementById: el, addEventListener() {}, activeElement: null},
  sessionStorage: {getItem: () => "s", setItem() {}, removeItem() {}},
  setTimeout: () => 0, clearTimeout() {}, setInterval() {},
  fetch: async (path) => path.startsWith("/console/action") ? {status: 200, json: async () => ({result})}
    : {status: 200, json: async () => views[Math.min(shown++, views.length - 1)]},
};
ctx.window = {parent: null, addEventListener() {}};
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  vm.createContext(ctx); vm.runInContext(card, ctx); vm.runInContext(page, ctx); await flush();
  await ctx.act("github", "connect"); await flush();
  const lines = [els.msg.textContent];
  for (let i = 2; i < views.length; i++) { await ctx.refresh(); await flush(); lines.push(els.msg.textContent); }
  process.stdout.write(JSON.stringify(lines));
})();
"""
BASE = {"room": "Our project", "repo": None, "user": "anna", "moderator": True, "mode": "off", "waiting": None, "html": ""}
NONE = {**BASE, "github": {"login": None, "code": None, "url": None}}
CODE = {**BASE, "github": {"login": None, "code": "AEA3-F84A", "url": "https://github.com/login/device"}}
LINKED = {**BASE, "github": {"login": "anna-gh", "code": None, "url": None}}


def card_js(call: str) -> str:
    esc = re.search(r"^const esc = .*$", panel.PAGE, re.M).group(0)
    done = subprocess.run([NODE, "-e", f"{esc}\n{panel.SCRIPTS['panel-github.js'].read_text()}\n{call}"],
                          capture_output=True, text=True, timeout=20)
    if done.returncode:
        raise AssertionError(done.stderr[-500:])
    return done.stdout


@unittest.skipIf(NODE is None, "node not installed")
class Banner(unittest.TestCase):
    def lines(self, views: list, result: str = pm_github.ON_CARD) -> list:
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", json.dumps(["https://e"])), re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h:
            h.write(HARNESS)
        done = subprocess.run([NODE, h.name, page, panel.SCRIPTS["panel-github.js"].read_text(), json.dumps(views), result],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout)

    def test_the_line_goes_once_the_link_is_connected(self):
        # views: at start, after the click (the code shows), then each refresh
        self.assertEqual(self.lines([NONE, CODE, CODE, LINKED, LINKED]), [pm_github.ON_CARD, pm_github.ON_CARD, "", ""])

    def test_the_line_goes_when_the_sign_in_expires_or_is_cancelled(self):
        self.assertEqual(self.lines([NONE, CODE, NONE]), [pm_github.ON_CARD, ""])

    def test_connected_before_any_view_showed_the_code_clears_it_too(self):
        """GPT review: a sign-in finished before the first poll saw its code left the line for good."""
        self.assertEqual(self.lines([NONE, LINKED, LINKED]), ["", ""])

    def test_a_refusal_stays_readable(self):
        """No code ever came (a rate limit): that answer is never wiped as if it were stale."""
        limited = "You've asked for several GitHub codes this hour; please try again later."
        self.assertEqual(self.lines([NONE, NONE, NONE], result=limited), [limited, limited])


@unittest.skipIf(NODE is None, "node not installed")
class CopyCode(unittest.TestCase):
    def test_the_code_has_a_copy_button_and_the_link_opens_a_new_tab(self):
        html = card_js(f"process.stdout.write(githubCard({json.dumps(CODE)}))")
        self.assertIn('data-copy="AEA3-F84A">Copy</button>', html)
        self.assertIn('value="AEA3-F84A"', html)
        self.assertRegex(html, r'<a href="https://github.com/login/device" target="_blank" rel="noopener[^"]*">')

    def test_copy_says_copied_and_survives_a_redraw(self):
        out = card_js("""(async () => { const b = {dataset: {copy: "AEA3-F84A"}, textContent: "Copy"}; let got = "";
          await copyCode(b, null, {writeText: async t => { got = t; }});
          process.stdout.write([got, b.textContent, githubCard(%s).includes("Copied ✓")].join("|")); })()""" % json.dumps(CODE))
        self.assertEqual(out, "AEA3-F84A|Copied ✓|true")

    def test_a_refused_clipboard_selects_the_code_instead(self):
        out = card_js("""(async () => { const b = {dataset: {copy: "X"}, textContent: "Copy"}; const did = [];
          const field = {focus: () => did.push("focus"), select: () => did.push("select")};
          await copyCode(b, field, {writeText: async () => { throw new Error("NotAllowedError"); }});
          process.stdout.write(did.join(",") + "|" + b.textContent); })()""")
        self.assertEqual(out, "focus,select|Selected: press Ctrl+C")


if __name__ == "__main__":
    unittest.main()
