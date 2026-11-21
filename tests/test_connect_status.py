"""The Connect page's live status and steps (connect_status.py, connect_page.py, static/connect.js):
the person opens @pm's link, runs the line, and the page turns from "waiting" to "connected" (or
"expired" with a link back to @pm). The status answers only that, by POST with the code in the body,
rate-limited. Run: .venv/bin/python -m unittest tests.test_connect_status"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common import agent_kinds
from server import connect_page
from server import connect_status
from server import pairing
from common import registry
from server import remote_agent

JS = connect_page.STATIC / "connect.js"
NODE = shutil.which("node")


class Status(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = Path(tempfile.mkdtemp())
        reg = folder / "agents.json"
        reg.write_text(json.dumps({"newbie1": {"agent": "remote"}}))
        for target, name, value in ((registry, "PATH", reg), (remote_agent, "CODES", pairing.Codes(folder / "p.json")),
                                    (remote_agent, "LIMIT", pairing.Limiter()),
                                    (remote_agent, "load_env", lambda: {"PW_NEWBIE1_AGENT": "s3cret"}),
                                    (connect_status, "LIMIT", connect_status.ClientLimiter()), (connect_status, "_used", {}),
                                    (connect_status, "_asks", {})):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        app = web.Application()
        app.add_routes([*connect_page.routes(), *remote_agent.routes()])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def status(self, code, **kw):
        r = await self.client.post(connect_page.STATUS_PATH, json={"code": code}, **kw)
        return r.status, await r.json(), r.headers

    async def test_waiting_then_connected_once_the_computer_used_the_code(self):
        code = remote_agent.CODES.issue("newbie1")
        status, body, headers = await self.status(code)
        self.assertEqual((status, body), (200, {"state": "waiting"}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual((await self.client.post("/connect", json={"code": code})).status, 200)   # connect.py
        self.assertEqual((await self.status(code))[1], {"state": "connected"})
        self.assertEqual((await self.status(code.lower().replace("-", "")))[1], {"state": "connected"})

    async def test_it_says_nothing_about_whose_code_it_is(self):
        code = remote_agent.CODES.issue("newbie1")
        for _ in range(2):
            r = await self.client.post(connect_page.STATUS_PATH, json={"code": code})
            text = await r.text()
            self.assertEqual(set(json.loads(text)), {"state"})
            for secret in ("newbie1", "agent", "s3cret", "bridge.localhost", "homeserver"):
                self.assertNotIn(secret, text)
            await self.client.post("/connect", json={"code": code})

    async def test_wrong_expired_or_odd_codes_are_expired(self):
        old = remote_agent.CODES.issue("newbie1", now=0)
        for code in (old, "ABCDE-FGHJK", "", "x" * 100, None, 7, ["a"]):
            r = await self.client.post(connect_page.STATUS_PATH, json={"code": code}, headers={"X-Forwarded-For": f"{code}"})
            self.assertEqual(await r.json(), {"state": "expired"}, code)
            connect_status.LIMIT = connect_status.ClientLimiter()        # each from a fresh limiter: only the answer is checked

    async def test_guessing_is_rate_limited_and_never_locks_out_connect_py(self):
        for i in range(pairing.FAILS_PER_CLIENT):
            self.assertEqual((await self.status(f"WRONG-{i:05d}"))[1], {"state": "expired"})
        self.assertEqual((await self.status("WRONG-99999"))[0], 429)
        good = remote_agent.CODES.issue("newbie1")
        self.assertEqual((await self.status(good))[0], 429)                           # this client: wait
        self.assertEqual((await self.client.post("/connect", json={"code": good})).status, 200)   # connect.py: fine

    async def test_polling_too_fast_is_slowed_down(self):
        code = remote_agent.CODES.issue("newbie1")
        answers = [(await self.status(code))[0] for _ in range(connect_status.REQUESTS_PER_MIN + 1)]
        self.assertEqual(answers[-1], 429)
        self.assertEqual(set(answers[:-1]), {200})
        with mock.patch.object(connect_status.time, "time", lambda: 10**10):
            self.assertEqual((await self.status(code))[0], 200)                       # a minute later: fine

    async def test_only_a_json_post_with_the_code_in_the_body(self):
        code = remote_agent.CODES.issue("newbie1")
        r = await self.client.get(connect_page.STATUS_PATH + f"?code={code}")
        self.assertEqual(r.status, 405)
        r = await self.client.post(connect_page.STATUS_PATH, data=f"code={code}",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 415)                       # a form from another site can't even ask
        r = await self.client.post(connect_page.STATUS_PATH + f"?code={code}", json={})
        self.assertEqual(await r.json(), {"state": "expired"})   # a code in the URL is never read

    def test_many_clients_guessing_never_block_someone_elses_status(self):
        """GPT review: a shared wrong-code budget would stop every page's status."""
        for i in range(pairing.FAILS_TOTAL + 5):
            connect_status.LIMIT.failed(f"10.0.{i // 250}.{i % 250}", now=100)
        self.assertFalse(connect_status.too_many("192.0.2.1", now=101))
        self.assertNotIn("*", connect_status.LIMIT.fails)                 # GPT re-check: no shared list to grow

    def test_refused_asks_are_not_piled_up(self):
        for _ in range(connect_status.REQUESTS_PER_MIN * 5):
            connect_status.too_many("192.0.2.9", now=50)
        self.assertEqual(len(connect_status._asks["192.0.2.9"]), connect_status.REQUESTS_PER_MIN)

    def test_used_codes_are_forgotten_in_time(self):
        connect_status.mark_used("ABCDE-FGHJK", now=100)
        codes = pairing.Codes(Path(tempfile.mkdtemp()) / "p.json")
        self.assertEqual(connect_status.state(codes, "ABCDE-FGHJK", now=101), "connected")
        self.assertEqual(connect_status.state(codes, "ABCDE-FGHJK", now=101 + connect_status.USED_KEEP_S), "expired")


