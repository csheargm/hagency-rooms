"""Your cards' "Update my agent" (pm_cards.OLD_CODE): the panel's button, clicked in node on the real script.

Owner, 2026-11-25: "Your agent runs older code. Run its connect line…" gave no button to fix it.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from common import registry
from server import pm_cards

ROOT = Path(__file__).resolve().parent.parent

UPDATE_HARNESS = r"""
const vm = require("vm");
const reply = JSON.parse(process.argv[2]);
const els = {}, handlers = {};
const mk = id => ({id, style: {}, textContent: "", innerHTML: "", dataset: {}, disabled: false,
  querySelector: () => null, querySelectorAll: () => [], appendChild() {}});
const update = Object.assign(mk("btn"), {dataset: {card: "update"}});
update.closest = s => s.startsWith("button") ? update : null;
const el = id => els[id] || (els[id] = id === "cardupdate" ? Object.assign(mk(id), {querySelector: () => update}) : mk(id));
const root = Object.assign(mk("cards"), {querySelector: s => s === "#cardsctl" ? (root.drawn ? {} : null) : el("cardlist")});
Object.defineProperty(root, "innerHTML", {set(v) { root.drawn = true; }, get() { return ""; }});
els.cards = root;
const posted = [];
const ctx = {JSON, Object, Number, Math, Set, Promise, setTimeout: () => 0, room: "!r", refresh() {},
  post: (url, body) => { posted.push(body); return new Promise(r => setImmediate(() => r(reply))); },
  document: {getElementById: el, querySelector: () => mk("make"), addEventListener: (t, f) => { handlers[t] = f; }}};
ctx.window = ctx; ctx.addEventListener = () => {};
vm.createContext(ctx); vm.runInContext(process.argv[1], ctx);
const view = {cards: {items: [], max: 6, making: null, changing: null, error: "Your agent runs older code and needs an update first.",
  agent_here: true, unable: null, update: "anna-agent", failed_at: 1, data: {}}, mode: "off"};
ctx.showCards(view);
const shown = els.cardupdate.style.display;
handlers.click({target: update}); handlers.click({target: update});      // a double click
setImmediate(() => setImmediate(() => {
  handlers.click({target: update});                                       // again, after it was sent, before a redraw
  const early = els.cardupdate.style.display;
  ctx.showCards(view);
  const after = els.cardupdate.style.display;
  ctx.showCards({...view, cards: {...view.cards, failed_at: 2}});        // the same agent fails again: a new button
  process.stdout.write(JSON.stringify({shown, posted, early, after, again: els.cardupdate.style.display,
                                       says: els.cardsays.textContent})); }));
"""


class UpdateButton(unittest.TestCase):
    JS = (ROOT / "static" / "panel_cards.js").read_text()

    def click(self, reply: dict) -> dict:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        done = subprocess.run([node, "-e", UPDATE_HARNESS, self.JS, json.dumps(reply)], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return json.loads(done.stdout)

    def test_update_my_agent_asks_pm_for_that_agents_connect_page_once(self):
        got = self.click({"result": "ok", "sent": True, "says": "Product Manager is sending you its Connect page."})
        self.assertEqual(got["shown"], "")                               # shown with the old-code notice
        self.assertEqual(got["posted"], [{"room": "!r", "action": "update", "value": "", "agent": "anna-agent"}])   # one code
        self.assertEqual(got["early"], "none")                           # sent: gone at once, no second code
        self.assertEqual(got["after"], "none")                           # sent: @pm's answer instead
        self.assertEqual(got["again"], "")                               # a new failure brings it back
        self.assertIn("Connect page", got["says"])

    def test_a_failure_keeps_the_button_to_try_again(self):
        got = self.click({"result": "I couldn't open a private chat with just you; please try again"})
        self.assertEqual(got["after"], "")
        self.assertEqual(len(got["posted"]), 2)                          # the later click could try again



ANNA, AGENT = "@anna:hs", "@anna-agent:hs"
OLD = {"error": pm_cards.OLD_CODE, "from": AGENT}


class WhichAgent(unittest.TestCase):
    """GPT review: the button is for the agent that answered with old code, on its owner's computer."""
    def update(self, job, agent=AGENT, kind=registry.REMOTE):
        with mock.patch.object(registry, "find", lambda person, lp: {"agent": kind} if person == ANNA else None):
            return pm_cards.update_for(ANNA, agent, job)

    def test_the_agent_that_answered_on_its_owners_computer(self):
        self.assertEqual(self.update(OLD), "anna-agent")

    def test_not_another_agent_picked_later(self):
        self.assertIsNone(self.update(OLD, agent="@anna-codex-agent:hs"))

    def test_not_one_the_server_runs(self):
        self.assertIsNone(self.update(OLD, kind="codex"))

    def test_not_for_other_failures(self):
        self.assertIsNone(self.update({"error": "Your agent couldn't make it: timed out", "from": AGENT}))
        self.assertIsNone(self.update(None))


if __name__ == "__main__":
    unittest.main()
