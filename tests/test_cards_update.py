"""Your cards' "Update my agent" (pm_cards.OLD_CODE): the panel's button, clicked in node on the real script.

Owner, 2026-11-25: "Your agent runs older code. Run its connect line…" gave no button to fix it.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UPDATE_HARNESS = r"""
const vm = require("vm");
const els = {}, handlers = {};
const mk = id => ({id, style: {}, textContent: "", innerHTML: "", dataset: {}, disabled: false,
  querySelector: () => null, querySelectorAll: () => [], appendChild() {}});
const el = id => els[id] || (els[id] = mk(id));
const root = Object.assign(mk("cards"), {querySelector: s => s === "#cardsctl" ? (root.drawn ? {} : null) : el("cardlist")});
Object.defineProperty(root, "innerHTML", {set(v) { root.drawn = true; }, get() { return ""; }});
els.cards = root;
const update = Object.assign(mk("btn"), {dataset: {card: "update"}, closest: s => s.startsWith("button") ? update : null});
let posted = null;
const ctx = {JSON, Object, Number, Math, Set, Promise, setTimeout: () => 0, room: "!r", refresh() {},
  post: async (url, body) => { posted = body; return {result: "Its Connect page is on its way."}; },
  document: {getElementById: el, querySelector: () => mk("make"), addEventListener: (t, f) => { handlers[t] = f; }}};
ctx.window = ctx; ctx.addEventListener = () => {};
vm.createContext(ctx); vm.runInContext(process.argv[1], ctx);
const view = {cards: {items: [], max: 6, making: null, changing: null, error: "Your agent runs older code and needs an update first.",
  agent_here: true, unable: null, update: "anna-agent", data: {}}, mode: "off"};
ctx.showCards(view);
const shown = els.cardupdate.style.display;
handlers.click({target: update});
setImmediate(() => { ctx.showCards(view); process.stdout.write(JSON.stringify([shown, posted, els.cardupdate.style.display])); });
"""



class UpdateButton(unittest.TestCase):
    JS = (ROOT / "static" / "panel_cards.js").read_text()

    def test_update_my_agent_asks_pm_for_that_agents_connect_page(self):
        """Owner: "Your agent runs older code" gave no button to fix it. Clicked in node, on the real script."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed")
        done = subprocess.run([node, "-e", UPDATE_HARNESS, self.JS], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        shown, posted, hidden_after = json.loads(done.stdout)
        self.assertEqual(shown, "")                                     # the button shows with the old-code notice
        self.assertEqual(posted, {"room": "!r", "action": "update", "value": "", "agent": "anna-agent"})
        self.assertEqual(hidden_after, "none")                          # then @pm's answer shows instead



if __name__ == "__main__":
    unittest.main()
