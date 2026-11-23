"""HAGENCY_TEST_LIMITS: higher per-person limits while testing (common/test_limits.py), owner's choice 2026-11-23."""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from common import test_limits

ROOT = Path(__file__).resolve().parents[1]
LIMITS = ("server.pm_projects.ROOMS_PER_HOUR", "server.pm_projects.LOOKUPS_PER_HOUR", "server.pm_welcome.CODES_PER_HOUR",
          "server.pm_github.STARTS_PER_HOUR", "server.pm_find.FINDS_PER_HOUR", "server.pm_joining.ASKS_PER_HOUR",
          "server.pm_rename.RENAMES_PER_HOUR", "common.registry.PER_PERSON_DEFAULT")
KEPT = ("server.pm_github.SERVER_STARTS_PER_HOUR", "server.pm_joining.ALL_ASKS_PER_HOUR")   # server-wide: never raised


def values(switch: str | None) -> dict:
    """The limits as a fresh @pm process sees them (they are read once, at import)."""
    env = {k: v for k, v in os.environ.items() if k != test_limits.SWITCH}
    if switch is not None:
        env[test_limits.SWITCH] = switch
    code = "import importlib\n" + "".join(
        f"m = importlib.import_module({name.rsplit('.', 1)[0]!r}); print({name!r}, getattr(m, {name.rsplit('.', 1)[1]!r}))\n"
        for name in LIMITS + KEPT)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    return {line.split()[0]: int(line.split()[1]) for line in out.splitlines()}


class Switch(unittest.TestCase):
    def test_only_exactly_1_turns_it_on(self):
        for value, on in (("1", True), (" 1 ", True), ("0", False), ("", False), ("yes", False), ("true", False)):
            with mock.patch.dict(os.environ, {test_limits.SWITCH: value}):
                self.assertEqual(test_limits.on(), on, value)
                self.assertEqual(test_limits.pick(3, 20), 20 if on else 3)

    def test_on_raises_every_per_person_limit_and_no_server_wide_one(self):
        public, testing = values(None), values("1")
        self.assertEqual(values("0"), public)
        self.assertEqual((public["server.pm_projects.ROOMS_PER_HOUR"], testing["server.pm_projects.ROOMS_PER_HOUR"]), (3, 20))
        self.assertEqual((public["common.registry.PER_PERSON_DEFAULT"], testing["common.registry.PER_PERSON_DEFAULT"]), (3, 5))
        for name in LIMITS:
            self.assertGreater(testing[name], public[name], name)
        for name in KEPT:
            self.assertEqual(testing[name], public[name], name)


class Deploy(unittest.TestCase):
    def test_the_container_gets_the_switch_off_by_default_and_envfile_accepts_it(self):
        compose = (ROOT / "deploy" / "docker-compose.yml").read_text()
        self.assertIn("HAGENCY_TEST_LIMITS: ${HAGENCY_TEST_LIMITS:-0}", compose)
        sys.path.insert(0, str(ROOT / "deploy"))
        import envfile
        self.assertTrue(re.fullmatch(envfile.RULES["HAGENCY_TEST_LIMITS"], "1"))
        self.assertFalse(re.fullmatch(envfile.RULES["HAGENCY_TEST_LIMITS"], "2"))
        self.assertIn("HAGENCY_TEST_LIMITS", envfile.OPTIONAL)
        self.assertIn("HAGENCY_TEST_LIMITS=0", (ROOT / "deploy" / ".env.example").read_text())


if __name__ == "__main__":
    unittest.main()
