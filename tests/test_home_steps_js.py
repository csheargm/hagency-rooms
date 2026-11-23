"""The browser side of the home page's steps ① and ③ (static/agent.js, static/invite_new.js; ADR 0015),
run under node: the Connect page address Get your agent opens is the one the Connect page reads, a
choice is sent as the panel's picker sends it, and only our own invite links are shown.
Run: .venv/bin/python -m unittest tests.test_home_steps_js"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from common import agent_kinds
from common import pm_kinds
from server import invite_page
from server import pairing

STATIC = Path(__file__).resolve().parent.parent / "static"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node isn't installed: the pages' scripts are not exercised")
class Scripts(unittest.TestCase):
    def js(self, expr: str, *data) -> object:
        program = (f"const agent = require({json.dumps(str(STATIC / 'agent.js'))});"
                   f"const invite = require({json.dumps(str(STATIC / 'invite_new.js'))});"
                   f"const connect = require({json.dumps(str(STATIC / 'connect.js'))});"
                   f"const data = JSON.parse(process.argv[1]); process.stdout.write(JSON.stringify({expr}));")
        out = subprocess.run([NODE, "-e", program, json.dumps(data)], capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_the_connect_page_reads_back_the_code_and_kind_a_real_code_has(self):
        code = pairing.Codes(Path(tempfile.mkdtemp()) / "c.json").issue("@a:x")
        kinds = agent_kinds.kinds()
        for kind in ("", *agent_kinds.KINDS):
            link = self.js("agent.connectLink(data[0], data[1])", code, kind)
            self.assertTrue(link.startswith(f"/connect/start#{code}"), link)
            got = self.js("connect.parseFragment(data[0].split('#')[1], data[1])", link, kinds)
            self.assertEqual(got, {"code": code, "kind": kind})

    def test_nothing_else_gets_into_the_address(self):
        for code, kind in (("x", ""), ("ABCDE-FGHJK/../x", ""), ("ABCDE-FGHJK", "a&b=c"), ("", "codex")):
            link = self.js("agent.connectLink(data[0], data[1])", code, kind)
            self.assertIn(link, ("", "/connect/start#ABCDE-FGHJK"), (code, kind))

    def test_a_choice_is_what_the_panels_picker_sends(self):
        for args, picked in ((("ollama", " qwen3:8b ", "ignored"), {"agent_kind": "ollama", "model": "qwen3:8b"}),
                             (("claude", "a-model", "x"), {"agent_kind": "claude"}),
                             (("remote", "openai/gpt-4o", "https://openrouter.ai/api/v1"),
                              {"agent_kind": "remote", "model": "openai/gpt-4o", "base_url": "https://openrouter.ai/api/v1"})):
            sent = self.js("agent.pickOf(data[0], data[1], data[2])", *args)
            self.assertEqual(pm_kinds.from_panel(json.dumps(sent)), ("", picked))
        self.assertEqual(pm_kinds.from_panel(json.dumps(self.js("agent.pickOf('', 'm', 'b')"))), ("", None))

    def test_what_a_signed_in_person_is_told(self):
        said = self.js("[agent.haveText({agent: 'newb-agent', connected: true, runs: 'Codex'}),"
                       " agent.haveText({agent: 'newb-agent', pick: true})]")
        self.assertEqual(said, ["Your agent newb-agent is connected already (Codex). To connect it on another computer, "
                                "get a new command.", "Your agent will be newb-agent."])

    def test_only_our_invite_links_are_shown(self):
        good = "https://rooms.example.org/invite#" + "a" * 43
        for link, shown in ((good, good), ("javascript:alert(1)//invite#" + "a" * 43, ""),
                            ("https://evil.example/x/invite#" + "a" * 43, ""), (good + '"<', "")):
            self.assertEqual(self.js("invite.shownLink(data[0])", link), shown)
        self.assertEqual(invite_page.PAGE_PATH, "/invite")
        self.assertIn("cancel invite link 1a2b3c4d", self.js("invite.aboutText(1, '1a2b3c4d')"))
        self.assertIn("within 1 day.", self.js("invite.aboutText(1, 'x')"))


if __name__ == "__main__":
    unittest.main()
