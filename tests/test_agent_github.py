"""The panel's "Sign in to GitHub", the agent's side (adapters/agents/agent_github.py, common/github_login.py;
docs/adr/0021-agent-github-signin.md). A real child process stands in for gh (tests/fake_gh_login.py).
Live test 2026-11-26: the only fix for "needs a GitHub sign-in" was a shell command.
Run: .venv/bin/python -m unittest tests.test_agent_github"""
import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import ToDeviceEvent, UnknownToDeviceEvent

from adapters.agents import agent_github
from adapters.agents import agent_update
from common import github
from common import github_login

FAKE = Path(__file__).resolve().parent / "fake_gh_login.py"
AGENT = "cx-agent"


def fake_gh(folder: Path) -> str:
    """An executable `gh` that runs the stand-in with this Python."""
    path = folder / "gh"
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    path.chmod(0o755)
    return str(path)


class FakeBridge(agent_github.BridgeGitHub):
    def __init__(self):
        self.owner, self.sent = "cx", []

        async def to_device(message, tx):
            self.sent.append((message.type, message.recipient, message.content))
        self.client = SimpleNamespace(to_device=to_device)


def request(sender, ask="k1"):
    event = ToDeviceEvent.parse_event({"sender": sender, "type": github_login.REQUEST_TYPE, "content": {"id": ask}})
    assert isinstance(event, UnknownToDeviceEvent)       # what listen_to_pm's callback is given
    return event


