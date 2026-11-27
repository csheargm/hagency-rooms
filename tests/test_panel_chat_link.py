"""Owner request: every "your chat with Product Manager" in the panel is a link that opens that chat.
Inside Element: the widget API's navigate (MSC2931, pre-approved for our panel), with a matrix.to link
in a new tab when Element doesn't allow it or refuses. The page's own scripts run in node against a
stand-in for Element. Run: .venv/bin/python -m unittest tests.test_panel_chat_link"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import unittest

from server import console
from server import panel

NODE = shutil.which("node")
ELEMENT = "https://element.example"
HARNESS = r"""
const vm = require("vm");
const [page, cards, scenario, element] = process.argv.slice(2);
const els = {}, posted = [], opened = [];
let clicker = null, listener = null;
const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: ""});
const view = {room: "P", repo: null, user: "anna", moderator: false, mode: "off", waiting: null, html: "",
  agents: [{name: "anna-agent", label: "main", first: true, follow: "always", remote: true, online: false,
            connected: false, here: false, invited: false}], can_add: false, pm_chat: "!dm:example"};
const ctx = {
  console, URLSearchParams, JSON, String, Promise, Date, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: "?room=%21r&widgetId=hagency", hash: "", pathname: "/panel"}, history: {replaceState() {}},
  document: {getElementById: el, addEventListener: (t, f) => { (clicker = clicker || []).push(f); },
             activeElement: null},
  sessionStorage: {getItem: () => "s", setItem() {}, removeItem() {}},
  setTimeout: () => 0, clearTimeout() {}, setInterval() {},
  fetch: async () => ({status: 200, json: async () => view}),
};
Object.assign(ctx, {parent: {postMessage: m => posted.push(m)}, addEventListener: (t, f) => { listener = f; },
              open: (...a) => opened.push(a)});
ctx.window = ctx;                                   // the page's functions are the window's, as in a browser
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const send = async data => { listener({origin: element, source: ctx.parent, data: {widgetId: "hagency", ...data}}); await flush(); };
(async () => {
  vm.createContext(ctx); vm.runInContext(cards, ctx); vm.runInContext(page, ctx); await flush();
  await send({api: "toWidget", requestId: "c1", action: "capabilities", data: {}});
  const asked = posted.find(m => m.requestId === "c1").response.capabilities;
  if (scenario !== "not_approved")
    await send({api: "toWidget", requestId: "c2", action: "notify_capabilities", data: {requested: asked, approved: asked}});
  const html = (els.controls.innerHTML + el("agents").innerHTML);
  const href = (html.match(/<a href="([^"]+)"[^>]*data-pmchat/) || [])[1];
  let prevented = false;
  const a = {href, closest: sel => sel.includes("pmchat") ? a : null,
             dataset: scenario === "refused_trial" ? {label: "open the Trial agent chat"} : {}};   // openChat's link
  clicker.forEach(f => f({target: a, preventDefault: () => { prevented = true; }}));
  const nav = posted.find(m => m.action === "org.matrix.msc2931.navigate");
  if (scenario.startsWith("refused")) await send({api: "fromWidget", requestId: nav.requestId, action: nav.action, response: {error: {message: "no"}}});
  process.stdout.write(JSON.stringify({asked, href, prevented, nav: nav ? nav.data.uri : null, opened, msg: el("msg").innerHTML}));
})();
"""


@unittest.skipIf(NODE is None, "node not installed")
class ChatLink(unittest.TestCase):
    def run_page(self, scenario: str) -> dict:
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", json.dumps([ELEMENT])), re.S).group(1)
        cards = "\n".join(path.read_text() for path in panel.SCRIPTS.values())
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h:
            h.write(HARNESS)
        done = subprocess.run([NODE, h.name, page, cards, scenario, ELEMENT], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout)

    def test_inside_element_the_link_opens_the_chat_in_place(self):
        got = self.run_page("approved")
        self.assertEqual(got["asked"], ["org.matrix.msc2931.navigate"])        # the one capability it asks for
        self.assertEqual(got["href"], "https://matrix.to/#/!dm%3Aexample")
        self.assertTrue(got["prevented"])
        self.assertEqual(got["nav"], got["href"])
        self.assertEqual(got["opened"], [])

    def test_without_the_capability_the_link_opens_a_new_tab(self):
        got = self.run_page("not_approved")
        self.assertFalse(got["prevented"])                                       # the link's own target=_blank
        self.assertIsNone(got["nav"])

    def test_a_refused_navigation_offers_a_link_to_click(self):
        """GPT re-check: a tab opened after the reply, not by the click itself, is blocked as a pop-up."""
        got = self.run_page("refused")
        self.assertEqual(got["opened"], [])
        self.assertIn('<a href="https://matrix.to/#/!dm%3Aexample" target="_blank"', got["msg"])
        self.assertIn("open your chat with Product Manager</a>", got["msg"])

    def test_a_refused_trial_chat_link_keeps_its_own_words(self):
        """GPT review: the fallback for the Trial agent chat's link said it opens Product Manager."""
        got = self.run_page("refused_trial")
        self.assertIn("open the Trial agent chat</a>", got["msg"])
        self.assertNotIn("Product Manager", got["msg"])