@unittest.skipUnless(NODE, "node isn't installed: the page's steps and status are not exercised")
class PageUnderNode(unittest.TestCase):
    def run_js(self, expr: str, data=None) -> object:
        program = (f"const m = require({json.dumps(str(JS))}); const data = JSON.parse(process.argv[1]);"
                   f"(async () => process.stdout.write(JSON.stringify(await ({expr}))))();")
        out = subprocess.run([NODE, "-e", program, json.dumps(data)], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_each_os_says_how_to_open_paste_and_run(self):
        steps = self.run_js("m.STEPS")
        for os_, opener, paste in (("mac", "Terminal", "⌘V"), ("windows", "PowerShell", "Ctrl+V"),
                                   ("linux", "Terminal", "Ctrl+Shift+V")):
            self.assertIn(opener, steps[os_]["how"])
            self.assertIn(paste, steps[os_]["paste"])
            self.assertIn(steps[os_]["enter"], steps[os_]["paste"])

    def test_the_fragment_names_the_code_and_a_known_kind(self):
        kinds = agent_kinds.kinds()
        got = self.run_js("['#ABCDE-FGHJK&kind=ollama', '#ABCDE-FGHJK&kind=gpt', '#ABCDE-FGHJK&kind=<b>', "
                          "'#abcde-fghjk', '#$(id)&kind=ollama'].map(h => m.parseFragment(h, data))", kinds)
        self.assertEqual(got, [{"code": "ABCDE-FGHJK", "kind": "ollama"}, {"code": "ABCDE-FGHJK", "kind": ""},
                               {"code": "ABCDE-FGHJK", "kind": ""}, {"code": "ABCDE-FGHJK", "kind": ""},
                               {"code": "", "kind": "ollama"}])

    def test_only_a_whole_code_is_asked_about(self):
        """GPT review: a code still being typed must not use up the wrong-code budget."""
        got = self.run_js("['ABCD', 'ABCDE-FGH', 'ABCDE-FGHJK', 'ABCDEFGHJK', 'ABC-DEFGH-JK', ''].map(m.pollable)")
        self.assertEqual(got, ["", "", "ABCDE-FGHJK", "ABCDEFGHJK", "ABC-DEFGH-JK", ""])

    def test_before_you_start_lists_the_chosen_kinds_needs(self):
        kinds = agent_kinds.kinds()
        got = self.run_js("[m.needsFor('mac', 'remote', data), m.needsFor('windows', '', data), "
                          "m.runsOn('windows', 'hermes', data), m.runsOn('linux', 'hermes', data)]", kinds)
        remote, legacy, hermes_windows, hermes_linux = got
        self.assertEqual(remote[:3], agent_kinds.ABOUT["remote"]["needs"])
        self.assertIn("git and Python 3.11", remote[-1])
        self.assertIn("Codex", legacy[0])
        self.assertIn("Claude Code", legacy[0])
        self.assertIn("winget", legacy[-1])
        self.assertEqual((hermes_windows, hermes_linux), (False, True))

    def test_the_status_follows_one_code_until_it_is_settled(self):
        """makePoller with a fake server: waiting, waiting, connected, then no more asks."""
        got = self.run_js("""(async () => {
            const shown = [], asked = [], answers = ['waiting', 'waiting', 'connected'];
            const later = (fn) => { setImmediate(fn); return 0; };
            const follow = m.makePoller((s) => shown.push(s), async (c) => { asked.push(c); return answers.shift(); }, later);
            follow('ABCDE-FGHJK');
            await new Promise((r) => setTimeout(r, 50));
            return { shown, asked: asked.length };
        })()""")
        self.assertEqual(got["shown"], ["waiting", "waiting", "waiting", "connected"])
        self.assertEqual(got["asked"], 3)

    def test_an_expired_code_stops_asking_and_offers_pm(self):
        got = self.run_js("""(async () => {
            const shown = [];
            let asks = 0;
            const follow = m.makePoller((s) => shown.push(s), async () => { asks++; return 'expired'; },
                                        (fn) => { setImmediate(fn); return 0; });
            follow('ABCDE-FGHJK');
            await new Promise((r) => setTimeout(r, 50));
            return { shown, asks, text: m.STATUS.expired };
        })()""")
        self.assertEqual((got["shown"], got["asks"]), (["waiting", "expired"], 1))
        self.assertIn("Ask @pm for a new one", got["text"])


if __name__ == "__main__":
    unittest.main()
