"""connect.py offers to sign the new agent in to GitHub (client/github_signin.py). Live test 2026-11-23:
an agent couldn't open issues or pull requests, and nothing during setup said it needed GitHub."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client import github_signin
from common import github
from common import github_login

AGENT = "anna-agent"


class Offer(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / ".gh"
        p = mock.patch.object(github, "IDENTITY_ROOT", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.runs = []

    def gh(self, signs_in=True, code=0):
        def run(cmd, env, check, **kw):
            if cmd[-1] == "--help":                      # the flag check (review: older gh lacks it)
                return SimpleNamespace(returncode=0, stdout="  --insecure-storage  Save ... in plain text")
            self.runs.append((cmd, env))
            if signs_in:
                (Path(env["GH_CONFIG_DIR"]) / "hosts.yml").write_text("github.com: {}\n")
            return SimpleNamespace(returncode=code)
        return run

    def offer(self, answer="", **kw):
        with mock.patch("builtins.print"):
            return github_signin.offer(AGENT, "/app", ask=lambda prompt: answer, which=lambda name: "/usr/bin/gh",
                                       interactive=True, **kw)

    def test_yes_signs_it_in_to_its_own_folder_without_other_tokens(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "owner-token", "GITHUB_TOKEN": "x", "GH_HOST": "evil"}):
            self.assertTrue(self.offer("", run=self.gh()))
        cmd, env = self.runs[0]
        self.assertEqual(cmd, ["gh", *github_login.LOGIN])     # the one recipe (ADR 0021)
        self.assertIn("--web", cmd)
        self.assertIn("--insecure-storage", cmd)                # GPT review: the keychain is shared by all agents
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.root / AGENT))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(self.root / AGENT / "gitconfig"))   # never the owner's git
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertNotEqual(env.get("GH_BROWSER"), github_login.NO_BROWSER)   # its owner is here: the browser opens
        for k in github.SCRUBBED_ENV:
            self.assertNotIn(k, env)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / AGENT).st_mode), 0o700)
        self.assertIsNotNone(github.identity_dir(AGENT))       # where github.run looks

    def test_no_skips_it_and_says_how_later(self):
        with mock.patch("builtins.print") as said:
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=lambda p: "n", run=self.gh(), which=lambda n: "gh",
                                                 interactive=True))
        self.assertEqual(self.runs, [])
        self.assertIn(github_login.command(AGENT), str(said.call_args_list))

    def test_already_signed_in_asks_nothing(self):
        (self.root / AGENT).mkdir(parents=True)
        (self.root / AGENT / "hosts.yml").write_text("x")
        self.assertTrue(github_signin.offer(AGENT, "/app", ask=self.fail, run=self.fail, which=self.fail))

    def test_without_gh_it_says_what_to_install(self):
        with mock.patch("builtins.print") as said:
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=self.fail, run=self.fail, which=lambda n: None))
        self.assertIn("cli.github.com", str(said.call_args_list))

    def test_with_nobody_at_a_terminal_it_never_waits_for_an_answer(self):
        with mock.patch("builtins.print") as said:
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=self.fail, run=self.fail, which=lambda n: "gh",
                                                 interactive=False))
        self.assertIn("gh auth login", str(said.call_args_list))

    def test_an_old_gh_is_told_to_update_not_failed_cryptically(self):
        def old_gh(cmd, env, check, **kw):
            assert cmd[-1] == "--help", cmd                   # never the real sign-in
            return SimpleNamespace(returncode=0, stdout="  --web  Open a browser")
        with mock.patch("builtins.print") as said:
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=lambda p: "", run=old_gh, which=lambda n: "gh",
                                                 interactive=True))
        self.assertIn("too old", str(said.call_args_list))

    def test_ctrl_c_at_the_question_is_a_no(self):
        with mock.patch("builtins.print"):
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=mock.Mock(side_effect=KeyboardInterrupt),
                                                 run=self.fail, which=lambda n: "gh", interactive=True))

    def test_a_failed_or_abandoned_sign_in_is_not_fatal(self):
        self.assertFalse(self.offer("y", run=self.gh(signs_in=False, code=1)))
        self.assertFalse(self.offer("y", run=mock.Mock(side_effect=OSError("gh vanished"))))
        with mock.patch("builtins.print"):
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=mock.Mock(side_effect=EOFError), run=self.fail,
                                                 which=lambda n: "gh", interactive=True))   # Ctrl-D
            self.assertFalse(github_signin.offer(AGENT, "/app", ask=mock.Mock(side_effect=OSError("tty gone")),
                                                 run=self.fail, which=lambda n: "gh", interactive=True))
        self.assertIn(github_login.command(AGENT), github_signin.later("/app", AGENT))
        with mock.patch.object(github_login.os, "chmod", side_effect=PermissionError("no")):
            self.assertFalse(self.offer("y", run=self.fail))


if __name__ == "__main__":
    unittest.main()