@unittest.skipIf(NODE is None, "node not installed")
class Cards(unittest.TestCase):
    def render(self, script: str, call: str, chat: str | None) -> str:
        page = panel.PAGE
        helpers = [re.search(rf"^const {n} = .*$", page, re.M).group(0) for n in ("esc", "cap")]
        link = re.search(r"^function pmChatLink\(.*?^}$", page, re.S | re.M).group(0)
        source = "\n".join([*helpers, f"let pmChat = {json.dumps(chat)};", link, panel.SCRIPTS[script].read_text(),
                            f"process.stdout.write({call});"])
        done = subprocess.run([NODE, "-e", source], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return done.stdout

    def test_every_mention_of_the_chat_is_a_link(self):
        never = {"name": "a-agent", "label": "main", "first": True, "follow": "always", "remote": True,
                 "online": False, "connected": False}
        agents = self.render("panel-agents.js", f"agentCards({json.dumps({'agents': [never], 'can_add': True})})", "!dm:x")
        self.assertIn('comes in <a href="https://matrix.to/#/!dm%3Ax"', agents)
        self.assertIn('Connect page in <a href="https://matrix.to/#/!dm%3Ax"', agents)
        github = self.render("panel-github.js", "githubCard({github: {code: 'AB-1', url: 'https://github.com/login/device'}})", "!dm:x")
        self.assertIn('data-pmchat="1">your chat with Product Manager</a>', github)

    def test_inside_my_hagency_it_says_here_and_links_nowhere(self):
        """Screen test 4: in @pm's chat itself the hints said "open your chat with Product Manager"."""
        never = {"name": "a-agent", "label": "main", "first": True, "follow": "always", "remote": True,
                 "online": False, "connected": False}
        codex = {**never, "name": "a-codex-agent", "label": "codex", "first": False}
        state = {"agents": [never, codex], "can_add": True, "personal": True}
        agents = self.render("panel-agents.js", f"agentCards({json.dumps(state)})", "!dm:x")
        self.assertEqual(agents.count("its Connect page comes here, in this chat"), 2)
        self.assertIn("Connect page here, in this chat", agents)
        self.assertNotIn("your chat with Product Manager", agents)
        self.assertNotIn("data-pmchat", agents)
        github = self.render("panel-github.js", "githubCard({personal: true, github: {code: 'AB-1', url: 'u'}})", "!dm:x")
        self.assertIn("also in\n    this chat.", github)
        self.assertNotIn("data-pmchat", github)

    def test_no_chat_yet_reads_as_plain_words(self):
        github = self.render("panel-github.js", "githubCard({github: {code: 'AB-1', url: 'u'}})", None)
        self.assertIn("also in\n    your chat with Product Manager.", github)
        self.assertNotIn("data-pmchat", github)


class OnlyTheirOwn(unittest.TestCase):
    def test_the_view_carries_only_the_signed_in_persons_chat(self):
        from tests.test_console import Actions
        case = Actions("test_view_shows_my_role_and_my_agent")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.loop.dm_rooms = {"@betty:bridge.localhost": "!betty-dm", "@anna:bridge.localhost": "!anna-dm"}
        view = asyncio.run(console.view(case.loop, "!r", "@betty:bridge.localhost"))
        self.assertEqual(view["pm_chat"], "!betty-dm")
        self.assertNotIn("!anna-dm", json.dumps(view))


if __name__ == "__main__":
    unittest.main()
