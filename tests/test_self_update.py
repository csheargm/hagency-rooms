"""An agent updates its own code from the official repo when its owner clicks Update
(adapters/agents/self_update.py, agent_update.py; docs/adr/0020-versions-and-agent-self-update.md).

Real git repos in a temporary folder: an "official" one with master and another branch, and an
agent's install cloned from it. pip is never run for real (a stand-in records it).
Run: .venv/bin/python -m unittest tests.test_self_update"""
import asyncio
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import ToDeviceEvent, UnknownToDeviceEvent

from adapters.agents import agent_update
from adapters.agents import self_update
from common import code_report

PLAIN_GIT = ["git", "-c", "core.hooksPath=/dev/null"]   # the tests' official repo is a local folder


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, name: str, text: str = "x") -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


class Install(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.official, self.app = tmp / "official", tmp / "home" / ".hagency" / "app"
        self.official.mkdir()
        git(self.official, "init", "-q", "-b", "master")
        (self.official / "requirements.txt").write_text("matrix-nio\n")
        self.old = commit(self.official, "requirements.txt", "matrix-nio\n")
        git(tmp, "clone", "-q", str(self.official), str(self.app))
        self.new = commit(self.official, "requirements.txt", "matrix-nio\naiohttp\n")
        self.newest = commit(self.official, "b.txt")
        for p in (mock.patch.object(self_update, "OFFICIAL", str(self.official)),
                  mock.patch.object(self_update, "GIT", PLAIN_GIT)):
            p.start()
            self.addCleanup(p.stop)
        self.pip = []

    def run_(self, args, **kw):
        """subprocess.run, except pip: recorded with the requirements it was given (and fails on demand)."""
        if args[1:3] == ["-m", "pip"]:
            self.pip.append(Path(args[-1]).read_text())
            return subprocess.CompletedProcess(args, 1 if getattr(self, "pip_fails", False) else 0, "", "")
        return subprocess.run(args, **kw)

    def apply(self, target, own=False, code=None):
        return self_update.apply(target, own, app=self.app, code=code or self.app, run=self.run_)

    def head(self):
        return git(self.app, "rev-parse", "HEAD")

    def test_moves_forward_to_the_servers_commit_after_installing_its_requirements(self):
        self.assertEqual(self.apply(self.new), "updated")
        self.assertEqual(self.head(), self.new)                      # the server's commit, not master's tip
        self.assertEqual(self.pip, ["matrix-nio\naiohttp\n"])        # the new commit's requirements
        self.assertEqual(self.apply(self.new), "current")            # a second click: nothing to do

    def test_a_failed_pip_changes_nothing(self):
        self.pip_fails = True
        self.assertEqual(self.apply(self.new), "failed")
        self.assertEqual(self.head(), self.old)

    def test_never_replaces_the_owners_own_version(self):
        self.assertEqual(self.apply(self.new, own=True), "own")
        self.assertEqual(self.apply(self.new, code=self.app / "custom" / "cx" / "live-abc"), "own")   # runs a live copy
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.pip, [])

    def test_edited_code_is_never_overwritten(self):
        (self.app / "requirements.txt").write_text("something of mine\n")
        self.assertEqual(self.apply(self.new), "edited")
        self.assertEqual((self.app / "requirements.txt").read_text(), "something of mine\n")

    def test_edits_status_hides_count_as_edits(self):
        git(self.app, "update-index", "--skip-worktree", "requirements.txt")
        (self.app / "requirements.txt").write_text("hidden edit\n")
        self.assertEqual(self.apply(self.new), "edited")
        self.assertEqual((self.app / "requirements.txt").read_text(), "hidden edit\n")

    def test_an_edit_made_while_pip_ran_is_not_overwritten(self):
        real = self.run_

        def edit_during_pip(args, **kw):
            done = real(args, **kw)
            if args[1:3] == ["-m", "pip"]:
                (self.app / "requirements.txt").write_text("edited meanwhile\n")
            return done
        self.run_ = edit_during_pip
        self.assertEqual(self.apply(self.new), "edited")
        self.assertEqual(self.head(), self.old)

    def test_one_update_of_the_install_at_a_time(self):
        with self_update._Lock(self.app), mock.patch.object(self_update, "LOCK_WAIT_S", 0.3):
            self.assertEqual(self_update.apply(self.new, False, app=self.app, code=self.app, run=self.run_), "failed")
        self.assertEqual(self.apply(self.new), "updated")    # held far longer than an update takes: "failed", asked again

    def test_its_git_calls_fit_the_budget_pm_waits_for(self):
        """GPT re-check: code_report.UPDATE_MAX_S counted 4 git calls; an update makes up to GIT_CALLS."""
        calls = []

        def counted(args, **kw):
            calls.append(args) if args[0] == "git" else None
            return self.run_(args, **kw)
        self.assertEqual(self_update.apply(self.new, False, app=self.app, code=self.app, run=counted), "updated")
        self.assertLessEqual(len(calls), self_update.GIT_CALLS)

    def test_another_agents_update_half_done_is_never_taken_for_an_edit(self):
        """GPT review: the clean-tree check ran before the lock, so a second agent could see the first's half-done
        checkout and answer "edited" (which stops automatic updates)."""
        lock = self_update._Lock(self.app)
        lock.__enter__()
        (self.app / "requirements.txt").write_text("half written\n")   # the first agent's checkout in progress
        timer = threading.Timer(0.5, lambda: (git(self.app, "checkout", "--", "requirements.txt"), lock.__exit__()))
        timer.start()
        self.assertEqual(self.apply(self.new), "updated")
        timer.join()

    def test_two_agents_of_one_install_asked_together_both_end_on_the_new_code(self):
        """Live test 2026-11-27: cx-agent updated the shared install while cx-codex2-agent's update said
        "failed" and kept running old code. Now the second waits for the first, finds the install at the
        target ("current"), and agent_update.stale() restarts it onto that code (AgentSide's test)."""
        real, started = self.run_, threading.Event()

        def slow_pip(args, **kw):
            if args[1:3] == ["-m", "pip"]:
                started.set()
                time.sleep(1.5)               # the first agent's pip, while the second is asked
            return real(args, **kw)
        first = {}
        worker = threading.Thread(target=lambda: first.update(r=self_update.apply(self.new, False, app=self.app,
                                                                                    code=self.app, run=slow_pip)))
        worker.start()
        self.assertTrue(started.wait(30))
        second = self.apply(self.new)
        worker.join(30)
        self.assertEqual((first["r"], second, self.head()), ("updated", "current", self.new))

    def test_only_a_clone_of_the_official_repo_on_master(self):
        git(self.app, "remote", "set-url", "origin", "https://github.com/someone/hagency-rooms")
        self.assertEqual(self.apply(self.new), "not_official")
        git(self.app, "remote", "set-url", "origin", str(self.official))
        git(self.app, "checkout", "-q", "-b", "mine")
        self.assertEqual(self.apply(self.new), "not_official")
        self.assertEqual(self.head(), self.old)

    def test_only_a_commit_on_the_official_master_and_only_forward(self):
        git(self.official, "checkout", "-q", "-b", "elsewhere")
        stray = commit(self.official, "evil.txt")                    # on the official repo, but not on master
        git(self.official, "checkout", "-q", "master")
        self.assertEqual(self.apply(stray), "not_newer")
        self.assertEqual(self.apply("not-a-commit"), "not_newer")
        self.assertEqual(self.apply("f" * 40), "not_newer")          # unknown anywhere
        self.assertEqual(self.head(), self.old)
        self.assertEqual(self.apply(self.newest), "updated")
        self.assertEqual(self.apply(self.new), "current")            # behind what runs: never backwards
        self.assertEqual(self.head(), self.newest)

    def test_the_url_is_fetched_never_a_remote_named_by_the_clone(self):
        """origin's URL is checked, but what is fetched is OFFICIAL itself (as deploy.sh does)."""
        calls = []
        real = self.run_
        self.run_ = lambda args, **kw: calls.append(args) or real(args, **kw)
        self.apply(self.new)
        fetches = [a for a in calls if "fetch" in a]
        self.assertEqual(len(fetches), 1)
        self.assertIn(str(self.official), fetches[0])
        self.assertNotIn("origin", fetches[0])

    def test_git_hooks_never_run(self):
        hook = self.app / ".git" / "hooks" / "post-merge"
        hook.write_text(f"#!/bin/sh\ntouch {self.app.parent / 'hook-ran'}\n")
        hook.chmod(0o755)
        with mock.patch.object(self_update, "GIT", ["git", "-c", "core.hooksPath=/dev/null"]):
            self.assertEqual(self.apply(self.new), "updated")
        self.assertFalse((self.app.parent / "hook-ran").exists())

    def test_every_outcome_is_a_word_pm_shows(self):
        self.assertIn(self.apply(self.new), code_report.RESULTS)
        with mock.patch.object(self_update, "_git", side_effect=OSError("no git")):
            self.assertEqual(self.apply(self.newest), "failed")


