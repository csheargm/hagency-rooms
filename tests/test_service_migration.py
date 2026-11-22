"""Installed agents keep running after the launchers moved into bin/ (docs/adr/0012-launchers-in-bin.md).

A background service written by an older connect.py names <app>/bridge.py (or hermes_local.py,
or on Windows connect_win.py); after the update those are gone. The next connect run rewrites
every such service of this installation to bin/ and loads it again (agent_service.migrate), and
Windows' stop still recognises a keeper started from the old path.
Run: .venv/bin/python -m unittest tests.test_service_migration"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client import agent_service
from client import connect
from tests import ROOT

APP = Path("/Users/a b/.hagency/app")        # a space, as a real home may have
OTHER = Path("/Users/a b/elsewhere/app")     # another installation's services are not ours to touch
CREDS = Path("/Users/a b/.hagency/credentials.json")
LOGS = CREDS.parent / "logs"


def old_command(app: Path, owner: str, backend: str) -> list[str]:
    """What connect.py wrote before ADR 0012: the launchers at the installation's root."""
    if backend == "hermes":
        return [sys.executable, str(app / "hermes_local.py")]
    return [sys.executable, str(app / "bridge.py"), owner, "--agent", backend]


def old_service(how: str, app: Path, backend: str, agent: str) -> str:
    write = agent_service.plist if how == "launchd" else agent_service.unit
    with mock.patch.object(agent_service, "command", old_command):
        return write(app, "anna", backend, agent, CREDS, LOGS)


class Recorder:
    def __init__(self, code: int = 0):
        self.calls, self.code = [], code

    def __call__(self, args, **_):
        self.calls.append(args)
        return SimpleNamespace(returncode=self.code, stdout="", stderr="Bootstrap failed: 5" if self.code else "")


