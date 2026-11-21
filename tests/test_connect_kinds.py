"""User-flow tests (R6): a person runs the connect line for each agent kind (connect.py,
connect_kinds.py), with @pm's server, the CLIs, Ollama and the service manager mocked. The API key
of a remote service is typed only at the prompt, saved 0600, and never in argv, a log, the saved
sign-in or the service file. Run: .venv/bin/python -m unittest tests.test_connect_kinds"""
import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from client import agent_service
from adapters.agents import codex_provider
from client import connect
from client import connect_kinds

KEY = "sk-live-do-not-leak-42"
SIGN_IN = {"homeserver": "https://matrix.example.org", "user_id": "@newbie1-agent:example.org",
           "password": "s3cret-pw", "owner": "newbie1"}
QWEN = [{"name": "qwen3:8b", "cloud": False, "chat": True}, {"name": "llama3:70b", "cloud": False, "chat": True}]


def server(answer: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(answer).encode()
    return mock.Mock(return_value=response)


class Flow(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()).resolve()
        self.installed = {"codex", "claude", "hermes"}

    def run_connect(self, *argv, answer=None, tty=True, answers=(), key=KEY, models=QWEN):
        """connect.py as the person runs it; returns (install call or None, what was shown, getpass mock)."""
        said, err = io.StringIO(), io.StringIO()
        replies = iter(answers)
        secret = mock.Mock(return_value=key)
        with mock.patch.object(sys, "argv", ["connect.py", "--home", str(self.home), "--server", "https://c.example", *argv]), \
                mock.patch.object(connect.urllib.request, "urlopen", server(answer or SIGN_IN)) as asked, \
                mock.patch.object(connect.cli_path, "startable", lambda b: b in self.installed), \
                mock.patch.object(connect_kinds.ollama_local, "installed_models", return_value=models), \
                mock.patch.object(connect_kinds.getpass, "getpass", secret), \
                mock.patch("builtins.input", lambda _: next(replies)), \
                mock.patch.object(sys.stdin, "isatty", lambda: tty), \
                mock.patch.object(agent_service, "install", return_value="launchd") as install, \
                mock.patch.object(connect.os, "execve"), redirect_stdout(said), redirect_stderr(err):
            try:
                connect.main()
                failed = None
            except SystemExit as e:
                failed = str(e)
        self.asked = asked
        return install.call_args, said.getvalue() + err.getvalue() + (failed or ""), secret, failed

    def saved(self) -> dict:
        return json.loads((self.home / "credentials.json").read_text())

    def test_no_kind_from_pm_is_todays_codex_or_claude(self):
        self.installed = {"codex"}
        install, _, _, failed = self.run_connect("ABCDE-FGHJK")
        self.assertIsNone(failed)
        self.assertEqual(install.args[2], "codex")
        self.assertEqual(self.saved()["agent"], "codex")
        self.assertNotIn("model", self.saved())

    def test_ollama_with_the_model_pm_named(self):
        install, said, secret, failed = self.run_connect("ABCDE-FGHJK", answer={**SIGN_IN, "agent_kind": "ollama",
                                                                                 "model": "qwen3:8b"})
        self.assertIsNone(failed)
        self.assertEqual(install.args[2], "ollama")
        self.assertEqual((self.saved()["agent"], self.saved()["model"]), ("ollama", "qwen3:8b"))
        self.assertIn("(ollama)", said)
        secret.assert_not_called()

    def test_ollama_asks_at_the_terminal_and_refuses_without_one(self):
        offer = {**SIGN_IN, "agent_kind": "ollama"}
        _, said, _, failed = self.run_connect("ABCDE-FGHJK", answer=offer, tty=False)
        self.assertIn("--model", failed)
        self.assertEqual(self.saved()["agent"], "ollama")                 # the used code's sign-in is kept
        install, said, _, failed = self.run_connect(answers=["2"])        # again, no code: it asks now
        self.assertIsNone(failed)
        self.assertIn("1. qwen3:8b", said)
        self.assertEqual(self.saved()["model"], "llama3:70b")
        install, _, _, failed = self.run_connect("--model", "qwen3:8b", tty=False)
        self.assertEqual((failed, self.saved()["model"]), (None, "qwen3:8b"))

    def test_ollama_without_codex_is_refused_before_the_code_is_used(self):
        self.installed = {"claude"}
        _, said, _, failed = self.run_connect("ABCDE-FGHJK", "--agent", "ollama")
        self.assertIn("runs through Codex", failed)
        self.asked.assert_not_called()

    def test_remote_key_is_typed_here_and_kept_only_in_its_0600_file(self):
        offer = {**SIGN_IN, "agent_kind": "remote", "model": "gpt-4.1-mini", "base_url": "https://llm.example.com/v1"}
        install, said, secret, failed = self.run_connect("ABCDE-FGHJK", answer=offer)
        self.assertIsNone(failed)
        secret.assert_called_once()
        self.assertIn("sent only to https://llm.example.com/v1", said)
        key_file = self.home / codex_provider.KEY_FILE
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(codex_provider.load_key(self.home)[0], KEY)
        creds_text = (self.home / "credentials.json").read_text()
        self.assertNotIn(KEY, creds_text)
        self.assertEqual(json.loads(creds_text)["base_url"], "https://llm.example.com/v1")
        self.assertNotIn(KEY, said)                                       # never shown or logged
        self.assertNotIn(KEY, json.dumps(self.asked.call_args.args[0].data.decode()))   # never sent to @pm
        owner, kind, agent, creds = install.args[1:5]
        self.assertEqual(kind, "remote")
        argv = agent_service.command(connect.APP, owner, kind)
        self.assertFalse(any(KEY in a for a in argv))
        service = agent_service.plist(connect.APP, owner, kind, agent, creds, self.home / "logs") + \
            agent_service.unit(connect.APP, owner, kind, agent, creds, self.home / "logs")
        self.assertNotIn(KEY, service)

    def test_remote_keeps_its_key_until_the_address_changes(self):
        offer = {**SIGN_IN, "agent_kind": "remote", "model": "m1", "base_url": "https://llm.example.com/v1"}
        self.run_connect("ABCDE-FGHJK", answer=offer)
        _, _, secret, failed = self.run_connect()                        # a rerun: same address, no prompt
        self.assertIsNone(failed)
        secret.assert_not_called()
        moved = {**offer, "base_url": "https://other.example.net/v1"}
        _, said, secret, _ = self.run_connect("ABCDE-FGHJK", answer=moved, key="sk-new")
        secret.assert_called_once()                                       # the old key never goes to a new address
        self.assertEqual(codex_provider.load_key(self.home)[0], "sk-new")
        _, _, secret, _ = self.run_connect("--new-key", key="sk-newer")
        self.assertEqual(codex_provider.load_key(self.home)[0], "sk-newer")
        _, _, _, failed = self.run_connect("--agent", "codex")            # another kind: no key left behind
        self.assertIsNone(failed)
        self.assertFalse((self.home / codex_provider.KEY_FILE).exists())

    def test_remote_without_a_terminal_never_guesses_a_key(self):
        offer = {**SIGN_IN, "agent_kind": "remote", "model": "m1", "base_url": "https://llm.example.com/v1"}
        install, _, secret, failed = self.run_connect("ABCDE-FGHJK", answer=offer, tty=False)
        self.assertIn("run this in a Terminal", failed)
        secret.assert_not_called()
        self.assertIsNone(install)
        self.assertFalse((self.home / codex_provider.KEY_FILE).exists())

    def test_remote_asks_for_address_and_model_when_pm_named_none(self):
        _, _, _, failed = self.run_connect("ABCDE-FGHJK", "--agent", "remote",
                                           answers=["http://evil.example/v1", "https://llm.example.com/v1/", "gpt-x"])
        self.assertIsNone(failed)
        self.assertEqual((self.saved()["base_url"], self.saved()["model"]), ("https://llm.example.com/v1", "gpt-x"))

    def test_a_kind_or_address_this_computer_wont_use_keeps_only_the_sign_in(self):
        """GPT review: the used code's sign-in is saved first; the odd settings never are."""
        for offer in ({**SIGN_IN, "agent_kind": "gpt"}, {**SIGN_IN, "agent_kind": "remote", "base_url": "http://x.example"}):
            install, _, _, failed = self.run_connect("ABCDE-FGHJK", answer=offer)
            self.assertIn("Your sign-in is saved", failed)
            self.assertIn("--agent KIND", failed)
            self.assertIsNone(install)
            self.assertEqual(self.saved()["password"], SIGN_IN["password"])
            self.assertNotIn("agent", self.saved())
            self.assertNotIn("base_url", self.saved())
            (self.home / "credentials.json").unlink()

    def test_settings_stay_with_their_kind(self):
        """GPT review: another kind's model or address is never reused, nor @pm's for a kind it didn't name."""
        pending = connect_kinds.pending_for
        saved = {**SIGN_IN, "agent": "remote", "model": "gpt-x", "base_url": "https://a.example/v1"}
        self.assertNotIn("model", pending("ollama", saved, {}))
        self.assertEqual(pending("remote", saved, {})["base_url"], "https://a.example/v1")
        self.assertNotIn("model", pending("remote", {**SIGN_IN}, {"agent_kind": "ollama", "model": "qwen3:8b"}))
        self.assertEqual(pending("ollama", {**SIGN_IN}, {"agent_kind": "ollama", "model": "qwen3:8b"})["model"], "qwen3:8b")
        self.assertNotIn("model", pending("ollama", {**SIGN_IN}, {"model": "qwen3:8b"}))   # GPT re-check: only with its kind

    def test_pms_model_survives_a_missing_tool(self):
        """GPT review: stopped for a missing tool, the rerun still has @pm's model."""
        self.installed = {"claude"}
        _, _, _, failed = self.run_connect("ABCDE-FGHJK", answer={**SIGN_IN, "agent_kind": "ollama", "model": "qwen3:8b"})
        self.assertIn("runs through Codex", failed)
        self.installed = {"codex"}
        _, _, _, failed = self.run_connect(tty=False)
        self.assertEqual((failed, self.saved()["model"]), (None, "qwen3:8b"))

    def test_the_persons_own_choice_wins_and_is_said(self):
        install, said, _, _ = self.run_connect("ABCDE-FGHJK", "--agent", "claude",
                                               answer={**SIGN_IN, "agent_kind": "codex"})
        self.assertEqual(install.args[2], "claude")
        self.assertIn("running it as claude", said)

    def test_hermes_is_set_up_in_its_own_process_from_the_file(self):
        runs = []
        with mock.patch.object(connect_kinds, "problem", return_value=None), \
                mock.patch.object(connect_kinds.subprocess, "run", lambda argv, **kw: runs.append((argv, kw)) or
                                  mock.Mock(returncode=0)):
            install, _, _, failed = self.run_connect("ABCDE-FGHJK", answer={**SIGN_IN, "agent_kind": "hermes"})
        self.assertIsNone(failed)
        (argv, kw), = runs
        self.assertEqual(argv[1:], [str(connect.APP / "hermes_local.py"), "--setup"])
        self.assertEqual(kw["env"]["HAGENCY_CREDENTIALS"], str(self.home / "credentials.json"))
        self.assertFalse(any(SIGN_IN["password"] in a for a in argv))
        self.assertEqual(install.args[2], "hermes")
        self.assertEqual(agent_service.command(connect.APP, "newbie1", "hermes")[1:], [str(connect.APP / "hermes_local.py")])

    def test_hermes_missing_keeps_the_sign_in_and_says_what_to_do(self):
        with mock.patch.object(connect_kinds, "problem", lambda k: "install Hermes Agent first" if k == "hermes" else None):
            install, _, _, failed = self.run_connect("ABCDE-FGHJK", answer={**SIGN_IN, "agent_kind": "hermes"})
        self.assertIn("install Hermes Agent first", failed)
        self.assertIn("no code needed", failed)
        self.assertIsNone(install)
        self.assertEqual(self.saved()["agent"], "hermes")


if __name__ == "__main__":
    unittest.main()
