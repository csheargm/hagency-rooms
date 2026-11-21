"""Unit tests: the Windows keep-alive (connect_win.py) that the Scheduled Task runs.
Run: .venv/bin/python -m unittest tests.test_connect_win"""
import ctypes
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import connect_win

CREDS = {"homeserver": "https://matrix.example.org", "user_id": "@anna-agent:matrix.example.org",
         "password": "s3cret-pw", "owner": "anna", "agent": "claude"}


def home_with(creds: dict | None = CREDS) -> Path:
    home = Path(tempfile.mkdtemp())
    if creds is not None:
        (home / "credentials.json").write_text(json.dumps(creds))
    return home


LOG = Path("logs") / "anna-agent.log"


class FakeProc:
    def __init__(self, code: int):
        self.pid, self.code = 4242, code

    def wait(self):
        return self.code


class KeepRunning(unittest.TestCase):
    def run_it(self, home: Path, runs: int, lifetimes: list[float], codes=None):
        calls, sleeps, ticks = [], [], iter([t for life in lifetimes for t in (0, life)])
        codes = iter(codes or [1] * runs)

        def spawn(argv, **kw):
            calls.append((argv, kw))
            kw["stdout"].write(b"bridge output\n")
            return FakeProc(next(codes))
        connect_win.keep_running(home, runs=runs, spawn=spawn, sleep=sleeps.append, clock=lambda: next(ticks))
        return calls, sleeps

    def test_runs_the_bridge_with_the_sign_in_by_file_not_on_the_command_line(self):
        home = home_with()
        (argv, kw), = self.run_it(home, 1, [10])[0]
        self.assertEqual(argv[1:], [str(connect_win.APP / "bridge.py"), "anna", "--agent", "claude"])
        self.assertNotIn("s3cret-pw", " ".join(argv))
        self.assertEqual(kw["env"]["HAGENCY_CREDENTIALS"], str(home / "credentials.json"))
        self.assertEqual(kw["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual((kw["cwd"], kw["stdin"], kw["stderr"]), (str(connect_win.APP), subprocess.DEVNULL, subprocess.STDOUT))
        self.assertEqual((home / "agent.pid").read_text().split()[1], "4242")
        log = (home / LOG).read_text()
        self.assertIn("bridge output", log)
        self.assertIn("the agent stopped (exit 1)", log)

    def test_restarts_with_a_growing_wait_that_resets_after_a_good_run(self):
        _, sleeps = self.run_it(home_with(), 5, [1, 1, 1, connect_win.HEALTHY_RUN_S, 1])
        self.assertEqual(sleeps, [5, 10, 20, 5, 10])

    def test_a_clean_exit_means_disconnected_and_stays_stopped(self):
        """bridge.py exits 0 when the server disconnected it (as launchd/systemd, no restart)."""
        home = home_with()
        calls, sleeps = self.run_it(home, 5, [1, 1], codes=[1, 0])
        self.assertEqual((len(calls), sleeps), (2, [5]))
        self.assertIn("stopped cleanly", (home / LOG).read_text())

    def test_only_one_keeper_per_sign_in_folder(self):
        home = home_with()
        first = connect_win.hold_lock(home / "agent.lock")
        self.addCleanup(first.close)
        self.assertIsNotNone(first)
        self.assertIsNone(connect_win.hold_lock(home / "agent.lock"))
        with mock.patch.object(sys, "argv", ["connect_win.py", "--home", str(home)]), \
                mock.patch.object(connect_win, "keep_running") as keep:
            connect_win.main()                                # a second task or Startup entry: does nothing
        keep.assert_not_called()

    def test_a_refused_job_is_said_not_fatal(self):
        home = home_with()

        def refuse():
            raise OSError(5, "access denied")
        connect_win.tie_or_say(home, tie=refuse)
        self.assertIn("no job for the agent", (home / "logs" / "connect_win.log").read_text())

    def test_an_unwritable_log_never_stops_the_agent(self):
        home = home_with()
        (home / "logs").write_text("not a folder")              # the log can't be opened
        calls, _ = self.run_it(home, 1, [10])
        self.assertEqual(len(calls), 1)

    def test_the_job_layout_matches_windows(self):
        """JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes on 64-bit Windows (112 on 32-bit)."""
        self.assertEqual(ctypes.sizeof(connect_win._JobLimits), 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)

    def test_the_wait_never_passes_the_cap(self):
        delay = 0.0
        for _ in range(20):
            delay = connect_win.next_delay(delay, 0)
        self.assertEqual(delay, connect_win.MAX_DELAY_S)

    def test_no_sign_in_stops_with_a_reason_in_the_log(self):
        home = home_with(None)
        with mock.patch.object(sys, "argv", ["connect_win.py", "--home", str(home)]), self.assertRaises(SystemExit):
            connect_win.main()
        self.assertIn("credentials file", (home / "logs" / "connect_win.log").read_text())

    def test_a_big_log_is_kept_once_and_started_over(self):
        home = home_with()
        (home / "logs").mkdir()
        (home / LOG).write_bytes(b"x" * (connect_win.LOG_MAX_BYTES + 1))
        self.run_it(home, 1, [10])
        self.assertEqual((home / "logs" / "anna-agent.log.1").stat().st_size, connect_win.LOG_MAX_BYTES + 1)
        self.assertLess((home / LOG).stat().st_size, 1000)


class Names(unittest.TestCase):
    def test_task_name(self):
        self.assertEqual(connect_win.task_name(CREDS), "Hagency agent anna")
        self.assertEqual(connect_win.task_name({**CREDS, "user_id": "@alice-at-matrix-org-1a2b-agent:x.org"}),
                         "Hagency agent alice-at-matrix-org-1a2b")

    def test_child_python_is_the_console_one_beside_pythonw(self):
        d = Path(tempfile.mkdtemp())
        (d / "python.exe").write_text("")
        with mock.patch.object(sys, "executable", str(d / "pythonw.exe")):
            self.assertEqual(connect_win.child_python(), str(d / "python.exe"))
        with mock.patch.object(sys, "executable", "/usr/bin/python3"):
            self.assertEqual(connect_win.child_python(), "/usr/bin/python3")


if __name__ == "__main__":
    unittest.main()
