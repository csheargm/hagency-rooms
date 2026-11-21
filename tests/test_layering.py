"""The repo's layout rules (docs/adr/0011-repo-layout.md):

  common/            imports only common/
  adapters/agents/   imports only adapters/ and common/
  server/, client/   never import each other (both may use adapters/ and common/)
  mcp_tools/         imports neither server/ nor client/
  the root           only launchers, under the paths installed agents and services already name

and @pm's process never loads the agent adapter or anything of client/.
"""
import ast
import os
import re
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE_TIMEOUT_S = 60
PACKAGES = ("common", "adapters", "server", "client", "mcp_tools")
MAY_IMPORT = {
    "common": {"common"},
    "adapters": {"adapters", "common"},
    "server": {"server", "adapters", "common"},
    "client": {"client", "adapters", "common"},
    "mcp_tools": {"mcp_tools", "adapters", "common"},
}
LAUNCHER_RE = re.compile(r'runpy\.run_module\("(?P<mod>[a-z_.]+)", run_name="__main__", alter_sys=True\)')


def imported_modules(path: Path) -> set[str]:
    """Every module `path` imports, fully dotted, at any depth in the file (lazy imports too)."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def code_files(package: str) -> list[Path]:
    return sorted((ROOT / package).rglob("*.py"))


class Layout(unittest.TestCase):
    def test_each_package_imports_only_what_it_may(self):
        offenders = {}
        for package, allowed in MAY_IMPORT.items():
            for path in code_files(package):
                bad = sorted(m for m in imported_modules(path)
                             if m.split(".")[0] in PACKAGES and m.split(".")[0] not in allowed)
                if bad:
                    offenders[str(path.relative_to(ROOT))] = bad
        self.assertEqual({}, offenders)

    def test_the_check_sees_lazy_imports(self):
        src = ROOT / "server" / "console_personal.py"     # imports bridge_config inside a function
        self.assertIn("common.bridge_config", imported_modules(src))

    def test_nothing_imports_a_moved_module_by_its_old_bare_name(self):
        """`import registry` would load nothing (or a launcher): every import names the package."""
        moved = {p.stem for package in PACKAGES for p in code_files(package)} - {"__init__"}
        # only the official source (GPT review: custom/<owner>/ holds owners' own older versions)
        files = [p for d in (*PACKAGES, "tests", "deploy") for p in (ROOT / d).rglob("*.py")]
        offenders = {str(p.relative_to(ROOT)): sorted(imported_modules(p) & moved) for p in files}
        self.assertEqual({}, {k: v for k, v in offenders.items() if v})

    def test_module_names_are_unique_across_packages(self):
        """customize.SENSITIVE matches by file name: two files called the same would blur it."""
        names = Counter(p.name for package in PACKAGES for p in code_files(package) if p.name != "__init__.py")
        self.assertEqual([], [n for n, c in names.items() if c > 1])

    def test_the_root_holds_only_launchers_and_each_starts_real_code(self):
        for path in ROOT.glob("*.py"):
            m = LAUNCHER_RE.search(path.read_text())
            self.assertIsNotNone(m, f"{path.name} is not a launcher")
            target = ROOT / (m["mod"].replace(".", "/") + ".py")
            self.assertTrue(target.is_file(), f"{path.name} starts {m['mod']}, which doesn't exist")
            self.assertEqual(path.stem, target.stem)       # same name as the code it starts
            self.assertIn('if __name__ == "__main__":', target.read_text())


class PmProcess(unittest.TestCase):
    def test_pm_never_loads_the_bridge_or_the_client(self):
        """A fresh process importing @pm's entry points: no agent adapter module, nothing of client/
        (GPT review: a scan by file name missed helpers that loop imports)."""
        probe = ("import sys; from server import loop, console, panel; "
                 "print(sorted(m for m in sys.modules if m.startswith(('client', 'adapters.agents.bridge'))))")
        env = {k: v for k, v in os.environ.items() if k != "HAGENCY_CREDENTIALS"}
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True,
                             text=True, timeout=PROBE_TIMEOUT_S)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual("[]", out.stdout.strip())


if __name__ == "__main__":
    unittest.main()
