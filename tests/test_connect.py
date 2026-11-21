"""Unit tests: an agent run from a person's own computer (connect.py, bridge_config.py), and the
server leaving it alone (supervise.py, setup_accounts.py). Run: .venv/bin/python -m unittest tests.test_connect"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import bridge_config
from client import connect
from adapters.agents import hermes_profile
from common import registry
from client import supervise
from tests import ROOT

CREDS = {"homeserver": "https://matrix.example.org", "user_id": "@newbie1-agent:example.org",
         "password": "s3cret", "owner": "newbie1"}


def write(data) -> str:
    path = Path(tempfile.mkdtemp()) / "credentials.json"
    path.write_text(json.dumps(data))
    return str(path)


class Config(unittest.TestCase):
    def test_no_file_means_this_server(self):
        self.assertEqual(bridge_config.load_credentials(None), {})

    def test_a_good_file_is_read(self):
        self.assertEqual(bridge_config.load_credentials(write(CREDS))["owner"], "newbie1")

    def test_bad_files_stop_with_a_plain_message(self):
        for bad in ({**CREDS, "password": ""}, {**CREDS, "owner": "mallory"}, [1], "x"):
            with self.assertRaises(SystemExit):
                bridge_config.load_credentials(write(bad))
        with self.assertRaises(SystemExit):
            bridge_config.load_credentials("/nonexistent/credentials.json")

    def test_a_bridge_started_with_the_file_uses_its_server_and_password(self):
        code = ("from adapters.agents import bridge; from common import bridge_config as c; "
                "print(c.HOMESERVER, c.SERVER_NAME, bridge.Bridge('newbie1', 'codex').owner_id, c.agent_password('newbie1-agent'))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             cwd=ROOT, env={**os.environ, "HAGENCY_CREDENTIALS": write(CREDS)}).stdout
        self.assertEqual(out.split(), ["https://matrix.example.org", "example.org", "@newbie1:example.org", "s3cret"])

    def test_the_file_is_only_for_its_owner(self):
        with mock.patch.object(bridge_config, "CREDENTIALS", CREDS):
            with self.assertRaises(SystemExit):
                bridge_config.agent_password("anna-agent")


class Connect(unittest.TestCase):
    def test_passwords_travel_over_https_or_stay_on_this_computer(self):
        for ok in ("https://rooms.example.org", "http://localhost:8650", "http://127.0.0.1:8650"):
            self.assertTrue(connect.safe_url(ok), ok)
        for bad in ("http://rooms.example.org", "ftp://x", "__HAGENCY_CONSOLE__", ""):
            self.assertFalse(connect.safe_url(bad), bad)

    def test_saved_only_for_me(self):
        path = Path(tempfile.mkdtemp()) / "h" / "credentials.json"
        connect.save(path, CREDS)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        connect.save(path, {**CREDS, "agent": "codex"})          # again: replaced whole, still only mine
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["credentials.json"])
        self.assertEqual(json.loads(path.read_text()), {**CREDS, "agent": "codex"})

    def test_open_folder_note_only_where_mode_bits_mean_something(self):
        """Windows has no mode bits (connect.ps1 sets the folder's ACL): no misleading note there."""
        folder = Path(tempfile.mkdtemp())
        folder.chmod(0o755)
        for name, noted in (("posix", True), ("nt", False)):
            with mock.patch.object(connect.os, "name", name), mock.patch("builtins.print") as out:
                connect.save(folder / "credentials.json", CREDS)
            self.assertEqual(any("can be read by others" in str(c.args[0]) for c in out.call_args_list), noted)

    def test_which_cli(self):
        with mock.patch.object(connect.cli_path.shutil, "which", lambda b: True):
            self.assertEqual(connect.pick_backend("claude", "codex"), "claude")
            self.assertEqual(connect.pick_backend(None, "codex"), "codex")
        with mock.patch.object(connect.cli_path.shutil, "which", lambda b: None), self.assertRaises(SystemExit):
            connect.pick_backend("claude", None)                 # chosen, but not installed: before the code is used
        with mock.patch.object(connect.cli_path.shutil, "which", lambda b: b == "claude"):
            self.assertEqual(connect.pick_backend(None, None), "claude")
        with mock.patch.object(connect.cli_path.shutil, "which", lambda b: None), self.assertRaises(SystemExit):
            connect.pick_backend(None, None)
        with mock.patch.object(connect.cli_path.shutil, "which", lambda b: True), \
                mock.patch.object(connect.sys.stdin, "isatty", lambda: True):
            answers = iter(["hermes", "Codex"])
            self.assertEqual(connect.pick_backend(None, None, ask=lambda _: next(answers)), "codex")

    def test_the_cli_is_chosen_before_the_code_is_used_up(self):
        """GPT review 21: no CLI installed must not burn the one-time code."""
        home = tempfile.mkdtemp()
        with mock.patch.object(sys, "argv", ["connect.py", "ABCDE-FGHJK", "--home", home]), \
                mock.patch.object(connect.cli_path.shutil, "which", lambda b: None), \
                mock.patch.object(connect, "redeem") as redeem, self.assertRaises(SystemExit):
            connect.main()
        redeem.assert_not_called()

    def test_an_odd_answer_from_the_server_is_not_saved(self):
        for bad in ({**CREDS, "homeserver": "http://evil.example"}, {**CREDS, "user_id": "@anna:example.org"},
                    {**CREDS, "owner": "anna"}, {"error": "x"}):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(bad).encode()
            with mock.patch.object(connect.urllib.request, "urlopen", return_value=response), self.assertRaises(SystemExit):
                connect.redeem("http://localhost:8650", "ABCDE-FGHJK")


class ServerLeavesItAlone(unittest.TestCase):
    def test_supervisor_skips_remote_agents(self):
        with mock.patch.object(registry, "load", return_value={"newbie1": {"agent": "remote"}, "betty": {"agent": "claude"}}), \
                mock.patch.object(hermes_profile, "set_repos"):
            names = [k.name for k in supervise.children("codex", False)]
        self.assertEqual(names, ["betty-agent", "pm"])

    def test_people_who_signed_up_themselves_stay_out_of_setup(self):
        reg = Path(tempfile.mkdtemp()) / "agents.json"
        reg.write_text(json.dumps({"anna": {}, "newbie1": {"agent": "remote"}}))
        code = "from common import setup_accounts as s; print(' '.join(s.USERS))"
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             cwd=ROOT, env={**os.environ, "HAGENCY_AGENTS": str(reg)}).stdout
        self.assertEqual(out.split(), ["anna", "anna-agent", "pm"])


if __name__ == "__main__":
    unittest.main()