class Migrate(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def place(self, how: str, agent: str, text: str) -> Path:
        path = agent_service.service_file(how, agent, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def migrate(self, how: str, run: Recorder) -> list[str]:
        with mock.patch.object(agent_service, "kind", return_value=how):
            return agent_service.migrate(APP, run=run, home=self.home)

    def test_an_old_launchd_service_is_rewritten_and_loaded_again(self):
        old = self.place("launchd", "anna-agent", old_service("launchd", APP, "codex", "anna-agent"))
        hermes = self.place("launchd", "anna-hermes-agent", old_service("launchd", APP, "hermes", "anna-hermes-agent"))
        run = Recorder()
        self.assertEqual(sorted(self.migrate("launchd", run)), sorted([old.name, hermes.name]))
        self.assertEqual(old.read_text(), agent_service.plist(APP, "anna", "codex", "anna-agent", CREDS, LOGS))
        self.assertEqual(hermes.read_text(), agent_service.plist(APP, "anna", "hermes", "anna-hermes-agent", CREDS, LOGS))
        self.assertIn(f"<string>{APP}/bin/bridge.py</string>", old.read_text())
        self.assertEqual([c[1] for c in run.calls], ["bootout", "bootstrap"] * 2)   # launchd keeps the loaded copy
        self.assertEqual(self.migrate("launchd", Recorder()), [])                    # again: nothing left to do

    def test_an_old_systemd_unit_is_rewritten_with_its_quoting_kept(self):
        app = Path("/home/a%h$x/.hagency/app")          # systemd's % and $ stay doubled in the new path too
        with mock.patch.object(agent_service, "command", old_command):
            text = agent_service.unit(app, "anna", "codex", "anna-agent", CREDS, LOGS)
        unit = self.place("systemd", "anna-agent", text)
        run = Recorder()
        with mock.patch.object(agent_service, "kind", return_value="systemd"):
            self.assertEqual(agent_service.migrate(app, run=run, home=self.home), [unit.name])
        self.assertEqual(unit.read_text(), agent_service.unit(app, "anna", "codex", "anna-agent", CREDS, LOGS))
        self.assertIn('"/home/a%%h$$x/.hagency/app/bin/bridge.py"', unit.read_text())
        self.assertEqual(run.calls, [["systemctl", "--user", "daemon-reload"]])

    def test_new_services_and_other_installations_are_left_alone(self):
        new = self.place("launchd", "anna-agent", agent_service.plist(APP, "anna", "codex", "anna-agent", CREDS, LOGS))
        other = self.place("launchd", "cx-codex-agent", old_service("launchd", OTHER, "codex", "cx-codex-agent"))
        before = (new.read_text(), other.read_text())
        run = Recorder()
        self.assertEqual(self.migrate("launchd", run), [])
        self.assertEqual((new.read_text(), other.read_text()), before)
        self.assertEqual(run.calls, [])

    def test_a_refused_reload_is_said_not_swallowed(self):
        self.place("launchd", "anna-agent", old_service("launchd", APP, "codex", "anna-agent"))
        with self.assertRaisesRegex(RuntimeError, "Bootstrap failed"):
            self.migrate("launchd", Recorder(code=5))

    def test_windows_and_no_service_manager_have_nothing_to_rewrite(self):
        for how in (agent_service.WINDOWS, None):
            run = Recorder()
            self.assertEqual(self.migrate(how, run), [])
            self.assertEqual(run.calls, [])


class WindowsKeeper(unittest.TestCase):
    def test_stop_still_recognises_a_keeper_started_from_the_old_path(self):
        app = Path("/app")
        for keeper in (app / "bin" / "connect_win.py", app / "connect_win.py", app / "bridge.py"):
            line = f'C:\\py\\pythonw.exe|"C:\\py\\pythonw.exe" "{keeper}" --home "C:\\h"'
            run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=line, stderr="")
            self.assertTrue(agent_service._ours("101", app, run), keeper)

    def test_the_windows_installer_knows_both_keeper_paths(self):
        ps1 = (ROOT / "client" / "connect.ps1").read_text()
        self.assertIn("$Keepers = @((Join-Path $App 'bin\\connect_win.py'), (Join-Path $App 'connect_win.py'))", ps1)
        self.assertIn("$runner = Join-Path $App 'bin\\connect_win.py'", ps1)   # new tasks run the new one


class ConnectRun(unittest.TestCase):
    """The user flow: re-running the connect line (git pull, then connect.py) fixes the old services first."""
    def run_connect(self, *argv, migrate):
        home = Path(tempfile.mkdtemp())
        said, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["connect.py", "--home", str(home), *argv]), \
                mock.patch.object(agent_service, "migrate", migrate), redirect_stdout(said), redirect_stderr(err):
            try:
                connect.main()
                ended = None
            except SystemExit as e:
                ended = str(e)
        return said.getvalue(), err.getvalue(), ended

    def test_migration_runs_before_anything_else_even_a_failing_run(self):
        migrate = mock.Mock(return_value=["xyz.hagencylabs.agent.anna-agent.plist"])
        said, _, ended = self.run_connect(migrate=migrate)          # not connected: it still fixed the services
        migrate.assert_called_once_with(connect.APP)
        self.assertIn("updated 1 background service(s)", said)
        self.assertIn("not connected yet", ended)

    def test_a_failed_migration_is_said_and_the_run_goes_on(self):
        migrate = mock.Mock(side_effect=RuntimeError("launchd wouldn't load x.plist again"))
        _, err, ended = self.run_connect("--stop", migrate=migrate)
        self.assertIn("couldn't update an older background service (launchd wouldn't load x.plist again)", err)
        self.assertIn("nothing to stop", ended)                      # the run went on to what was asked

    def test_the_stop_hint_names_the_new_path(self):
        self.assertEqual(connect.CONNECT, connect.APP / "bin" / "connect.py")
        self.assertTrue(connect.CONNECT.is_file())


if __name__ == "__main__":
    unittest.main()
