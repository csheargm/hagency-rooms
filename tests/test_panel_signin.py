"""The side panel's sign-in, as a newcomer meets it in Element (screen test: a new member saw the panel
in the Lobby stuck at "Signing in…" for good). The page's own script runs in node against a stand-in
for Element's widget API: every way signing in can fail ends in one clear line, never a silent wait.
Run: .venv/bin/python -m unittest tests.test_panel_signin"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest

from server import panel
from server import project_rooms

# A stand-in for Element and the browser: the page's script runs unchanged; timers fire when told.
HARNESS = r"""
const vm = require("vm");
const [script, scenario, element] = [process.argv[2], process.argv[3], process.argv[4]];
const els = {me: {textContent: "Signing in…", innerHTML: ""}};
const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: ""});
let timers = [], listener = null, store = {}, calls = [], sent = [];
const parent = {posted: [], postMessage(m) { this.posted.push(m); }};
const routes = JSON.parse(process.argv[5]);
const ctx = {
  console, URLSearchParams, JSON, String, Promise, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: "?room=%21r&widgetId=hagency", hash: scenario === "link_once" ? "#login=abc" : "", pathname: "/panel"},
  history: {replaceState() {}},
  document: {getElementById: el, addEventListener() {}, activeElement: null},
  sessionStorage: {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; }},
  setTimeout: (f) => { timers.push(f); return f; }, clearTimeout: (f) => { timers = timers.filter(t => t !== f); },
  setInterval() {},
  AbortSignal: {timeout: () => { const c = new AbortController(); timers.push(() => c.abort()); return c.signal; }},
  fetch: async (path, opts) => { const p = path.split("?")[0].replace(/^\/?/, "/"); calls.push(p);
    if (p === "/auth/openid") sent.push(opts.body);
    let r = routes[p]; if (r === "first_then_hang") r = calls.filter(c => c === p).length > 1 ? "hang" : routes.first;
    if (r === "hang") return new Promise((_, no) => opts && opts.signal && opts.signal.addEventListener("abort", () => no(new Error("aborted"))));
    if (r === undefined || r === "broken") return {status: 500, json: async () => { throw new SyntaxError("<html>"); }};
    return {status: r.status || 200, json: async () => r.body}; },
};
ctx.window = {parent, addEventListener: (t, f) => { listener = f; }};
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r)); };
const fire = async () => { const due = timers; timers = []; due.forEach(f => f()); await flush(); };
const send = async data => { listener({origin: element, source: parent, data: {widgetId: "hagency", ...data}}); await flush(); };
const asked = () => parent.posted.filter(m => m.action === "get_openid").pop();
(async () => {
  if (scenario === "expired") store["hagency-session"] = "old";
  vm.createContext(ctx); vm.runInContext(script, ctx); await flush();
  if (scenario !== "expired") await send({api: "toWidget", requestId: "c1", action: "capabilities", data: {}});
  if (scenario !== "expired") await fire();              // the 300 ms before asking
  const reply = response => send({api: "fromWidget", action: "get_openid", requestId: asked().requestId, response});
  if (scenario === "silent") await fire();               // Element never answers: the watchdog
  if (scenario === "error_reply") await reply({error: {message: "no"}});
  if (scenario === "prompt_declined") {
    await reply({state: "request"});
    process.stdout.write(els.me.textContent + "\n");
    await send({api: "toWidget", requestId: "c2", action: "openid_credentials",
                data: {state: "blocked", original_request_id: asked().requestId}});
  }
  if (["broken_server", "lobby", "happy", "state_hangs", "poll_stalls"].includes(scenario)) await reply({state: "allowed", access_token: "t"});
  if (scenario === "other_server") { await reply({state: "allowed", access_token: "t", matrix_server_name: "matrix.org"});
    process.stdout.write(sent.join("|") + "\n"); }
  if (scenario === "expired") {
    process.stdout.write(els.me.textContent + "|" + (asked() ? "asked" : "not asked") + "\n");
    await send({api: "toWidget", requestId: "c1", action: "capabilities", data: {}});
    await fire();
    process.stdout.write((asked() ? "asked" : "not asked") + "\n");
  }
  if (["state_hangs", "link_once"].includes(scenario)) { await fire(); await fire(); }
  if (scenario === "poll_stalls") { ctx.refresh(); await flush(); await fire();
    process.stdout.write(els.me.textContent + "\n"); ctx.refresh(); await flush();
    process.stdout.write(calls.filter(c => c === "/console/state").length + "\n"); }
  if (scenario === "link_once") process.stdout.write(calls.filter(c => c === "/auth/link").length + "\n");
  process.stdout.write(els.me.textContent);
})();
"""
VIEW = {"room": "Our project", "repo": None, "user": "finaltest2", "moderator": False, "mode": "off",
        "waiting": None, "html": ""}


class SignIn(unittest.TestCase):
    def setUp(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        self.node = node
        page = panel.PAGE.replace("__EMBEDDERS__", json.dumps([panel.ELEMENT_ORIGIN]))
        self.script = re.search(r"<script>(.*)</script>", page, re.S).group(1)

    def run_page(self, scenario: str, routes: dict | None = None) -> list[str]:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as h:
            h.write(HARNESS)
        done = subprocess.run([self.node, h.name, self.script, scenario, panel.ELEMENT_ORIGIN, json.dumps(routes or {})],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return done.stdout.split("\n")

    def assert_clear(self, line: str):
        self.assertNotEqual(line, "Signing in…")
        self.assertTrue(line.startswith("Couldn't") or line.startswith("The Hagency"), line)
        self.assertNotIn("\n", line)

    def test_element_never_answers_says_so(self):
        last = self.run_page("silent")[-1]
        self.assert_clear(last)
        self.assertIn("Element didn't answer", last)

    def test_an_error_reply_from_element_says_so(self):
        self.assert_clear(self.run_page("error_reply")[-1])

    def test_declining_elements_prompt_says_so(self):
        asked, last = self.run_page("prompt_declined")
        self.assertIn("Allow Hagency in Element's prompt", asked)
        self.assertIn("didn't share who you are", last)

    def test_a_broken_server_answer_says_so(self):
        last = self.run_page("broken_server", {"/auth/openid": "broken"})[-1]
        self.assert_clear(last)
        self.assertIn("Hagency server", last)

    def test_in_a_room_that_isnt_a_project_the_reason_is_shown(self):
        routes = {"/auth/openid": {"body": {"session": "s"}},
                  "/console/state": {"body": {"error": project_rooms.NOT_A_PROJECT}}}
        self.assertEqual(self.run_page("lobby", routes)[-1], project_rooms.NOT_A_PROJECT)

    def test_signed_in_clears_the_line(self):
        routes = {"/auth/openid": {"body": {"session": "s"}}, "/console/state": {"body": VIEW}}
        self.assertEqual(self.run_page("happy", routes)[-1], "")

    def test_a_view_that_never_comes_still_ends_in_a_clear_line(self):
        """GPT review: a good token then a /console/state that never answered cleared the watchdog."""
        last = self.run_page("state_hangs", {"/auth/openid": {"body": {"session": "s"}}, "/console/state": "hang"})[-1]
        self.assert_clear(last)

    def test_a_stalled_poll_gives_up_says_so_and_polling_goes_on(self):
        """GPT re-check: a poll that never answered held the one-at-a-time lock, freezing the panel."""
        shown, polls, _ = self.run_page("poll_stalls", {"/auth/openid": {"body": {"session": "s"}},
                                                        "/console/state": "first_then_hang", "first": {"body": VIEW}})
        self.assertIn("Hagency server", shown)
        self.assertEqual(polls, "3")                       # the next poll went out

    def test_a_one_time_link_is_never_sent_twice(self):
        """GPT review: an expired link session replayed the used code ("that link expired or was used")."""
        sent, last = self.run_page("link_once", {"/auth/link": {"body": {"session": "s"}},
                                                 "/console/state": {"status": 401, "body": {"error": "sign in"}}})
        self.assertEqual(sent, "1")
        self.assert_clear(last)

    def test_the_token_goes_with_the_server_that_issued_it(self):
        """Screen test: a matrix.org person couldn't sign in; @pm checks the token with THEIR server
        (fed_openid.py), so the page sends Element's matrix_server_name along."""
        sent, last = self.run_page("other_server", {"/auth/openid": {"body": {"session": "s"}}, "/console/state": {"body": VIEW}})
        self.assertEqual(json.loads(sent), {"access_token": "t", "matrix_server_name": "matrix.org"})
        self.assertEqual(last, "")

    def test_an_expired_session_asks_element_again(self):
        """A session from before @pm restarted: 401, then a fresh sign-in, never a stale page."""
        before, after, _ = self.run_page("expired", {"/console/state": {"status": 401, "body": {"error": "sign in"}}})
        self.assertEqual(before, "Signing in…|not asked")
        self.assertEqual(after, "asked")


if __name__ == "__main__":
    unittest.main()