class Base(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.root, self.log = tmp / ".gh", tmp / "gh-log.json"
        for p in (mock.patch.object(github, "IDENTITY_ROOT", self.root), mock.patch.object(github, "GH_BIN", fake_gh(tmp)),
                  mock.patch.dict(os.environ, {"FAKE_GH_LOG": str(self.log), "FAKE_GH_MODE": "ok", "GH_TOKEN": "owners-own",
                                               "GITHUB_TOKEN": "owners-too"})):
            p.start()
            self.addCleanup(p.stop)

    def click(self, bridge=None, sender=agent_github.PM_ID, mode="ok", ask="k1"):
        bridge = bridge or FakeBridge()
        os.environ["FAKE_GH_MODE"] = mode

        async def go():
            await bridge.on_github_request(request(sender, ask))
            job = bridge.__dict__.get("_gh_job")
            if job:
                await job
        with self.assertLogs("bridge", "INFO") as logs:
            asyncio.run(go())
            agent_github.log.info("done")                # assertLogs needs one line
        self.logs = "\n".join(logs.output)
        return bridge

    def answers(self, bridge):
        return [c for t, r, c in bridge.sent if t == github_login.ANSWER_TYPE]


class SignIn(Base):
    def test_pms_click_signs_it_in_and_only_the_code_and_login_go_out(self):
        b = self.click()
        self.assertEqual(self.answers(b), [{"id": "k1", "code": "WDJB-MJHT"}, {"id": "k1", "result": "signed_in", "login": "octo-cat"}])
        self.assertEqual(b.sent[-1], (github_login.STATE_TYPE, agent_github.PM_ID, {"state": "signed_in", "login": "octo-cat", "id": "k1"}))
        everything = json.dumps(b.sent) + self.logs
        self.assertNotIn("gho_", everything)                # gh printed a token-like line: never sent, never logged
        self.assertNotIn("curious", everything)
        given = json.loads(self.log.read_text())
        self.assertEqual(given["args"], github_login.LOGIN)
        self.assertEqual(given["stdin"], "\n")              # Enter, if it asks
        env = given["env"]
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.root / AGENT))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(self.root / AGENT / "gitconfig"))
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual((env["BROWSER"], env["GH_BROWSER"]), (github_login.NO_BROWSER, github_login.NO_BROWSER))   # nothing opens there
        self.assertEqual((env["GH_TOKEN"], env["GITHUB_TOKEN"]), (None, None))   # never the owner's own login
        self.assertEqual(stat.S_IMODE(os.stat(self.root / AGENT).st_mode), 0o700)
        self.assertIsNotNone(github.identity_dir(AGENT))   # where github.run looks

    def test_only_pms_account_is_obeyed(self):
        for sender in ("@cx:bridge.localhost", "@mallory-agent:bridge.localhost"):
            b = self.click(sender=sender)
            self.assertEqual(b.sent, [])
        self.assertFalse(self.log.exists())

    def test_already_signed_in_skips_gh(self):
        (self.root / AGENT).mkdir(parents=True)
        (self.root / AGENT / "hosts.yml").write_text("github.com:\n    user: octo-cat\n")
        b = self.click()
        self.assertEqual(self.answers(b), [{"id": "k1", "result": "already", "login": "octo-cat"}])
        self.assertFalse(self.log.exists())

    def test_each_failure_is_one_word_and_no_code_goes_out_for_a_bad_one(self):
        for mode, word in (("fail", "failed"), ("badcode", "failed"), ("silent", "failed"), ("old", "too_old_gh")):
            b = self.click(mode=mode)
            answers = self.answers(b)
            self.assertEqual(answers[-1], {"id": "k1", "result": word}, mode)
            self.assertTrue(all("code" not in a for a in answers) or mode == "fail", mode)
            self.assertIn(word, github_login.RESULTS)
            self.assertEqual(b.sent[-1][2], {"state": "none", "id": "k1"})

    def test_no_gh_on_that_computer(self):
        with mock.patch.object(github, "GH_BIN", "/nowhere/gh"):
            b = self.click()
        self.assertEqual(self.answers(b), [{"id": "k1", "result": "no_gh"}])
        self.assertEqual(b.sent[-1][2], {"state": "no_gh", "id": "k1"})

    def test_a_code_nobody_enters_times_out_and_gh_is_stopped(self):
        with mock.patch.object(agent_github, "SIGNIN_S", 0.5):
            b = self.click(mode="hang")
        self.assertEqual(self.answers(b)[-1], {"id": "k1", "result": "timeout"})
        self.assertIsNone(github.identity_dir(AGENT))

    def test_a_code_pm_never_got_stops_the_sign_in_at_once(self):
        b = FakeBridge()

        async def refused(message, tx):
            raise OSError("down")
        b.client.to_device = refused
        with mock.patch.object(agent_github, "SIGNIN_S", 30):
            self.click(b, mode="hang")                     # not 30 seconds: done as soon as the code didn't go out
        self.assertIn("couldn't tell @pm", self.logs)

    def test_a_newer_click_stops_the_running_sign_in_and_starts_again(self):
        """Review 2026-11-26: @pm restarted (or took the code as late) and sent a new click; the agent ignored
        it while gh waited 15 minutes for the old code, so the owner never got one."""
        b = FakeBridge()
        os.environ["FAKE_GH_MODE"] = "hang"

        async def go():
            with mock.patch.object(agent_github, "SIGNIN_S", 1):
                await b.on_github_request(request(agent_github.PM_ID, "k1"))
                first, pid = b._gh_job, await self.gh_started(b)
                await b.on_github_request(request(agent_github.PM_ID, "k2"))   # a new click: the newest wins
                self.assertTrue(first.cancelled())
                self.assertFalse(alive(pid))               # the old gh is gone, not left waiting
                await b._gh_job
        asyncio.run(go())
        self.assertEqual(self.answers(b), [{"id": "k1", "code": "WDJB-MJHT"}, {"id": "k2", "code": "WDJB-MJHT"},
                                           {"id": "k2", "result": "timeout"}])

    def test_a_restart_kills_a_running_gh_first(self):
        """Review 2026-11-26: an Update's execv left gh orphaned, waiting on GitHub for 15 minutes."""
        b = Updating()
        os.environ["FAKE_GH_MODE"] = "hang"

        async def go():
            with mock.patch.object(agent_github, "SIGNIN_S", 30), \
                    mock.patch.object(agent_update.lifecycle, "serve", mock.AsyncMock()):
                await b.on_github_request(request(agent_github.PM_ID, "k1"))
                pid = await self.gh_started(b)
                await asyncio.wait_for(b.serve_until_updated(), 10)   # not 30 seconds
                self.assertTrue(b._gh_job.cancelled())
                self.assertFalse(alive(pid))
        asyncio.run(go())

    async def gh_started(self, bridge) -> int:
        """The stand-in gh's pid, once its code has gone to @pm."""
        for _ in range(200):
            if self.answers(bridge) and self.log.exists():
                return json.loads(self.log.read_text())["pid"]
            await asyncio.sleep(0.05)
        self.fail("gh never gave its code")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:                         # gone and reaped: what these tests want
        return False
    return True


class Updating(FakeBridge, agent_update.BridgeUpdate):
    """An agent with ADR 0020's update loop, for its restart."""
    def __init__(self):
        super().__init__()
        self.tasks, self.sync_point, self.updated = set(), None, False


def now(agent: str) -> dict:
    return asyncio.run(agent_github.current_state(agent))


