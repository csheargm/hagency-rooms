"""The server (@pm, the console, the panel) never imports the agent bridge: what both need (where
Synapse is, the server name, the repo folder, stale-event checks) lives in bridge_config.py and
lifecycle.py. This keeps the coming split into server/ and adapters/agents/ a pure move."""
import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE_TIMEOUT_S = 60
SERVER_PREFIXES = ("pm_", "console", "loop", "panel", "connect_page", "connect_status", "fed_", "routing")
AGENT_BRIDGE = {"bridge", "bridge_custom", "bridge_followup", "bridge_voice"}


def imported_modules(path: Path) -> set[str]:
    """Every top-level module name `path` imports, at any depth in the file (lazy imports too)."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class ServerDoesNotImportBridge(unittest.TestCase):
    def test_no_server_module_imports_the_bridge(self):
        server = [p for p in ROOT.glob("*.py") if p.name.startswith(SERVER_PREFIXES)]
        self.assertGreater(len(server), 30)          # the prefixes still find the server's files
        offenders = {p.name: sorted(imported_modules(p) & AGENT_BRIDGE) for p in server}
        self.assertEqual({}, {k: v for k, v in offenders.items() if v})

    def test_server_process_never_loads_the_bridge(self):
        """GPT review: the name scan misses helpers like ownership.py that loop.py imports. A fresh
        process importing @pm's entry points must end with no bridge module loaded."""
        probe = ("import sys, loop, console, panel; "
                 f"print(sorted(m for m in sys.modules if m in {sorted(AGENT_BRIDGE)!r}))")
        env = {k: v for k, v in os.environ.items() if k != "HAGENCY_CREDENTIALS"}
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=PROBE_TIMEOUT_S)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual("[]", out.stdout.strip())

    def test_the_check_sees_lazy_imports(self):
        src = ROOT / "console_personal.py"           # imports bridge_config inside a function
        self.assertIn("bridge_config", imported_modules(src))


if __name__ == "__main__":
    unittest.main()
