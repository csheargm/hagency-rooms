"""My agents' "Sign in to GitHub", end to end: the agent's start report, the panel's real button clicked in
node, console.act, @pm's to-device request, the agent running a stand-in gh, its code shown to the owner
only, then "GitHub: @login ✓" (server/console_github.py, adapters/agents/agent_github.py; ADR 0021).
Live test 2026-11-26: "needs a GitHub sign-in" offered only a shell command.
Run: .venv/bin/python -m unittest tests.test_console_github"""
import asyncio
import html
import json
import re
import shutil
import subprocess
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import ToDeviceEvent, ToDeviceResponse, UnknownToDeviceEvent

from adapters.agents import agent_github
from common import github_login
from common import registry
from server import console
from server import console_github
from server import panel
from tests import test_agent_github as agent_side
from tests import test_more_agents_flows as flows
from tests.test_more_agents_flows import BA, CX, CXA

NODE = shutil.which("node")
CLICK_HARNESS = r"""
const vm = require("vm"); const els = {}; const el = id => els[id] || (els[id] = {textContent: "", innerHTML: "", value: "", style: {}});
const view = JSON.parse(process.argv[1]), clicks = [], posted = [];
const ctx = {console, URLSearchParams, JSON, String, Promise, Date, encodeURIComponent, crypto: globalThis.crypto,
  location: {search: "?room=%21dm", hash: "", pathname: "/panel"}, history: {replaceState() {}},
  document: {getElementById: el, addEventListener: (t, f) => { if (t === "click") clicks.push(f); }, activeElement: null},
  sessionStorage: {getItem: () => "s", setItem() {}, removeItem() {}}, setTimeout: () => 0, clearTimeout() {}, setInterval() {},
  fetch: async (url, opts) => opts && opts.method === "POST"
    ? (posted.push(JSON.parse(opts.body)), {status: 200, json: async () => JSON.parse(process.argv[4])})
    : {status: 200, json: async () => view}};
ctx.window = ctx; ctx.parent = ctx; ctx.addEventListener = () => {};
vm.createContext(ctx); vm.runInContext(process.argv[3], ctx); vm.runInContext(process.argv[2], ctx);
const later = f => setImmediate(() => setImmediate(() => setImmediate(f)));
later(() => {
  const html = els.controls.innerHTML, m = /<button data-a="github_signin" data-v="([^"]*)" data-g="([^"]*)"[^>]*>/.exec(html);
  if (m) {                                  // the person clicks it: every click listener the page added gets it
    const b = {dataset: {a: "github_signin", v: m[1], g: m[2]}};
    b.closest = s => s.startsWith("button[data-a]") ? b : null;
    for (const f of clicks) f({target: b, button: 0});
  }
  later(() => process.stdout.write(JSON.stringify({html, posted, msg: el("msg").textContent})));
});
"""


class Flow(flows.Base):
    def setUp(self):
        super().setUp()
        self.online = {"cx-agent"}
        self.lp.agent_online = lambda a: True if a in self.online else None
        self.lp.ever_connected = lambda a: True
        self.lp.client = SimpleNamespace(to_device=mock.AsyncMock(side_effect=lambda m, tx: ToDeviceResponse(m)),
                                         rooms={"!r": SimpleNamespace(users={CX: 0}, invited_users={}, name="P", room_id="!r")})
        self.lp.is_person = lambda r, u: True
        self.gh = agent_side.Base("setUp")       # the agent's computer: its .gh folder and a stand-in gh
        self.gh.setUp()
        self.addCleanup(self.gh.doCleanups)
        self.agent = agent_side.FakeBridge()

    def deliver(self, sent, sender=CXA):
        """What the agent sent, as @pm receives it (nio's parsing, Synapse's sender)."""
        for kind, _, content in sent:
            event = ToDeviceEvent.parse_event({"sender": sender, "type": kind, "content": content})
            self.assertIsInstance(event, UnknownToDeviceEvent)
            asyncio.run(self.lp.on_kind_report(event))

    def start(self):
        asyncio.run(self.agent.report_github())
        self.deliver(self.agent.sent)
        self.agent.sent.clear()

    def row(self):
        return console.my_agents(self.lp, SimpleNamespace(users={}, invited_users={}), "!r", CX)[0]

    def click(self, user=CX):
        return asyncio.run(console.act(self.lp, "!r", user, "github_signin", "", "cx-agent"))

    def pm_asks_agent(self, mode="ok"):
        """@pm's request reaches the agent's computer, which signs in with the stand-in gh."""
        message = self.lp.client.to_device.await_args.args[0]
        self.assertEqual((message.type, message.recipient), (github_login.REQUEST_TYPE, CXA))
        self.gh.click(self.agent, mode=mode, ask=message.content["id"])   # @pm's own click id, echoed
        sent = list(self.agent.sent)
        self.agent.sent.clear()
        return sent

    def panel(self, reply=None) -> dict:
        page = re.search(r"<script>(.*)</script>", panel.PAGE.replace("__EMBEDDERS__", '["https://e"]'), re.S).group(1)
        scripts = "\n".join(p.read_text() for p in panel.SCRIPTS.values())
        view = {"personal": True, "room": "My Hagency", "user": "cx", "agents": [self.row()], "can_add": False,
                "kinds": [], "github": None, "html": ""}
        done = subprocess.run([NODE, "-e", CLICK_HARNESS, json.dumps(view), page, scripts,
                               json.dumps(reply or {"result": "ok"})], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout)


