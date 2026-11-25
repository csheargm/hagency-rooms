"""Unit tests: the panel's cards as a person sees them, rendered by node from the real page helpers
and card scripts (static/panel_*.js). Run: .venv/bin/python -m unittest tests.test_panel_cards"""
import json
import re
import shutil
import subprocess
import unittest

from server import panel

NODE = shutil.which("node")


def render(script: str, call: str) -> str:
    """The HTML `call` (JavaScript) returns, with panel.PAGE's own esc / cap / buttons and that card script."""
    helpers = [re.search(rf"^const {name} = .*$", panel.PAGE, re.M).group(0) for name in ("esc", "cap")]
    buttons = re.search(r"^function buttons\(.*?`</span>`; }$", panel.PAGE, re.S | re.M).group(0)
    source = "\n".join([*helpers, buttons, panel.SCRIPTS[script].read_text(encoding="utf-8"),
                        f"process.stdout.write({call});"])
    done = subprocess.run([NODE, "-e", source], capture_output=True, text=True)
    if done.returncode:
        raise AssertionError(done.stderr[-500:])
    return done.stdout


def text_of(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


@unittest.skipIf(NODE is None, "node not installed")
class WhoCanJoin(unittest.TestCase):
    def card(self, visibility: str) -> str:
        return render("panel-project.js", f"projectCard({json.dumps({'visibility': visibility, 'moderator': True})})")

    def test_the_help_text_names_the_option_that_is_selected(self):
        """Screen test: "Private" was selected and the help text began "Unlisted: ...": two words for
        one setting. Each option's help starts with its own button label, and never says Unlisted."""
        for value in ("private", "listed", "open"):
            page = self.card(value)
            selected = re.search(r"class=on\s*>([^<]+)</button>", page).group(1)
            hint = text_of(re.search(r'<div class="hint">(.*?)</div>', page, re.S).group(1))
            self.assertEqual(selected, value.capitalize())
            self.assertTrue(hint.startswith(f"{selected}:"), hint)
            self.assertNotIn("Unlisted", page)

    def test_no_room_setting_yet_reads_as_private(self):
        self.assertIn("Private: not listed anywhere", render("panel-project.js", "projectCard({moderator: false})"))


@unittest.skipIf(NODE is None, "node not installed")
class ProjectRepo(unittest.TestCase):
    """The owner asked: a repo could be switched, never removed. Remove repo asks twice in the page."""
    OWNER = {"repo": "a/x", "moderator": True}

    def test_the_owner_sees_remove_repo_only_when_a_repo_is_set(self):
        self.assertIn("Remove repo", text_of(render("panel-project.js", f"repoCard({json.dumps(self.OWNER)})")))
        self.assertNotIn("Remove repo", render("panel-project.js", "repoCard({repo: null, moderator: true})"))
        self.assertIn("Pick a project repo", render("panel-project.js", "repoCard({repo: null, moderator: true})"))
        self.assertNotIn("Remove repo", render("panel-project.js", 'repoCard({repo: "a/x", moderator: false})'))

    def test_the_first_click_only_arms_it_and_the_second_removes(self):
        owner = json.dumps(self.OWNER)
        clicks = ('(() => { const b = {dataset: {a: "unrepo", v: "", g: "a/x"}, textContent: ""}; const first = repoConfirmed(b);'
                  f' const shown = repoCard({owner}); const second = repoConfirmed(b);'
                  ' return [first, b.dataset.v, shown.includes("Click again to remove the repo"), second, '
                  f'repoCard({owner}).includes("Click again")].join(","); }})()')
        self.assertEqual(render("panel-project.js", clicks), "false,sure:a/x,true,true,false")

    def test_a_repo_bound_after_the_first_click_is_never_armed(self):
        """GPT re-check: the armed state followed a redraw onto a newer repo, whose click then counted."""
        clicks = ('(() => { const b = {dataset: {a: "unrepo", v: "", g: "a/x"}, textContent: ""}; repoConfirmed(b);'
                  ' const shown = repoCard({repo: "a/y", moderator: true});'
                  ' const c = {dataset: {a: "unrepo", v: "", g: "a/y"}, textContent: ""};'
                  ' return [shown.includes("Click again"), repoConfirmed(c)].join(","); })()')
        self.assertEqual(render("panel-project.js", clicks), "false,false")

    def test_the_page_never_sends_it_without_its_script(self):
        ok = re.search(r"^const ok = .*?; };$", panel.PAGE, re.S | re.M).group(0)
        done = subprocess.run([NODE, "-e", f"const window = {{}}; {ok}\nprocess.stdout.write(String("
                               'ok({dataset: {a: "unrepo", v: "sure:a/x"}})) + String(ok({dataset: {a: "mode"}})))'],
                              capture_output=True, text=True)
        self.assertEqual(done.stdout, "falsetrue", done.stderr)


NEVER = {"name": "newbie1-agent", "label": "main", "first": True, "follow": "always", "remote": True,
         "online": False, "connected": False, "here": False, "invited": True}


@unittest.skipIf(NODE is None, "node not installed")
class MyAgents(unittest.TestCase):
    """Screen test, a newcomer's "My agents": the label "main", "Run the connect line again ... (🔁)" for an
    agent that had never connected, and "Code it runs: the official version" nobody had changed."""
    def card(self, agents: list, version: str | None = "") -> str:
        state = {"agents": agents, "can_add": True, "version": version}
        return text_of(render("panel-agents.js", f"agentCards({json.dumps(state)})"))

    def test_one_agent_that_never_connected(self):
        card = self.card([NEVER])
        self.assertIn("newbie1-agent", card)
        self.assertNotIn("main", card)                                 # a label only matters with several
        self.assertIn("Click Get a fresh code to connect it: its Connect page comes in your chat with Product Manager", card)
        self.assertEqual(card.count("Get a fresh code"), 2)            # the hint and its button
        self.assertNotIn("Run the connect line again", card)
        self.assertNotIn("Code it runs", card)                         # nothing customized: nothing to say
        self.assertNotIn("official version", card)

    def test_disconnect_only_for_one_that_has_connected(self):
        """Screen test 4: a never-connected agent showed Disconnect; Remove and Rename stay."""
        never = render("panel-agents.js", f"agentCards({json.dumps({'agents': [NEVER], 'can_add': False})})")
        self.assertNotIn('data-a="disconnect"', never)
        self.assertIn('data-a="remove"', never)
        self.assertIn('data-a="rename"', never)
        for seen in ({"connected": True}, {"online": True, "connected": True}):
            html = render("panel-agents.js", f"agentCards({json.dumps({'agents': [{**NEVER, **seen}], 'can_add': False})})")
            self.assertIn('data-a="disconnect"', html, seen)

    def test_a_connected_agent_gone_offline_gets_the_reconnect_hint(self):
        card = self.card([{**NEVER, "connected": True, "here": True, "invited": False}])
        self.assertIn("Run the connect line again on its computer", card)
        self.assertIn("Offline", card)
        self.assertIn("in this room", card)

    def test_each_agent_can_be_renamed_and_shows_its_name(self):
        """pm_rename.py: the name people see, with its ID beside it; every value escaped."""
        named = {**NEVER, "display": 'Night <b>"Shift"</b>'}
        html = render("panel-agents.js", f"agentCards({json.dumps({'agents': [named], 'can_add': False})})")
        self.assertIn('<input id="rename-newbie1-agent" maxlength="40"', html)
        self.assertIn('value="Night &lt;b&gt;&quot;Shift&quot;&lt;/b&gt;"', html)
        self.assertIn('data-a="rename" data-v="" data-g="newbie1-agent">Rename</button>', html)
        self.assertIn('<b>Night &lt;b&gt;&quot;Shift&quot;&lt;/b&gt;</b> <span class="muted">· newbie1-agent</span>', html)

    def test_labels_show_when_there_are_several_and_a_custom_version_shows(self):
        codex = {**NEVER, "name": "newbie1-codex-agent", "label": "codex", "first": False}
        card = self.card([NEVER, codex], version="abc1234")
        self.assertIn("main · newbie1-agent", card)
        self.assertIn("codex · newbie1-codex-agent", card)
        self.assertIn("Code it runs: your own version (abc1234)", card)
        self.assertEqual(card.count("Get a fresh code"), 4)            # each: a hint and a button (screen test:
        self.assertNotIn('type "', card)                               # it said what to type in the chat)
        self.assertIn("Go back to official", card)


PAGE_HARNESS = r"""
const vm = require("vm");
const [page, cards, view] = process.argv.slice(1);
const els = {}; const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: ""});
const ctx = {console, URLSearchParams, JSON, String, Promise, Date, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: "?room=%21r", hash: "", pathname: "/panel"}, history: {replaceState() {}},
  document: {getElementById: el, addEventListener() {}, activeElement: null},
  sessionStorage: {getItem: () => "s", setItem() {}, removeItem() {}}, setTimeout: () => 0, clearTimeout() {}, setInterval() {},
  fetch: async () => ({status: 200, json: async () => JSON.parse(view)})};
ctx.window = ctx; ctx.parent = ctx; ctx.addEventListener = () => {};   // a window has it (panel_cards.js)
vm.createContext(ctx); vm.runInContext(cards, ctx); vm.runInContext(page, ctx);
setImmediate(() => setImmediate(() => setImmediate(() => process.stdout.write(els.controls.innerHTML))));
"""


@unittest.skipIf(NODE is None, "node not installed")
class RepoThenMode(unittest.TestCase):
    """Owner request: the "Project repo" card comes before "Mode"; with no repo, only Off can be clicked."""
    def mode(self, **view) -> str:
        return render("panel-project.js", f"modeCard({json.dumps({'mode': 'off', 'moderator': True, **view})})")

    def enabled(self, html: str) -> list:
        """The mode buttons that can be clicked; all three must be there (GPT review: a missing one isn't "disabled")."""
        found = re.findall(r'<button data-a="mode" data-v="(\w+)"([^>]*)>', html)
        self.assertEqual([v for v, _ in found], ["off", "assist", "autonomous"])
        return [v for v, attrs in found if "disabled" not in attrs]

    def test_with_no_repo_only_off_is_clickable_and_it_says_why(self):
        html = self.mode(repo=None)
        self.assertEqual(self.enabled(html), ["off"])
        self.assertIn("Pick the project repo first.", html)

    def test_with_a_repo_every_mode_is_clickable(self):
        html = self.mode(repo="a/b", mode="assist")
        self.assertEqual(self.enabled(html), ["off", "assist", "autonomous"])
        self.assertNotIn("Pick the project repo first", html)
        self.assertIn("a person merges each change", html)

    def test_a_contributor_can_click_none(self):
        self.assertEqual(self.enabled(self.mode(repo="a/b", moderator=False)), [])

    def test_the_repo_card_comes_first(self):
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", '["https://e"]'), re.S).group(1)
        cards = "\n".join(p.read_text() for p in panel.SCRIPTS.values())
        view = {"room": "P", "repo": None, "user": "anna", "moderator": True, "mode": "off", "waiting": None, "html": "",
                "agents": [], "can_add": False}
        done = subprocess.run([NODE, "-e", PAGE_HARNESS, page, cards, json.dumps(view)], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        html = done.stdout
        self.assertLess(html.index("<h2>Pick a project repo</h2>"), html.index("<h2>Mode</h2>"))

    def test_without_its_script_the_panel_can_still_pause(self):
        """GPT review: Mode moved into panel_project.js; if that script doesn't load, Off stays."""
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", '["https://e"]'), re.S).group(1)
        cards = "\n".join(p.read_text() for k, p in panel.SCRIPTS.items() if k != "panel-project.js")
        view = {"room": "P", "repo": "a/b", "user": "anna", "moderator": True, "mode": "assist", "waiting": None,
                "html": "", "agents": [], "can_add": False}
        done = subprocess.run([NODE, "-e", PAGE_HARNESS, page, cards, json.dumps(view)], capture_output=True,
                              text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertRegex(done.stdout, r'<h2>Mode</h2><span class="seg"><button data-a="mode" data-v="off"\s+>Off</button>')


if __name__ == "__main__":
    unittest.main()
