"""The panel's tabs (Room · My agents · Cards) and its one-line agents (owner request: the panel docked above
the chat was one long page). Unit tests run the page's scripts in node; the flow test clicks through the real
page in headless Chrome, as a person does: tabs, keyboard, a remembered tab, one agent open at a time, and
"+ Add agent". Run: .venv/bin/python -m unittest tests.test_panel_tabs"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from server import panel

NODE = shutil.which("node")
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME = (os.environ.get("HAGENCY_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
          or shutil.which("chromium-browser") or (MAC_CHROME if Path(MAC_CHROME).exists() else None))
AGENTS_JS = panel.SCRIPTS["panel-agents.js"].read_text()
TABS_JS = panel.SCRIPTS["panel-tabs.js"].read_text()
ESC = "const esc = t => String(t).replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';');" \
      "const cap = t => t.charAt(0).toUpperCase() + t.slice(1);"


def agent(name, **more):
    return {"name": name, "label": name, "first": False, "follow": "ask", "remote": True, "online": True,
            "connected": True, "here": True, "invited": False, "runs": "Claude Code", "code": "current",
            "code_auto": True, "code_name": "v1.2.0 · abc1234", "code_version": "v1.2.0 · abc1234", "code_says": "",
            "gh": "signed_in", "gh_login": "octo", "auto_update": True, **more}


def node(src: str) -> str:
    done = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    if done.returncode:
        raise AssertionError(done.stderr[-800:])
    return done.stdout


@unittest.skipIf(NODE is None, "node not installed")
class Tabs(unittest.TestCase):
    def test_which_tabs_a_view_has(self):
        out = node(TABS_JS + "\nconst before = tabsOf({cards: {items: []}}); globalThis.showCards = () => {};"
                             "process.stdout.write(JSON.stringify([tabsOf(null), tabsOf({personal: true}),"
                             " tabsOf({cards: null}), tabsOf({cards: {items: []}}), before]));")
        self.assertEqual(json.loads(out), [[], ["agents"], ["room", "agents"], ["room", "agents", "cards"],
                                           ["room", "agents"]])          # GPT review: no Cards script, no blank tab

    def test_a_remembered_tab_that_isnt_there_falls_back_to_the_first(self):
        out = node(TABS_JS + "\nprocess.stdout.write([pickTab(['room', 'agents'], 'cards'), pickTab(['room', 'agents'],"
                             " 'agents'), pickTab([], 'room')].join('|'));")
        self.assertEqual(out, "room|agents|")

    def test_a_newcomer_without_an_agent_starts_on_my_agents(self):
        """GPT review: "Get my agent" was behind a tab nobody had opened yet; a choice made since still wins."""
        out = node(TABS_JS + "\nconst s = {first_pick: true}, tabs = ['room', 'agents'];"
                             "process.stdout.write([pickTab(tabs, '', s), pickTab(tabs, 'room', s), pickTab(tabs, '', {})].join('|'));")
        self.assertEqual(out, "agents|room|room")

    def test_a_chosen_tab_comes_back_when_its_tab_does(self):
        """GPT review: Cards gone for one view (Room shown) must not replace the choice of Cards."""
        fake_dom = ("const panes = ['room', 'agents', 'cards'].map(t => ({dataset: {tab: t}, hidden: false, setAttribute() {},"
                    " removeAttribute() {}})); globalThis.localStorage = {getItem: () => null, setItem() {}};"
                    "globalThis.showCards = () => {};"
                    "globalThis.document = {body: {classList: {toggle() {}}}, getElementById: () => ({}),"
                    " querySelectorAll: q => q === '[data-tab]' ? panes : []};")
        out = node(fake_dom + TABS_JS + "\nconst open = () => panes.find(p => !p.hidden).dataset.tab, seen = [];"
                   "tabsFor({cards: {}}); selectTab('cards'); seen.push(open());"
                   "tabsFor({cards: null}); seen.push(open()); tabsFor({cards: {}}); seen.push(open());"
                   "process.stdout.write(seen.join('|'));")
        self.assertEqual(out, "cards|room|cards")

    def test_blocked_storage_never_breaks_the_tabs(self):
        out = node("globalThis.localStorage = {getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }};"
                   + TABS_JS + "\nsaveTab('agents'); process.stdout.write('[' + savedTab() + ']');")
        self.assertEqual(out, "[]")


@unittest.skipIf(NODE is None, "node not installed")
class OneLineAgents(unittest.TestCase):
    def card(self, agents, **view) -> str:
        state = {"agents": agents, "can_add": True, "kinds": [], **view}
        return node(AGENTS_JS + "\n" + ESC + f"process.stdout.write(agentCards({json.dumps(state)}));")

    def test_each_agent_is_one_line_with_its_details_closed(self):
        html = self.card([agent("cx-claude", display="Night shift"), agent("cx-codex", online=False, runs="Codex")])
        lines = re.findall(r'<div class="aline" data-open="([^"]+)">(.*?)</div>', html, re.S)
        self.assertEqual([n for n, _ in lines], ["cx-claude", "cx-codex"])
        first, second = lines[0][1], lines[1][1]
        self.assertIn('<span class="dot on"></span><b>Night shift</b>', first)
        self.assertIn("Claude Code", first)
        self.assertIn("v1.2.0 · updates automatically", first)
        self.assertIn("GitHub ✓", first)
        self.assertIn('<span class="dot off"></span>', second)
        self.assertIn("Offline", second)
        self.assertEqual(html.count('class="adet"'), 2)
        self.assertEqual(len(re.findall(r'class="adet" id="[^"]+" hidden>', html)), 2)       # both closed
        for button in ("rename", "stop", "disconnect", "remove", "follow", "github_signin"):  # all still there, inside
            self.assertIn(f'data-a="{button}"', html, button)

    def test_one_waiting_to_connect_opens_by_itself(self):
        html = self.card([agent("cx-claude"), agent("cx-new", online=False, connected=False, gh=None)])
        self.assertRegex(html, r'class="adet" id="adet-cx-claude" hidden>')
        self.assertRegex(html, r'class="adet" id="adet-cx-new">')
        self.assertIn('<span class="dot new"></span>', html)
        self.assertIn("Not connected yet", html)

    def test_update_states_show_on_the_line(self):
        updating = self.card([agent("a", code="behind", code_updating=True)])
        self.assertIn("Updating…", re.search(r'<div class="aline".*?</div>', updating, re.S).group(0))
        retry = self.card([agent("a", code="behind", code_retry=True)])
        self.assertRegex(re.search(r'<div class="aline".*?</div>', retry, re.S).group(0), r'data-a="selfupdate"[^>]*>Retry update')
        signin = self.card([agent("a", gh="none")])
        self.assertIn('data-a="github_signin"', re.search(r'<div class="aline".*?</div>', signin, re.S).group(0))

    def test_add_agent_is_a_button_until_clicked(self):
        html = self.card([agent("a")])
        self.assertIn('<button data-addtoggle="1" aria-expanded="false" aria-controls="addform">+ Add agent</button>', html)
        self.assertRegex(html, r'<div class="set" id="addform" hidden>')
        self.assertIn('id="newagent"', html)                                                # drawn, hidden

    def test_auto_update_stays_visible_and_small(self):
        html = self.card([agent("a")])
        self.assertIn('<div class="set small" title="On: your agents update themselves', html)
        self.assertIn('data-a="autoupdate" data-v="on"', html)


FLOW = r"""
<script>
const VIEW = __VIEW__;
window.fetch = async url => ({status: 200, json: async () => String(url).startsWith("console/state") ? VIEW : {}});
try { sessionStorage.setItem("hagency-session", "s"); } catch (e) { /* the page then signs in: the test fails */ }
const wait = ms => new Promise(r => setTimeout(r, ms));
const shown = id => { const e = document.getElementById(id); return !!e && !e.closest("[hidden]"); };
const sel = () => [...document.querySelectorAll("[role=tab]")].filter(b => b.getAttribute("aria-selected") === "true").map(b => b.textContent);
async function flow() {
  const out = {first: sel(), tabs: [...document.querySelectorAll("[role=tab]")].map(b => b.textContent)};
  out.roomFirst = [shown("controls"), shown("agents"), shown("cards")];
  const pane = document.getElementById("pane-room");
  out.roles = [pane.getAttribute("role"), pane.getAttribute("aria-labelledby"), !!document.getElementById("tab-room")];
  document.querySelector("[data-tab-btn=agents]").click(); await wait(50);
  out.agentsTab = [shown("controls"), shown("agents"), shown("cards"), localStorage.getItem("hagency-panel-tab")];
  out.closed = [shown("adet-cx-claude"), shown("adet-cx-codex"), shown("addform"), shown("rename-cx-claude"), shown("rename-cx-codex")];
  document.querySelector('.aline[data-open="cx-claude"] .muted').click(); await wait(50);
  out.one = [shown("adet-cx-claude"), shown("adet-cx-codex"), shown("rename-cx-claude"), shown("rename-cx-codex")];
  document.querySelector('.ahead[data-open="cx-codex"]').click(); await wait(50);
  out.other = [shown("adet-cx-claude"), shown("adet-cx-codex")];
  await wait(3500);                                     // a redraw (REFRESH_MS) keeps what is open
  out.afterRedraw = [shown("adet-cx-claude"), shown("adet-cx-codex"), document.querySelector('.ahead[data-open="cx-codex"]').getAttribute("aria-expanded")];
  document.querySelector("[data-addtoggle]").click(); await wait(50);
  out.add = [shown("addform"), document.activeElement && document.activeElement.id];
  const tab = document.querySelector("[data-tab-btn=agents]"); tab.focus();
  tab.dispatchEvent(new KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true})); await wait(50);
  out.arrow = [sel(), document.activeElement.dataset.tabBtn, shown("cards")];
  document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {key: "Home", bubbles: true})); await wait(50);
  out.home = sel();
  document.querySelector("[data-tab-btn=agents]").click();
  document.getElementById("result").textContent = JSON.stringify(out);
}
window.addEventListener("load", () => setTimeout(flow, 500));
</script><pre id="result"></pre>"""
VIEW = {"room": "Demo", "repo": "a/b", "user": "anna", "moderator": True, "mode": "assist", "waiting": None,
        "html": "<p>Project status</p>", "visibility": "listed", "can_add": True, "kinds": [],
        "agents": [agent("cx-claude"), agent("cx-codex", runs="Codex", gh="none")],
        "cards": {"items": [], "max": 3, "data": {}, "agent_here": True}}


# My Hagency for someone with no agent of their own: the shared Trial agent card (pm_trial.trial_view) sits in My agents.
TRIAL_FLOW = r"""
<script>
const VIEW = __VIEW__, sent = [];
window.fetch = async (url, o) => { if (o && o.body) sent.push(JSON.parse(o.body).action);
  return {status: 200, json: async () => String(url).startsWith("console/state") ? VIEW : {result: "ok"}}; };
