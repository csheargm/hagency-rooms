"""Loading a rewritten service again survives launchd's slow bootout, and one refusal doesn't stop the rest.

On a Mac the connect line said "launchd wouldn't load …-agent.plist again: Bootstrap failed: 5:
Input/output error" for the first service, alphabetically, and left the other four (further agents)
running the removed root bridge.py. bootout returns before the job is gone; a bootstrap meanwhile
fails with error 5. Now the load waits (bounded) until `launchctl print` no longer finds the job,
tries error 5 again, and migrate() moves every service it can and names the ones it couldn't.
Run: .venv/bin/python -m unittest tests.test_launchd_reload"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import agent_service
from client import service_migration
from tests.fake_launchd import FakeLaunchd

HOME = Path("/Users/a b")                    # a made-up home; nothing here is anyone's real service
APP = HOME / ".hagency" / "app"
OWNERS = "chrisxie-at-example-org-0123456789-agent"   # the owner's own agent sorts first, as on that Mac
FURTHER = ("cx-agent", "cx-codex-agent", "cx-codex2-agent", "cx-hermes-agent")


def further_plist(agent: str, launcher: str) -> str:
    """A further agent's plist as launchd lays it out: its own sign-in, the venv's Python, the root bridge."""
    name = agent.removesuffix("-agent")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>HAGENCY_CREDENTIALS</key>
\t\t<string>{HOME}/.hagency/{name}/credentials.json</string>
\t</dict>
\t<key>Label</key>
\t<string>{agent_service.LABEL}.{agent}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>{APP}/.venv/bin/python</string>
\t\t<string>{launcher}</string>
\t\t<string>cx</string>
\t\t<string>--agent</string>
\t\t<string>codex</string>
\t</array>
\t<key>WorkingDirectory</key>
\t<string>{APP}</string>
</dict>
</plist>
"""


class Reload(unittest.TestCase):
    """agent_service.load_again: bootout, wait until the job is gone, bootstrap (again on error 5)."""
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / f"{agent_service.LABEL}.cx-agent.plist"

    def load(self, launchd: FakeLaunchd):
        return agent_service.load_again(self.path, self.path.stem, launchd, launchd.sleep, launchd.clock)

    def test_the_load_waits_until_the_booted_out_job_is_gone(self):
        launchd = FakeLaunchd(linger=1.0)
        self.assertEqual(self.load(launchd).returncode, 0)
        self.assertGreaterEqual(launchd.now, 1.0)                       # it waited for the job to go
        self.assertEqual(launchd.calls[-1][1], "bootstrap")
        self.assertEqual([c[1] for c in launchd.calls].count("bootstrap"), 1)   # no refused load first
        self.assertIn(self.path.stem, launchd.loaded)

    def test_error_5_once_the_job_is_gone_is_tried_again(self):
        """The regression: the first bootstrap fails with error 5, the next, once it's gone, loads it."""
        launchd = FakeLaunchd(linger=0.5, busy_after_gone=1)
        self.assertEqual(self.load(launchd).returncode, 0)
        self.assertEqual([c[1] for c in launchd.calls].count("bootstrap"), 2)
        self.assertIn(self.path.stem, launchd.loaded)

    def test_a_job_that_never_goes_is_waited_for_a_bounded_time_and_reported(self):
        launchd = FakeLaunchd(linger=float("inf"))
        r = self.load(launchd)
        self.assertIn("Input/output error", r.stderr)
        self.assertEqual([c[1] for c in launchd.calls].count("bootstrap"), agent_service.BOOTSTRAP_TRIES)
        most = (agent_service.GONE_TIMEOUT_S + agent_service.GONE_POLL_S
                + (agent_service.BOOTSTRAP_TRIES - 1) * agent_service.BOOTSTRAP_RETRY_S)
        self.assertLessEqual(launchd.now, most)                         # one deadline for every try