class EndToEnd(Flow):
    def test_click_code_for_the_owner_only_then_signed_in(self):
        if NODE is None:
            self.skipTest("node not installed")
        self.start()
        shown = self.panel(reply={"result": "ok", "says": console_github.ASKED})
        self.assertIn("Not signed in to GitHub yet", shown["html"])
        self.assertEqual(shown["msg"], console_github.ASKED)   # what the page says after the click
        self.assertIn(html.escape(github_login.command("cx-agent")), shown["html"])   # terminal users: the one recipe
        self.assertEqual(len(shown["posted"]), 1)          # the button, clicked: what the page sends
        body = shown["posted"][0]
        self.assertEqual((body["action"], body["agent"], body["value"]), ("github_signin", "cx-agent", ""))
        said = asyncio.run(console.act(self.lp, "!r", CX, body["action"], body["value"], body["agent"]))
        self.assertEqual(said, {"result": "ok", "says": console_github.ASKED})
        self.assertEqual(self.row()["gh_says"], console_github.STARTING)

        code, *rest = self.pm_asks_agent()
        self.deliver([code])                                # the code first
        row = self.row()
        self.assertEqual((row["gh_code"], row["gh_url"], row["gh_busy"]), ("WDJB-MJHT", github_login.DEVICE_PAGE, True))
        waiting = self.panel()["html"]
        self.assertIn("WDJB-MJHT", waiting)
        self.assertIn("https://github.com/login/device", waiting)
        self.assertNotIn('data-a="github_signin"', waiting)   # no second click while it waits
        self.assertEqual(self.said, [("!dm", console_github.DM.format(agent="cx-agent", url=github_login.DEVICE_PAGE,
                                                                     code="WDJB-MJHT"))])   # their private chat only

        self.deliver(rest)                                  # its result, then its state
        done = self.panel()
        self.assertIn("GitHub: @octo-cat ✓", done["html"])
        self.assertIn('class="quiet">Sign in again</button>', done["html"])   # a revoked token isn't a lock-out
        self.assertEqual([b["action"] for b in done["posted"]], ["github_signin"])
        self.assertEqual(registry.find(CX, "cx-agent")[registry.GITHUB], {"state": "signed_in", "login": "octo-cat"})
        self.assertNotIn("gho_", json.dumps(self.said) + json.dumps(self.row()))

    def test_a_failed_sign_in_says_so_and_the_button_comes_back(self):
        self.start()
        self.click()
        self.deliver(self.pm_asks_agent(mode="fail"))
        row = self.row()
        self.assertEqual((row["gh"], row["gh_says"], row["gh_busy"]), ("none", console_github.SAYS["failed"], False))

    def test_another_server_or_agent_never_sees_the_code(self):
        self.start()
        self.click()
        code, *_ = self.pm_asks_agent()
        self.deliver([code], sender=BA)                     # another person's agent can't answer for it
        self.assertEqual(self.row()["gh_code"], "")
        self.assertEqual(self.said, [])


