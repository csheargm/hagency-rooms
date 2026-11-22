"""local_signup.py setup on the local server: it restarts local/'s Synapse (the Compose file moved
there, ADR 0012), and a restart that fails leaves the settings unapplied, so a rerun tries again.
Run: .venv/bin/python -m unittest tests.test_local_signup"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import local_signup
from tests import ROOT


class Setup(unittest.TestCase):
    def setUp(self):
        self.config = Path(tempfile.mkdtemp()) / "homeserver.yaml"
        self.config.write_text("server_name: bridge.localhost\n")
        for p in (mock.patch.object(local_signup, "CONFIG", self.config),
                  mock.patch.object(local_signup, "configure", lambda text: text + "enable_registration: true\n")):
            p.start()
            self.addCleanup(p.stop)

    def test_it_restarts_the_stack_in_local(self):
        compose = ROOT / "local" / "docker-compose.yml"
        self.assertTrue(compose.is_file())
        with mock.patch.object(local_signup.subprocess, "run") as run, \
                mock.patch.object(local_signup.urllib.request, "urlopen", mock.MagicMock()):
            self.assertTrue(local_signup.setup())
        for call in run.call_args_list:
            self.assertEqual(call.args[0][:4], ["docker", "compose", "-f", str(compose)])

    def test_a_failed_restart_is_said_and_tried_again_next_time(self):
        """GPT review: the settings were written before the restart failed, so a rerun said "already set up"."""
        before = self.config.read_text()
        with mock.patch.object(local_signup.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, ["docker"])), \
                self.assertRaisesRegex(SystemExit, "couldn't restart Synapse"):
            local_signup.setup()
        self.assertEqual(self.config.read_text(), before)
        with mock.patch.object(local_signup.subprocess, "run"), \
                mock.patch.object(local_signup.urllib.request, "urlopen", mock.MagicMock()):
            self.assertTrue(local_signup.setup())               # still to do, so done now


if __name__ == "__main__":
    unittest.main()