try { sessionStorage.setItem("hagency-session", "s"); } catch (e) { /* the page then signs in: the test fails */ }
const shown = el => !!el && !el.closest("[hidden]");
window.addEventListener("load", () => setTimeout(async () => {
  const pane = document.getElementById("agents"), h2 = [...pane.querySelectorAll("h2")].map(h => h.textContent);
  const out = {bar: shown(document.getElementById("tabs")), agents: shown(pane), room: shown(document.getElementById("controls")), h2};
  const go = [...pane.querySelectorAll('button[data-a="trial"]')].find(b => b.textContent === "Try it");
  out.tryIt = shown(go); if (go) go.click(); await new Promise(r => setTimeout(r, 300));
  out.sent = sent;
  document.getElementById("result").textContent = JSON.stringify(out);
}, 500));
</script><pre id="result"></pre>"""
TRIAL_VIEW = {"room": "My Hagency", "personal": True, "user": "newbie", "html": "", "agents": [], "can_add": False,
              "first_pick": True, "kinds": [], "github": {"login": None, "code": None},
              "trial": {"on": False, "left": 20, "per_day": 20, "model": "glm-5.3", "models": ["glm-5.3"]}}


def page_file(view: dict, folder: str, flow: str = FLOW) -> str:
    """The real panel page with its scripts inlined, a fake server (fetch) and the person's clicks (FLOW)."""
    html = panel.PAGE.replace("__EMBEDDERS__", "[]").replace('<script src="/auth/signin.js"></script>', "")
    for name, path in panel.SCRIPTS.items():
        html = html.replace(f'<script src="{name}"></script>', f"<script>{path.read_text()}</script>")
    html = html.replace("<title>", flow.replace("__VIEW__", json.dumps(view)) + "<title>", 1)
    path = Path(folder) / "panel.html"
    path.write_text(html)
    return path.as_uri() + "?room=%21r"


