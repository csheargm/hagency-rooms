"""Unit + turn tests: the agent kinds' contract (agent_kinds.py), Codex with another model provider
(codex_provider.py, agents.run_turn) and Ollama on this computer (ollama_local.py), with every CLI
and HTTP call mocked. Run: .venv/bin/python -m unittest tests.test_agent_kinds"""
import asyncio
import io
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import agent_kinds
from adapters.agents import agents
from adapters.agents import codex_provider
from client import ollama_local
from adapters.agents import owner_cli
from tests import ROOT

KEY = "sk-test-0123456789abcdef"
REMOTE = {"model": "gpt-4.1-mini", "base_url": "https://llm.example.com/v1"}


class Contract(unittest.TestCase):
    def test_every_kind_is_described_once(self):
        listed = agent_kinds.kinds()
        self.assertEqual([k["kind"] for k in listed], list(agent_kinds.KINDS))
        self.assertEqual({k["kind"] for k in listed if not k["windows"]}, {"hermes"})
        for k in listed:
            self.assertTrue(k["name"] and k["needs"] and k["program"] in ("codex", "claude", "hermes"))

    def test_no_kind_in_the_connect_info_means_todays_behaviour(self):
        self.assertEqual(agent_kinds.from_connect_info({"user_id": "@a-agent:s", "password": "p"}), {})

    def test_the_connect_info_names_kind_model_and_address(self):
        got = agent_kinds.from_connect_info({"agent_kind": "remote", **REMOTE, "base_url": REMOTE["base_url"] + "/"})
        self.assertEqual(got, {"agent_kind": "remote", **REMOTE})
        self.assertEqual(agent_kinds.from_connect_info({"agent_kind": "ollama", "model": "qwen3:8b"}),
                         {"agent_kind": "ollama", "model": "qwen3:8b"})

    def test_wrong_values_are_refused_not_dropped(self):
        for bad in ({"agent_kind": "gpt"}, {"agent_kind": 3}, {"model": "-m evil"}, {"model": "a b"},
                    {"model": "x" * 200}, {"model": "../etc"}, {"base_url": "http://llm.example.com/v1"},
                    {"base_url": "https://user:pw@llm.example.com"}, {"base_url": "https://h/v1?k=1"},
                    {"base_url": 'https://h/v1"; x="'}, {"base_url": "https://h/v1\\"}, {"base_url": "https://h:99999"},
                    {"base_url": "file:///etc/passwd"}, {"base_url": "https://h/ v1"}):
            with self.assertRaises(agent_kinds.BadConnectInfo, msg=bad):
                agent_kinds.from_connect_info(bad)

    def test_local_services_may_use_plain_http(self):
        for ok in ("http://localhost:8000/v1", "http://127.0.0.1:1234/v1", "https://openrouter.ai/api/v1"):
            self.assertTrue(agent_kinds.valid_base_url(ok), ok)

    def test_the_windows_installer_knows_the_same_kinds_and_model_names(self):
        ps1 = (ROOT / "client" / "connect.ps1").read_text()
        kinds = re.search(r"\[ValidateSet\(([^)]*)\)\]\[string\]\$Agent", ps1).group(1)
        self.assertEqual(re.findall(r"'([a-z]+)'", kinds), [k for k in agent_kinds.KINDS if k not in agent_kinds.NOT_ON_WINDOWS])
        model = re.search(r"\[ValidatePattern\('([^']*)'\)\]\[string\]\$Model", ps1).group(1)
        self.assertEqual(model, agent_kinds.MODEL_RE.pattern.removesuffix("$") + "\\z")   # .NET: \z, not a "\n" before $