class MigrateAll(unittest.TestCase):
    """service_migration.migrate on the owner's five plists: every one it can is moved."""
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.files = {}
        for agent in (OWNERS, *FURTHER):
            path = agent_service.service_file("launchd", agent, self.home)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(further_plist(agent, f"{APP}/bridge.py"))
            self.files[agent] = path

    def migrate(self, how: str, run, **timing) -> list[str]:
        with mock.patch.object(agent_service, "kind", return_value=how):
            return service_migration.migrate(APP, run=run, home=self.home, **timing)

    def test_further_agents_in_launchds_own_layout_are_moved_and_nothing_else_changes(self):
        launchd = FakeLaunchd(linger=0.75, busy_after_gone=1)
        moved = self.migrate("launchd", launchd, sleep=launchd.sleep, clock=launchd.clock)
        self.assertEqual(moved, sorted(p.name for p in self.files.values()))
        for agent, path in self.files.items():
            self.assertEqual(path.read_text(), further_plist(agent, f"{APP}/bin/bridge.py"))
        self.assertEqual(launchd.loaded, {p.stem for p in self.files.values()})

    def test_one_refused_plist_doesnt_stop_the_others_and_is_named(self):
        owners = self.files[OWNERS]
        launchd = FakeLaunchd(linger=0.25, refuse={owners.stem})
        with self.assertRaises(service_migration.MigrationError) as e:
            self.migrate("launchd", launchd, sleep=launchd.sleep, clock=launchd.clock)
        self.assertEqual(list(e.exception.failed), [owners.name])
        self.assertIn("Bootstrap failed: 5", e.exception.failed[owners.name])
        self.assertEqual(e.exception.moved, sorted(self.files[a].name for a in FURTHER))
        self.assertEqual(launchd.loaded, {self.files[a].stem for a in FURTHER})
        self.assertEqual(owners.read_text(), further_plist(OWNERS, f"{APP}/bridge.py"))   # put back: still to do
        self.assertEqual(self.migrate("launchd", FakeLaunchd()), [owners.name])          # the next run moves it

    def test_an_unreadable_plist_is_named_and_the_others_still_move(self):
        """GPT review: a file that isn't text (UnicodeDecodeError) mustn't end the run."""
        self.files[OWNERS].write_bytes(b"\xff\xfe\x00garbage")
        launchd = FakeLaunchd()
        with self.assertRaises(service_migration.MigrationError) as e:
            self.migrate("launchd", launchd, sleep=launchd.sleep, clock=launchd.clock)
        self.assertEqual(list(e.exception.failed), [self.files[OWNERS].name])
        self.assertEqual(launchd.loaded, {self.files[a].stem for a in FURTHER})

    def test_a_unit_that_cant_be_put_back_doesnt_stop_the_others_being_put_back(self):
        """GPT review: after a refused daemon-reload every unit is put back, even if one write fails."""
        units = self.units()
        first, olds = units[0], {u: u.read_text() for u in units}
        write = service_migration._write
        def refuse_first(path, text):
            if path == first and text == olds[first]:
                raise PermissionError("read-only")
            write(path, text)
        with mock.patch.object(service_migration, "_write", refuse_first), \
                self.assertRaises(service_migration.MigrationError) as e:
            self.migrate("systemd", lambda *a, **k: mock.Mock(returncode=1, stdout="", stderr="no bus"))
        self.assertEqual(sorted(e.exception.failed), sorted(u.name for u in units))
        self.assertIn("couldn't be put back", e.exception.failed[first.name])
        self.assertEqual({u: u.read_text() for u in units[1:]}, {u: olds[u] for u in units[1:]})

    def units(self) -> list[Path]:
        units = []
        for agent in FURTHER:
            unit = agent_service.service_file("systemd", agent, self.home)
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(f'[Service]\nExecStart="{APP}/.venv/bin/python" "{APP}/bridge.py" "cx"\n')
            units.append(unit)
        return units

    def test_systemd_still_reloads_once_and_never_waits(self):
        units = self.units()
        launchd = FakeLaunchd()
        self.assertEqual(self.migrate("systemd", launchd, sleep=self.fail), sorted(u.name for u in units))
        self.assertEqual(launchd.calls, [["systemctl", "--user", "daemon-reload"]])
        for unit in units:
            self.assertIn(f'"{APP}/bin/bridge.py"', unit.read_text())


if __name__ == "__main__":
    unittest.main()
