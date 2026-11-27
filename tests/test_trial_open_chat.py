"""The Trial agent card's "Open its chat again" opens the trial chat (GPT whole-batch review: it said "Done" and left
the visitor where they were): @pm returns the chat's room, and the panel shows its link, opened at once inside Element.
Run: .venv/bin/python -m unittest tests.test_trial_open_chat"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_trial_agent import NEW
from tests.test_trial_flow import TrialBase

STATIC = Path(__file__).resolve().parent.parent / "static"
NODE = shutil.which("node")
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, embedded] = process.argv.slice(1);   // node -e: no script path
const els = {msg: {textContent: "Done.", innerHTML: ""}, trialchat: {clicked: 0, click() { this.clicked++; }}};
const ctx = {encodeURIComponent, document: {getElementById: id => els[id], addEventListener() {}},
             esc: s => String(s).replace(/[&<>"]/g, c => "&#" + c.charCodeAt(0) + ";")};
vm.createContext(ctx);
vm.runInContext(`var embedded = ${embedded}, canNavigate = true;` + fs.readFileSync(file, "utf8"), ctx);
ctx.openChat("!trial:example");
process.stdout.write(JSON.stringify({html: els.msg.innerHTML, clicked: els.trialchat.clicked}));
"""


class AgainOpensTheChat(TrialBase):
    def test_the_button_returns_the_trial_chat_each_time(self):
        first = self.run_(self.lp.trial_action(NEW))
        self.assertEqual(first["result"], "ok")
        self.assertTrue(first["chat"])
        again = self.run_(self.lp.trial_action(NEW, "trial", "again"))
        self.assertEqual(again, {"result": "ok", "chat": first["chat"]})   # the same chat, not a new one

    def test_the_panel_hands_the_room_to_open_chat(self):
        self.assertIn("else if (r.chat && typeof openChat", (STATIC / "panel.html").read_text())


@unittest.skipIf(NODE is None, "node not installed")
class PanelOpensIt(unittest.TestCase):
    def run_js(self, embedded):
        out = subprocess.run([NODE, "-e", HARNESS, str(STATIC / "panel_agents.js"), embedded],
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_inside_element_it_opens_at_once(self):
        got = self.run_js("true")
        self.assertIn('href="https://matrix.to/#/!trial%3Aexample"', got["html"])
        self.assertIn("data-pmchat", got["html"])                    # panel.html's navigate handler opens it
        self.assertIn("Open the Trial agent chat", got["html"])
        self.assertEqual(got["clicked"], 1)

    def test_in_a_browser_tab_it_is_a_link_to_click(self):
        self.assertEqual(self.run_js("false")["clicked"], 0)          # never a pop-up the browser would block


if __name__ == "__main__":
    unittest.main()