class Provider(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def test_ollama_is_codexs_own_provider_with_the_model(self):
        self.assertEqual(codex_provider.args("ollama", {"model": "qwen3:8b"}),
                         ["-c", 'model_provider="ollama"', "-m", "qwen3:8b"])
        self.assertEqual(codex_provider.env("ollama", None, {}), {})

    def test_remote_names_the_key_variable_never_the_key(self):
        args = codex_provider.args("remote", REMOTE)
        self.assertIn(f'model_providers.hagency_remote.base_url="{REMOTE["base_url"]}"', args)
        self.assertIn('model_providers.hagency_remote.wire_api="responses"', args)
        self.assertIn(f'model_providers.hagency_remote.env_key="{codex_provider.KEY_ENV}"', args)
        self.assertIn(f'shell_environment_policy.set.{codex_provider.KEY_ENV}=""', args)   # its shells can't print it
        self.assertEqual(args[-2:], ["-m", REMOTE["model"]])

    def test_a_damaged_saved_setting_is_refused(self):
        for creds in ({}, {"model": "-x"}, {**REMOTE, "base_url": 'https://h/"x'}):
            with self.assertRaises(codex_provider.ProviderError):
                codex_provider.args("remote", creds)

    def test_the_key_file_is_only_mine(self):
        path = codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(codex_provider.load_key(self.folder), (KEY, REMOTE["base_url"]))
        self.assertEqual(codex_provider.env("remote", str(self.folder / "credentials.json"), REMOTE), {codex_provider.KEY_ENV: KEY})
        path.chmod(0o644)
        with self.assertRaises(codex_provider.ProviderError):
            codex_provider.load_key(self.folder)

    def test_odd_keys_are_not_saved(self):
        for bad in ("", "two words", "line\nbreak", "x" * 5000):
            with self.assertRaises(codex_provider.ProviderError):
                codex_provider.save_key(self.folder, bad, REMOTE["base_url"])
        self.assertIsNone(codex_provider.load_key(self.folder))
        with self.assertRaises(codex_provider.ProviderError):
            codex_provider.env("remote", str(self.folder / "credentials.json"), REMOTE)


class Turns(unittest.TestCase):
    """agents.run_turn for the ollama / remote kinds: Codex's own argv, plus the provider."""
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.folder = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.folder / "codex"),
                                           "HAGENCY_CREDENTIALS": str(self.folder / "credentials.json")})
        env.start()
        self.addCleanup(env.stop)

    def turn(self, kind, creds, owner=True, code=0, out=None, err=b""):
        seen = {}
        out = out if out is not None else (b'{"type":"thread.started","thread_id":"0123abcd-0000"}\n'
                                           b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n')

        async def fake_exec(*argv, **kw):
            seen.update(argv=list(argv), env=kw["env"])
            proc = mock.Mock(returncode=code)
            proc.communicate = mock.AsyncMock(return_value=(out, err))
            return proc
        with mock.patch.object(agents.bridge_config, "CREDENTIALS", creds), \
                mock.patch("asyncio.create_subprocess_exec", fake_exec):
            reply = asyncio.run(agents.run_turn(kind, self.work, "what's new?", owner=owner))
        return reply, seen

    def test_an_ollama_owner_turn_is_the_owners_codex_on_their_model(self):
        reply, seen = self.turn("ollama", {"model": "qwen3:8b"})
        self.assertEqual(reply, "hi")
        argv = seen["argv"]
        self.assertEqual(argv[:2], ["codex", "exec"])
        self.assertIn('model_provider="ollama"', argv)
        self.assertEqual(argv[argv.index("-m") + 1], "qwen3:8b")
        self.assertIn('approval_policy="never"', argv)                   # ADR 0006's owner powers
        self.assertIn('web_search="disabled"', argv)                     # no OpenAI-hosted search there
        self.assertNotIn('web_search="live"', argv)

    def test_an_owner_turn_on_another_provider_drops_openai_hosted_features(self):
        """Live test 2026-11-23: the owner's plugins and computer/browser use made a 147k-token request,
        past llama3.2's context; Ollama refused every turn. With them off: about 2.6k tokens."""
        for kind, creds in (("ollama", {"model": "qwen3:8b"}), ("remote", REMOTE)):
            if kind == "remote":
                codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
            _, seen = self.turn(kind, creds)
            for feature in agents.CODEX_FEATURES_OFF:
                self.assertIn(f"features.{feature}=false", seen["argv"], (kind, feature))
        _, seen = self.turn("codex", {})                              # their own Codex keeps all of it
        self.assertNotIn("features.plugins=false", seen["argv"])

    def test_a_failure_says_the_clis_own_error_not_its_log_lines(self):
        log = "2026-11-23T06:24:21.548588Z ERROR codex_models_manager::manager: failed to refresh available models"
        out = "\n".join([log, log, "ERROR: Reconnecting... 5/5",
                         "ERROR: stream disconnected before completion: request (147017 tokens) exceeds the available context size"])
        self.assertEqual(agents.failure_reason(out), "stream disconnected before completion: request (147017 tokens) "
                                                     "exceeds the available context size")
        self.assertEqual(agents.failure_reason(f"{log}\nboom\n{log}"), "boom")
        self.assertEqual(agents.failure_reason(log), log)                 # nothing else: the log line, not nothing

    def test_a_credential_in_the_clis_error_never_reaches_the_room(self):
        """GPT review: the ERROR line is chosen from anywhere in the output, so it may hold a key."""
        with self.assertRaises(agents.AgentError) as e:
            self.turn("codex", {}, code=1, out=b"",
                      err=b"ERROR: auth failed for key sk-abcdefghijklmnopqrstuvwxyz0123\nshutting down")
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz0123", str(e.exception))
        self.assertIn("auth failed", str(e.exception))

    def test_a_locked_turn_keeps_the_lockdown(self):
        _, seen = self.turn("ollama", {"model": "qwen3:8b"}, owner=False)
        self.assertIn('sandbox_mode="read-only"', seen["argv"])
        self.assertIn("features.apps=false", seen["argv"])

    def test_a_remote_key_travels_only_in_the_environment(self):
        codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
        _, seen = self.turn("remote", REMOTE)
        self.assertFalse(any(KEY in a for a in seen["argv"]))
        self.assertEqual(seen["env"][codex_provider.KEY_ENV], KEY)
        self.assertNotIn(codex_provider.KEY_ENV, os.environ)              # the agent adapter itself never holds it

    def test_codex_kind_gets_no_provider_and_no_key(self):
        codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
        _, seen = self.turn("codex", REMOTE)
        self.assertNotIn("-m", seen["argv"])
        self.assertNotIn(codex_provider.KEY_ENV, seen["env"])
        self.assertIn('web_search="live"', seen["argv"])

    def test_no_key_or_model_says_what_to_do(self):
        with self.assertRaisesRegex(agents.AgentError, "connect.py again"):
            self.turn("remote", REMOTE)
        with self.assertRaisesRegex(agents.AgentError, "connect.py again"):
            self.turn("ollama", {})

    def test_an_error_that_echoes_the_key_never_carries_it(self):
        codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
        with self.assertRaises(agents.AgentError) as e:
            self.turn("remote", REMOTE, code=1, out=b"", err=f"401: bad key {KEY}".encode())
        self.assertNotIn(KEY, str(e.exception))

    def test_a_reply_that_echoes_the_key_never_carries_it(self):
        """GPT review: the service gets the key and could put it in a reply bound for the room."""
        codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
        reply, _ = self.turn("remote", REMOTE, out=json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": f"your key is {KEY}"}}).encode())
        self.assertEqual(reply, "your key is ***")
        quoted = 'sk-"q\\uote'                                          # GPT re-check: as JSON would spell it
        self.assertEqual(codex_provider.scrub(json.dumps({"error": quoted}), {codex_provider.KEY_ENV: quoted}),
                         '{"error": "***"}')

    def test_a_key_is_only_sent_to_the_address_it_was_typed_for(self):
        """GPT review: during a reconnect the running agent adapter still has the old address."""
        codex_provider.save_key(self.folder, KEY, "https://new.example.com/v1")
        with self.assertRaisesRegex(agents.AgentError, "another service address"):
            self.turn("remote", REMOTE)

    def test_a_conversation_never_resumes_at_another_service(self):
        codex_provider.save_key(self.folder, KEY, REMOTE["base_url"])
        self.turn("remote", REMOTE)
        state = json.loads((self.work / ".bridge-session.json").read_text())
        (key,) = state
        self.assertTrue(key.startswith("remote:"))
        other = {**REMOTE, "base_url": "https://other.example.com/v1"}
        self.assertNotEqual(codex_provider.session_tag("remote", other), key.removeprefix("remote"))
        self.assertEqual(codex_provider.session_tag("ollama", other), "")

    def test_values_with_a_trailing_line_break_are_refused(self):
        """GPT review: `$` matches before a final newline; these go into TOML strings and argv."""
        self.assertFalse(agent_kinds.valid_model("qwen3:8b\n"))
        self.assertFalse(agent_kinds.valid_base_url("https://h.example/v1\n"))
        self.assertFalse(agent_kinds.valid_base_url("https://[::1/v1"))      # malformed brackets: no exception

    def test_owner_args_keep_their_own_web_choice(self):
        (self.folder / "codex").mkdir()
        (self.folder / "codex" / "config.toml").write_text('web_search = "live"\n')
        self.assertFalse(any("web_search" in a for a in owner_cli.codex_owner_args(hosted_web=False)))   # theirs stays


def tags(models) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({"models": models}).encode()
    return mock.Mock(return_value=response)


MODELS = [{"name": "qwen3:8b", "capabilities": ["completion", "tools"]},
          {"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com", "capabilities": ["completion", "tools"]},
          {"name": "x/z-image-turbo:latest", "capabilities": ["image"]},
          {"name": "gemma:2b", "capabilities": ["completion"]},
          {"name": "--evil", "capabilities": ["completion", "tools"]}, {"name": 7}]


class Ollama(unittest.TestCase):
    def test_installed_models_come_from_api_tags_on_this_computer(self):
        opener = tags(MODELS)
        got = ollama_local.installed_models(opener)
        self.assertEqual(opener.call_args.args[0], "http://localhost:11434/api/tags")
        self.assertEqual([m["name"] for m in got], ["qwen3:8b", "gpt-oss:120b-cloud", "x/z-image-turbo:latest", "gemma:2b"])
        self.assertEqual([m["name"] for m in got if m["chat"]], ["qwen3:8b", "gpt-oss:120b-cloud"])

    def test_not_running_says_how_to_start_it(self):
        with self.assertRaisesRegex(ollama_local.OllamaError, "ollama.com"):
            ollama_local.installed_models(mock.Mock(side_effect=OSError("refused")))

    def test_the_model_passed_in_must_be_pulled_and_able_to_use_tools(self):
        models = ollama_local.installed_models(tags(MODELS))
        self.assertEqual(ollama_local.choose("qwen3:8b", models, tty=False), "qwen3:8b")
        for missing in ("llama3:70b", "gemma:2b", "x/z-image-turbo:latest"):
            with self.assertRaisesRegex(ollama_local.OllamaError, "ollama pull"):
                ollama_local.choose(missing, models, tty=True)

    def test_a_terminal_picks_from_the_list_and_without_one_it_refuses(self):
        models = ollama_local.installed_models(tags(MODELS))
        answers = iter(["9", "x", "2"])
        with mock.patch("sys.stdout", io.StringIO()) as shown:
            self.assertEqual(ollama_local.choose(None, models, ask=lambda _: next(answers), tty=True), "gpt-oss:120b-cloud")
        self.assertIn("1. qwen3:8b", shown.getvalue())
        self.assertIn("runs on ollama.com", shown.getvalue())
        self.assertNotIn("image", shown.getvalue())
        with self.assertRaisesRegex(ollama_local.OllamaError, "--model"):
            ollama_local.choose(None, models, ask=lambda _: self.fail("asked without a terminal"), tty=False)
        with self.assertRaisesRegex(ollama_local.OllamaError, "ollama pull"):
            ollama_local.choose(None, [], tty=True)


if __name__ == "__main__":
    unittest.main()