class Checks(Flow):
    def test_only_the_owner_of_an_agent_on_their_computer(self):
        self.start()
        self.assertEqual(self.click(user="@betty:bridge.localhost"), console_github.NOT_YOURS)
        self.lp.client.to_device.assert_not_awaited()

    def test_older_code_is_asked_to_update_first(self):
        row = self.row()                                    # never sent a GitHub state: code from before 0021
        self.assertEqual((row["gh"], row["gh_says"]), ("old", console_github.OLD_CODE))
        self.assertEqual(self.click(), console_github.OLD_CODE)
        self.lp.client.to_device.assert_not_awaited()

    def test_offline_is_not_asked_and_signed_in_may_sign_in_again(self):
        self.start()
        self.online.clear()
        self.assertIn("offline", self.click())
        self.online.add("cx-agent")
        (self.gh.root / "cx-agent").mkdir(parents=True)
        (self.gh.root / "cx-agent" / "hosts.yml").write_text("github.com:\n    user: octo-cat\n")
        self.start()
        self.assertEqual(self.click()["result"], "ok")     # its token may be revoked since: the agent checks (GPT re-check)
        self.assertEqual(self.lp.client.to_device.await_count, 1)

    def test_one_click_a_minute_and_none_while_one_waits(self):
        self.start()
        self.assertEqual(self.click()["result"], "ok")
        self.assertEqual(self.click(), console_github.STARTING)        # waiting for its code
        self.lp._gh_signins["cx-agent"]["at"] -= console_github.CODE_WAIT_S + 1
        self.assertEqual(self.row()["gh_says"], console_github.NO_ANSWER)
        self.lp._gh_signins["cx-agent"]["at"] = time.time() - 30
        self.lp._gh_signins["cx-agent"]["code"], self.lp._gh_signins["cx-agent"]["code_at"] = None, None
        self.lp._gh_signins["cx-agent"]["result"], self.lp._gh_signins["cx-agent"]["done_at"] = "failed", time.time()
        self.assertIn("Wait a minute", self.click())
        self.assertEqual(self.lp.client.to_device.await_count, 1)

    def test_a_failed_send_frees_the_click(self):
        self.start()
        self.lp.client.to_device.side_effect = OSError("down")
        self.assertIn("couldn't reach", self.click())
        self.assertNotIn("cx-agent", self.lp._gh_signins)

    def test_only_the_waiting_clicks_answers_in_githubs_shape(self):
        self.start()
        self.click()
        ask = self.lp._gh_signins["cx-agent"]["id"]
        for content in ({"id": "older", "code": "WDJB-MJHT"}, {"id": ask, "code": "<b>hi</b>"},
                        {"id": ask, "code": "WDJB-MJHT", "extra": "x"}, {"id": ask, "result": "rm -rf"},
                        {"id": ask, "result": "signed_in", "login": "a b"}, {"id": ask}):
            self.deliver([(github_login.ANSWER_TYPE, None, content)])
        row = self.row()
        self.assertEqual((row["gh_code"], row["gh_says"]), ("", console_github.STARTING))
        self.assertEqual(self.said, [])

    def test_a_code_shown_too_long_ago_counts_as_timed_out(self):
        self.start()
        self.click()
        ask = self.lp._gh_signins["cx-agent"]["id"]
        self.deliver([(github_login.ANSWER_TYPE, None, {"id": ask, "code": "WDJB-MJHT"})])
        self.lp._gh_signins["cx-agent"]["code_at"] -= console_github.SIGNIN_S + 1
        row = self.row()
        self.assertEqual((row["gh_code"], row["gh_says"]), ("", console_github.SAYS["timeout"]))

    def test_a_code_after_it_said_no_answer_is_never_shown(self):
        """GPT review: a late code started a fresh window and went to the owner's chat."""
        self.start()
        self.click()
        self.lp._gh_signins["cx-agent"]["at"] -= console_github.CODE_WAIT_S + 1
        ask = self.lp._gh_signins["cx-agent"]["id"]
        self.deliver([(github_login.ANSWER_TYPE, None, {"id": ask, "code": "WDJB-MJHT"})])
        self.assertEqual((self.row()["gh_code"], self.said), ("", []))

    def test_the_server_runs_agents_get_nothing(self):
        self.assertEqual(console_github.status(self.lp, {"agent": "codex", "localpart": "x"})["gh"], "")

    def test_every_result_word_has_plain_words(self):
        self.assertEqual(set(console_github.SAYS), set(github_login.RESULTS))
        self.assertEqual(agent_github.PM_ID, f"@pm:{flows.S}")


if __name__ == "__main__":
    unittest.main()
