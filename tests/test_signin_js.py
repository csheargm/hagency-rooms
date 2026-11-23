"""The shared sign-in's SSO return (static/signin.js) and the invite page's start (static/invite.js),
run under node with a small fake browser: only a return carrying this tab's own nonce signs in, and a
fresh invite link's fragment wins over an older one kept in the tab (GPT review of ADR 0015).
Run: .venv/bin/python -m unittest tests.test_signin_js"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
NODE = shutil.which("node")
NONCE = "ab" * 16
# A fake browser just big enough for signin.js and invite.js; prints the requests the page made.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [dir, setup] = [process.argv[1], JSON.parse(process.argv[2])];
const calls = [], els = {}, store = new Map(Object.entries(setup.storage));
const el = () => ({hidden: true, textContent: "", className: "", value: "", appendChild() {}, append() {},
  addEventListener() {}, replaceChildren() {}});
const url = new URL(setup.url);
const ctx = {console, URL, URLSearchParams, JSON, Date, Promise, Array, Object, String, Uint8Array,
  crypto: {getRandomValues: a => a},
  location: {hash: url.hash, search: url.search, pathname: url.pathname, origin: url.origin, assign: u => calls.push(["go", u]),
    reload() {}},
  history: {replaceState: (a, b, u) => { calls.push(["address", u]); const n = new URL(u, url.origin);
    ctx.location.hash = n.hash; ctx.location.search = n.search; }},
  sessionStorage: {getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k)},
  localStorage: {getItem: () => "x", setItem() {}},
  document: {querySelector: q => ({content: q.includes("homeserver") ? "https://hs" : "https://r"}),
    getElementById: id => (els[id] = els[id] || el()), createElement: el, addEventListener: (e, f) => { ctx.ready = f; }},
  window: {addEventListener() {}},
  fetch: async (u, o) => { calls.push(["fetch", String(u), o && o.body ? JSON.parse(o.body) : null]);
    const data = String(u).endsWith("/login") && !o ? {flows: []} : String(u).includes("invite/check") ? {state: "ok", room: "R", by: "B"}
      : String(u).endsWith("/login") ? {} : {};
    return {status: 200, json: async () => data}; }};
vm.createContext(ctx);
for (const f of ["signin.js", "invite.js"]) vm.runInContext(fs.readFileSync(dir + "/" + f, "utf8"), ctx, {filename: f});
ctx.ready();
setTimeout(() => process.stdout.write(JSON.stringify(calls)), 50);
"""


@unittest.skipUnless(NODE, "node isn't installed: the sign-in script is not exercised")
class SsoReturn(unittest.TestCase):
    def own(self, mark, query, now=0):
        program = (f"const m = require({json.dumps(str(STATIC / 'signin.js'))});"
                   "const [mark, q, now] = JSON.parse(process.argv[1]);"
                   "process.stdout.write(JSON.stringify(m.ownReturn(mark, new URLSearchParams(q), now)));")
        out = subprocess.run([NODE, "-e", program, json.dumps([mark, query, now])], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_only_this_tabs_own_nonce_counts(self):
        mark = {"idp": "google", "until": 10, "nonce": NONCE}
        self.assertEqual(self.own(mark, f"loginToken=t&sso={NONCE}"), {"idp": "google", "token": "t"})
        for m, q in ((mark, "loginToken=t"), (mark, "loginToken=t&sso=" + "cd" * 16), (None, f"loginToken=t&sso={NONCE}"),
                     ({**mark, "nonce": "short"}, "loginToken=t&sso=short"), ({**mark, "until": 0}, f"loginToken=t&sso={NONCE}"),
                     ({"idp": "google", "until": 10}, "loginToken=t&sso=undefined"), (mark, f"sso={NONCE}")):
            self.assertIsNone(self.own(m, q), (m, q))

    def page(self, url, storage):
        out = subprocess.run([NODE, "-e", HARNESS, str(STATIC), json.dumps({"url": url, "storage": storage})],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)

    def test_a_fresh_link_wins_over_an_sso_return_and_an_older_link(self):
        mark = json.dumps({"idp": "google", "until": 10**13, "nonce": NONCE})
        calls = self.page(f"https://r/invite?loginToken=t&sso={NONCE}#FRESH", {"hagency-invite": "OLD", "hagency-invite-idp": mark})
        checks = [c[2] for c in calls if c[0] == "fetch" and c[1].endswith("invite/check")]
        self.assertEqual(checks, [{"token": "FRESH"}])
        self.assertNotIn("m.login.token", json.dumps(calls))                     # a link in the address: no SSO sign-in
        self.assertNotIn("loginToken", json.dumps([c for c in calls if c[0] == "address"][-1]))

    def test_an_sso_return_signs_in_only_with_this_tabs_nonce(self):
        mark = json.dumps({"idp": "google", "until": 10**13, "nonce": NONCE})
        signed = self.page(f"https://r/invite?loginToken=t&sso={NONCE}", {"hagency-invite": "OLD", "hagency-invite-idp": mark})
        self.assertIn({"type": "m.login.token", "token": "t", "initial_device_display_name": "Hagency invite link"},
                      [c[2] for c in signed if c[0] == "fetch"])
        crafted = self.page("https://r/invite?loginToken=t&sso=" + "cd" * 16, {"hagency-invite": "OLD", "hagency-invite-idp": mark})
        self.assertNotIn("m.login.token", json.dumps(crafted))


if __name__ == "__main__":
    unittest.main()
