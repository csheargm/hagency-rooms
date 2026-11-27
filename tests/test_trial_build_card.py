"""The Server agent card's "Try a build" part (static/panel_agents.js, ADR 0027): shown only when @pm sends it,
with what it does, where the code goes, and the button disabled while a build of theirs waits or runs.
Run: .venv/bin/python -m unittest tests.test_trial_build_card"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
NODE = shutil.which("node")
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const [file, card] = process.argv.slice(1);
const ctx = {document: {getElementById: () => null, addEventListener() {}},
             esc: s => String(s).replace(/[&<>"]/g, c => "&#" + c.charCodeAt(0) + ";")};
vm.createContext(ctx);
vm.runInContext(`var embedded = false, canNavigate = false;` + fs.readFileSync(file, "utf8"), ctx);
process.stdout.write(JSON.stringify(ctx.trialCard(JSON.parse(card))));
"""
TRIAL = {"on": True, "left": 5, "per_day": 50, "model": "glm-5.3", "models": ["glm-5.3"]}


@unittest.skipIf(NODE is None, "node not installed")
class BuildCard(unittest.TestCase):
    def card(self, build=None):
        trial = {**TRIAL, **({"build": build} if build is not None else {})}
        out = subprocess.run([NODE, "-e", HARNESS, str(STATIC / "panel_agents.js"), json.dumps(trial)],
                             capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_no_build_part_while_builds_are_off(self):
        self.assertNotIn("trial_build", self.card())

    def test_the_button_says_what_it_does_and_where_code_goes(self):
        html = self.card({"left": 3, "per_day": 3, "github": True, "days": 30, "state": None})
        self.assertIn('data-a="trial_build"', html)
        self.assertIn("for a person to merge", html)
        self.assertIn("E2B", html)
        self.assertIn("3 of 3 builds left today", html)
        self.assertNotIn("disabled", html)

    def test_needs_github_and_waits_while_one_is_queued(self):
        html = self.card({"left": 3, "per_day": 3, "github": False, "days": 30, "state": "queued #2"})
        self.assertIn("Needs GitHub connected (an account at least 30 days old)", html)
        self.assertIn("<b>queued #2</b>", html)
        self.assertIn(" disabled>Try a build", html)


if __name__ == "__main__":
    unittest.main()