def chrome(url: str, profile: str) -> dict:
    done = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                           "--allow-file-access-from-files", "--virtual-time-budget=8000", "--dump-dom", url],
                          capture_output=True, text=True, timeout=90)
    found = re.search(r'<pre id="result">(.*?)</pre>', done.stdout, re.S)
    if not found or not found.group(1):
        raise AssertionError("the flow didn't finish: " + done.stderr[-800:])   # empty is a failure, never a pass
    return json.loads(found.group(1).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))


@unittest.skipIf(CHROME is None, "Chrome not installed")
class TabsInABrowser(unittest.TestCase):
    def test_a_person_clicks_through_tabs_agents_and_add(self):
        with tempfile.TemporaryDirectory() as folder:
            url, profile = page_file(VIEW, folder), str(Path(folder) / "profile")
            out = chrome(url, profile)
            self.assertEqual(out["tabs"], ["Room", "My agents", "Cards"])
            self.assertEqual(out["first"], ["Room"])
            self.assertEqual(out["roomFirst"], [True, False, False])
            self.assertEqual(out["roles"], ["tabpanel", "tab-room", True])        # labelled by a tab that exists
            self.assertEqual(out["agentsTab"], [False, True, False, "agents"])     # remembered
            self.assertEqual(out["closed"], [False] * 5)          # its fields too (a <li> once escaped it)
            self.assertEqual(out["one"], [True, False, True, False])
            self.assertEqual(out["other"], [False, True])                           # one open at a time
            self.assertEqual(out["afterRedraw"], [False, True, "true"])
            self.assertEqual(out["add"], [True, "newagent"])
            self.assertEqual(out["arrow"], [["Cards"], "cards", True])
            self.assertEqual(out["home"], ["Room"])
            self.assertEqual(chrome(url, profile)["first"], ["My agents"])          # the next visit opens it again

    def test_my_hagency_without_an_agent_shows_the_trial_card_in_my_agents(self):
        """Merge with the trial agent (PR #73): its card is in My agents, after the agent picker, before GitHub."""
        with tempfile.TemporaryDirectory() as folder:
            out = chrome(page_file(TRIAL_VIEW, folder, TRIAL_FLOW), str(Path(folder) / "profile"))
        self.assertEqual((out["bar"], out["agents"], out["room"]), (False, True, False))   # one tab: no bar
        self.assertEqual(out["h2"], ["My agents · 0", "Trial agent (shared, limited)", "GitHub"])
        self.assertTrue(out["tryIt"])
        self.assertEqual(out["sent"], ["trial"])                                   # Try it reaches @pm


if __name__ == "__main__":
    unittest.main()