class FakeBridge(agent_update.BridgeUpdate):
    def __init__(self, buttons_only=False):
        self.owner, self.buttons_only, self.tasks = "cx", buttons_only, set()
        self.sent = []
        self.sync_point = SimpleNamespace(stop=asyncio.Event())

        async def to_device(message, tx):
            self.sent.append((message.type, message.recipient, message.content))
        self.client = SimpleNamespace(to_device=to_device)


def request(sender, commit="a" * 40):
    event = ToDeviceEvent.parse_event({"sender": sender, "type": code_report.UPDATE_TYPE, "content": {"commit": commit, "id": "k1"}})
    assert isinstance(event, UnknownToDeviceEvent)      # what listen_to_pm's callback is given
    return event


class AgentSide(unittest.TestCase):
    def click(self, bridge, sender, result="updated"):
        async def go():
            with mock.patch.object(self_update, "apply", return_value=result) as apply:
                await bridge.on_update_request(request(sender))
                await asyncio.gather(*bridge.tasks)
            return apply
        return asyncio.run(go())

    def test_only_pms_request_is_obeyed_then_it_restarts_on_the_new_code(self):
        b = FakeBridge()
        self.assertFalse(self.click(b, "@cx:bridge.localhost").called)        # its owner's own account: no
        self.assertFalse(self.click(b, "@mallory-agent:bridge.localhost").called)
        apply = self.click(b, agent_update.PM_ID)
        apply.assert_called_once_with("a" * 40, mock.ANY)
        self.assertEqual(b.sent, [(code_report.RESULT_TYPE, agent_update.PM_ID, {"result": "updated", "id": "k1"})])
        self.assertTrue(b.updated and b.sync_point.stop.is_set())            # hands off, drains, then restarts

        async def served(os_name):
            with mock.patch.object(agent_update.lifecycle, "serve", mock.AsyncMock()), \
                    mock.patch.object(agent_update.os, "name", os_name), \
                    mock.patch.object(agent_update.os, "execv") as execv:
                await b.serve_until_updated()
            return execv
        execv = asyncio.run(served("posix"))      # in place: a service and a terminal (--foreground) alike (GPT review)
        execv.assert_called_once_with(agent_update.sys.executable, agent_update.sys.orig_argv)
        with self.assertRaises(SystemExit) as stop:
            asyncio.run(served("nt"))             # the Windows keeper restarts a failed exit
        self.assertEqual(stop.exception.code, agent_update.EXIT_UPDATED)
        self.assertNotEqual(agent_update.EXIT_UPDATED, 0)                   # 0 = stay stopped

    def test_a_refusal_is_said_and_it_keeps_running(self):
        b = FakeBridge()
        self.click(b, agent_update.PM_ID, result="edited")
        self.assertEqual(b.sent, [(code_report.RESULT_TYPE, agent_update.PM_ID, {"result": "edited", "id": "k1"})])
        self.assertFalse(b.updated or b.sync_point.stop.is_set())
        self.assertTrue(self.click(b, agent_update.PM_ID, result="edited").called)   # not stuck: a later click works

    def test_a_hermes_helper_never_updates(self):
        b = FakeBridge(buttons_only=True)
        self.assertFalse(self.click(b, agent_update.PM_ID).called)
        self.assertEqual(b.sent, [(code_report.RESULT_TYPE, agent_update.PM_ID, {"result": "not_official", "id": "k1"})])

    def test_another_agent_updated_the_shared_install_so_this_one_restarts_too(self):
        """GPT review follow-up: several agents of one computer share ~/.hagency/app."""
        b = FakeBridge()
        with mock.patch.object(agent_update, "STARTED", {"commit": "a" * 40, "time": 1}), \
                mock.patch.object(agent_update.version, "of", return_value={"commit": "b" * 40, "time": 2}):
            self.click(b, agent_update.PM_ID, result="current")
        self.assertTrue(b.updated)
        self.assertEqual(b.sent[0][2]["result"], "updated")

    def test_the_real_settings_turn_hooks_and_local_repos_off(self):
        self.assertIn("core.hooksPath=/dev/null", self_update.GIT)
        self.assertIn("protocol.file.allow=never", self_update.GIT)
        self.assertIn("core.fsmonitor=false", self_update.GIT)
        self.assertEqual((self_update.GIT_ENV["GIT_CONFIG_NOSYSTEM"], self_update.GIT_ENV["GIT_NO_REPLACE_OBJECTS"]), ("1", "1"))
        self.assertEqual(self_update.GIT_ENV["GIT_CONFIG_GLOBAL"], self_update.os.devnull)   # no insteadOf from there
        self.assertEqual(self_update.GIT_ENV["GIT_TERMINAL_PROMPT"], "0")
        self.assertTrue(self_update.same_repo("https://github.com/csheargm/hagency-rooms.git"))
        self.assertTrue(self_update.same_repo("https://github.com/csheargm/hagency-rooms/\n"))
        self.assertFalse(self_update.same_repo("https://github.com/csheargm/hagency-rooms-fork"))

    def test_an_owners_chosen_version_counts_as_their_own(self):
        with mock.patch.object(agent_update.customize, "in_use", return_value={"cx": "b" * 40}):
            self.assertTrue(agent_update.runs_own("cx"))
            self.assertFalse(agent_update.runs_own("betty"))


if __name__ == "__main__":
    unittest.main()
