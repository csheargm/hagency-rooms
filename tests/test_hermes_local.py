"""Tests: the hermes kind on a person's own computer (hermes_local.py): what it checks before
running, the two profiles it writes from the saved sign-in (owner only / @pm only, the person's own
homeserver), and the service that keeps both gateways running and stops when the server
disconnects the agent. Hermes itself is never run. Run: .venv/bin/python -m unittest tests.test_hermes_local"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import bridge_config
from client import hermes_local
from adapters.agents import hermes_profile
from client import supervise
from adapters.agents import workdirs

CREDS = {"homeserver": "https://matrix.example.org", "user_id": "@newbie1-agent:example.org",
         "password": "s3cret-pw", "owner": "newbie1", "agent": "hermes"}


class Ready(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        patch = mock.patch.object(hermes_local, "hermes_home", lambda: self.home)
        patch.start()
        self.addCleanup(patch.stop)
        self.adapter = self.home / "hermes-agent" / hermes_local.ADAPTER

    def check(self, installed=True):
        with mock.patch.object(hermes_local.cli_path, "startable", lambda b: installed and b == "hermes"), \
                mock.patch.dict(os.environ, {"HAGENCY_HERMES_SRC": ""}):
            return hermes_local.problem()

    def test_each_missing_piece_is_named(self):
        self.assertIn("install Hermes Agent", self.check(installed=False))
        self.assertIn("hermes model", self.check())
        (self.home / "config.yaml").write_text("model:\n  provider: openrouter\n")
        self.assertIn("HAGENCY_HERMES_SRC", self.check())
        self.adapter.parent.mkdir(parents=True)
        self.adapter.write_text("async def _on_invite(self): await self._join_room_by_id(room_id)\n")
        self.assertIn(hermes_local.INVITE_PATCH, self.check())         # anyone could pull it into a room
        self.adapter.write_text("def _invite_allowed(self, room_id, inviter): ...\n")
        self.assertIsNone(self.check())

    def test_windows_is_told_plainly(self):
        with mock.patch.object(hermes_local.os, "name", "nt"):
            self.assertIn("WSL", hermes_local.problem())


class Profiles(unittest.TestCase):
    def setUp(self):
        home = Path(tempfile.mkdtemp())
        for name in ("newbie1room", "newbie1loop"):
            (home / "profiles" / name).mkdir(parents=True)
            (home / "profiles" / name / "SOUL.md").write_text("You are helpful.")
        (home / "config.yaml").write_text("model:\n  default: m\n  provider: openrouter\n")
        (home / ".env").write_text("OPENROUTER_API_KEY=model-key\n")
        self.folder = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(hermes_profile, "HERMES_HOME", home),
                  mock.patch.object(workdirs, "WORK_HOME", Path(tempfile.mkdtemp())),
                  mock.patch.object(bridge_config, "CREDENTIALS", CREDS),
                  mock.patch.object(bridge_config, "SERVER_NAME", "example.org"),
                  mock.patch.object(hermes_profile, "SERVER_NAME", "example.org"),
                  mock.patch.object(hermes_local, "problem", lambda: None),
                  mock.patch.dict(os.environ, {"HAGENCY_CREDENTIALS": str(self.folder / "credentials.json")})):
            p.start()
            self.addCleanup(p.stop)
        self.home = home

    def test_two_profiles_one_account_each_obeying_one_sender(self):
        self.assertEqual(hermes_local.setup(), ("newbie1room", "newbie1loop"))
        room = (self.home / "profiles" / "newbie1room" / ".env").read_text()
        loop = (self.home / "profiles" / "newbie1loop" / ".env").read_text()
        for env in (room, loop):
            self.assertIn("MATRIX_HOMESERVER=https://matrix.example.org\n", env)   # the person's server, not 127.0.0.1
            self.assertIn("MATRIX_USER_ID=@newbie1-agent:example.org\n", env)
        self.assertIn("MATRIX_ALLOWED_USERS=@newbie1:example.org\n", room)
        self.assertIn("MATRIX_ALLOWED_USERS=@pm:example.org\n", loop)
        cfg = (self.home / "profiles" / "newbie1loop" / "config.yaml").read_text()
        self.assertIn(f"HAGENCY_CREDENTIALS: '{self.folder / 'credentials.json'}'", cfg)   # its MCP server signs in from the file
        self.assertNotIn("s3cret-pw", cfg)                                 # the password only in the 0600 .env

    def test_a_person_from_another_server_is_obeyed_by_full_id(self):
        other = {**CREDS, "owner": "@alice:matrix.org"}
        with mock.patch.object(bridge_config, "CREDENTIALS", other), \
                mock.patch.object(bridge_config, "agent_handle", lambda owner_id: "alice-at-matrix-org-1a2b3c4d5e"), \
                mock.patch.object(hermes_profile, "configure_both") as both:
            hermes_local.setup()
        self.assertEqual(both.call_args.kwargs["owner_id"], "@alice:matrix.org")
        self.assertEqual(both.call_args.args[0], "alice-at-matrix-org-1a2b3c4d5e")

    def test_a_line_break_from_the_server_never_adds_settings(self):
        """GPT review: a password with a line break could add MATRIX_ALLOWED_USERS=* to .env."""
        for brk in ("\n", "\r", " ", "\x1c"):                     # GPT re-check: every kind of line break
            evil = {**CREDS, "password": f"pw{brk}MATRIX_ALLOWED_USERS=*"}
            with mock.patch.object(bridge_config, "CREDENTIALS", evil), self.assertRaises(SystemExit):
                hermes_local.setup()
            for name in ("newbie1room", "newbie1loop"):
                self.assertFalse((self.home / "profiles" / name / "config.yaml").exists())   # refused before any write
                self.assertFalse((self.home / "profiles" / name / ".env").exists())

    def test_an_older_env_file_becomes_only_mine(self):
        env = self.home / "profiles" / "newbie1room" / ".env"
        env.write_text("X=1\n")
        env.chmod(0o644)
        hermes_local.setup()
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)

    def test_quotes_in_yaml_values_are_kept_as_text(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/o'brien"}):
            hermes_local.setup()
        cfg = (self.home / "profiles" / "newbie1room" / "config.yaml").read_text()
        self.assertIn("HOME: '/home/o''brien'", cfg)

    def test_a_quote_in_the_path_is_refused(self):
        with mock.patch.dict(os.environ, {"HAGENCY_CREDENTIALS": "/home/o'brien/.hagency/credentials.json"}), \
                self.assertRaises(SystemExit):
            hermes_local.setup()


class Service(unittest.IsolatedAsyncioTestCase):
    def test_both_gateways_and_the_buttons_run(self):
        with mock.patch.object(hermes_profile, "profile_dir", lambda n: Path(tempfile.gettempdir())), \
                mock.patch.object(Path, "exists", lambda self: True), mock.patch.object(hermes_profile, "set_repos"):
            kids = supervise.hermes_children("newbie1", "newbie1", "connect.py again")
        self.assertEqual([k.argv[:3] for k in kids[:2]], [["hermes", "-p", "newbie1room"], ["hermes", "-p", "newbie1loop"]])
        self.assertEqual(kids[2].argv[1:], ["bin/bridge.py", "newbie1", "--buttons-only"])

    async def test_a_clean_exit_stops_it_instead_of_restarting(self):
        kid = supervise.Child("newbie1-buttons", ["x"])
        kid.on_clean_exit = asyncio.Event()
        kid.proc = mock.Mock(returncode=0)
        with mock.patch.object(kid, "start") as start:
            await supervise.revive(kid)
        self.assertTrue(kid.on_clean_exit.is_set())
        start.assert_not_called()
        kid.proc = mock.Mock(returncode=1)                                # a crash is still restarted
        kid.on_clean_exit = asyncio.Event()
        with mock.patch.object(kid, "start") as start, mock.patch.object(supervise.asyncio, "sleep", mock.AsyncMock()):
            await supervise.revive(kid)
        start.assert_called_once()
        self.assertFalse(kid.on_clean_exit.is_set())


if __name__ == "__main__":
    unittest.main()