class State(Base):
    def test_its_start_says_signed_in_or_not_or_no_gh(self):
        self.assertEqual(now(AGENT), {"state": "none"})
        with mock.patch.object(github, "GH_BIN", "/nowhere/gh"):
            self.assertEqual(now(AGENT), {"state": "no_gh"})
        (self.root / AGENT).mkdir(parents=True)
        (self.root / AGENT / "hosts.yml").write_text("github.com:\n    oauth_token: gho_secret\n    user: octo-cat\n")
        self.assertEqual(now(AGENT), {"state": "signed_in", "login": "octo-cat"})
        (self.root / AGENT / "hosts.yml").write_text("github.com:\n    user: bad name; rm\n")
        self.assertEqual(now(AGENT), {"state": "none"})   # no valid github.com user: not signed in

    def test_pm_keeps_only_its_exact_shape(self):
        self.assertEqual(github_login.state({"state": "signed_in", "login": "octo-cat"}), {"state": "signed_in", "login": "octo-cat"})
        self.assertEqual(github_login.state({"state": "none"}), {"state": "none", "login": None})
        for bad in ({"state": "signed_in", "login": "a b"}, {"state": "maybe"}, {"state": "none", "token": "x"}, "none", None):
            self.assertIsNone(github_login.state(bad), bad)


class Recipe(unittest.TestCase):
    """One recipe for the connect line, the button and the command people type (R3)."""
    def test_the_command_runs_the_same_recipe_in_a_shell(self):
        """Run for real in sh, in a folder with a space, with the owner's token exported (GPT review)."""
        tmp = Path(tempfile.mkdtemp()).resolve() / "my app"   # resolved: $PWD is (macOS: /private/var)
        tmp.mkdir()
        (tmp / "bin").mkdir()
        gh = tmp / "bin" / "gh"
        gh.write_text('#!/bin/sh\nenv > "$GH_CONFIG_DIR/seen"; echo "$@" >> "$GH_CONFIG_DIR/seen"\n')
        gh.chmod(0o755)
        done = subprocess.run(["sh", "-c", github_login.command("anna-agent")], cwd=tmp, capture_output=True, text=True,
                              env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "GH_TOKEN": "owners-own", "GITHUB_TOKEN": "x"})
        self.assertEqual(done.returncode, 0, done.stderr)
        d = tmp / ".gh" / "anna-agent"
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        seen = (d / "seen").read_text()
        for line in (f"GH_CONFIG_DIR={d}", f"GIT_CONFIG_GLOBAL={d}/gitconfig", "GIT_CONFIG_NOSYSTEM=1", " ".join(github_login.LOGIN)):
            self.assertIn(line, seen)
        self.assertNotIn("TOKEN=", seen)

    def test_the_docs_show_that_command(self):
        docs = (Path(__file__).resolve().parent.parent / "docs" / "GITHUB_SETUP.md").read_text()
        self.assertIn(github_login.command("AGENT").replace("AGENT", "<agent>"), docs)

    def test_a_strange_name_is_quoted_for_the_shell(self):
        self.assertIn("'a;b'", github_login.command("a;b"))

    def test_only_github_coms_user_counts_as_signed_in(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.object(github, "IDENTITY_ROOT", root):
            (root / AGENT).mkdir()
            (root / AGENT / "hosts.yml").write_text("ghe.example.com:\n    user: someone\n")
            self.assertIsNone(github_login.login_of(AGENT))
            self.assertIn(now(AGENT)["state"], ("none", "no_gh"))
            (root / AGENT / "hosts.yml").write_text("ghe.example.com:\n    user: someone\ngithub.com:\n    user: octo-cat\n")
            self.assertEqual(github_login.login_of(AGENT), "octo-cat")

    def test_a_forced_color_code_is_still_read(self):
        async def go():
            stream = asyncio.StreamReader()
            stream.feed_data(b"! First copy your one-time code: \x1b[1mWDJB-MJHT\x1b[0m\n")
            stream.feed_eof()
            return await agent_github._read_code(stream)
        self.assertEqual(asyncio.run(go()), "WDJB-MJHT")
        with mock.patch.dict(os.environ, {"CLICOLOR_FORCE": "1"}):
            env = github_login.env_for(Path("/x"), quiet=True)
        self.assertNotIn("CLICOLOR_FORCE", env)
        self.assertEqual(env["NO_COLOR"], "1")

    def test_codes_are_githubs_shape_only(self):
        self.assertTrue(github_login.USER_CODE_RE.fullmatch("WDJB-MJHT"))
        for bad in ("wdjb-mjht", "WDJB-MJH", "WDJB-MJHT\n", "WDJB MJHT", "<b>-1234"):
            self.assertIsNone(github_login.USER_CODE_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
