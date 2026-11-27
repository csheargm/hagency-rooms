"""ADR 0006: the owner's agent runs their own CLI as they configured it; everything else stays locked.
Run: .venv/bin/python -m unittest tests.test_owner_powers"""
import asyncio
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.agents import agents
from adapters.agents import bridge
from adapters.agents import tool_refusal
from adapters.agents import workdirs
from tests import ROOT

CHAT = "--dangerously-skip-permissions; rm -rf ~ $(whoami)"     # chat text that must never reach argv
LOCKDOWN_CLAUDE = ("--tools", "--strict-mcp-config", "--setting-sources")
OWNER, MEMBER = "@anna:bridge.localhost", "@betty:bridge.localhost"


def codex_home(toml=""):
    d = Path(tempfile.mkdtemp())
    (d / "config.toml").write_text(toml)
    return mock.patch.dict("os.environ", {"CODEX_HOME": str(d)})


def claude_home(settings: dict | None = None):
    d = Path(tempfile.mkdtemp())
    if settings is not None:
        (d / "settings.json").write_text(json.dumps(settings))
    return mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(d)})


def flag(argv, name):
    return argv[argv.index(name) + 1]


class OwnerArgv(unittest.TestCase):
    """Live bug: with only the owner's settings, `claude -p` denied web search and fetch (nobody can
    approve from a room). The owner's turn now gets web, edits and commands without a prompt."""
    def test_claude_owner_turn_works_without_prompts_and_adds_no_lockdown(self):
        with claude_home({"permissions": {"allow": ["Read"]}}):
            argv = agents._claude_argv(Path("/w"), None, owner=True)
        for name in ("WebSearch", "WebFetch", "Edit", "Write", "Bash"):
            self.assertIn(name, flag(argv, "--allowedTools").split(","))
        self.assertEqual(flag(argv, "--permission-mode"), "acceptEdits")   # never "default": that blocks
        for f in (*LOCKDOWN_CLAUDE, "--dangerously-skip-permissions"):
            self.assertNotIn(f, argv)

    def test_an_owner_who_already_bypasses_permissions_keeps_it(self):
        with claude_home({"permissions": {"defaultMode": "bypassPermissions"}}):
            self.assertNotIn("--permission-mode", agents._claude_argv(Path("/w"), None, owner=True))

    def test_claude_owner_turn_resumes_its_session(self):
        with claude_home():
            self.assertEqual(agents._claude_argv(Path("/w"), "abcdef12-x", owner=True)[-2:], ["--resume", "abcdef12-x"])

    def test_codex_owner_turn_never_blocks_and_has_web_and_network(self):
        with codex_home('[mcp_servers.node_repl]\ncommand="x"\n'):
            for sid in (None, "abcdef12-x"):
                argv = agents._codex_argv(Path("/w"), sid, owner=True)
                for c in ('approval_policy="never"', 'sandbox_mode="workspace-write"',
                          "sandbox_workspace_write.network_access=true", 'web_search="live"'):
                    self.assertIn(c, argv)
                self.assertFalse([a for a in argv if "features." in a or "mcp_servers" in a])   # theirs stay on
                self.assertEqual(argv[-1], "-")       # prompt on stdin
            self.assertEqual(agents._codex_argv(Path("/w"), None, owner=True)[-3:], ["-C", "/w", "-"])

    def test_the_owners_explicit_codex_choices_win(self):
        with codex_home('sandbox_mode = "danger-full-access"\nweb_search = "disabled"\n'
                        '[sandbox_workspace_write]\nnetwork_access = false\n'):
            argv = " ".join(agents._codex_argv(Path("/w"), None, owner=True))
        self.assertNotIn("sandbox_mode", argv)
        self.assertNotIn("web_search", argv)
        self.assertNotIn("network_access", argv)
        self.assertIn('approval_policy="never"', argv)    # exec can't ask anyway

    def test_a_codex_permission_profile_of_theirs_is_never_mixed_with_ours(self):
        with codex_home('profile = "careful"\n[profiles.careful]\nsandbox_mode = "read-only"\n'):
            argv = " ".join(agents._codex_argv(Path("/w"), None, owner=True))
        self.assertNotIn("sandbox_mode", argv)
        self.assertNotIn("network_access", argv)

    def test_codex_owner_turn_does_not_need_switchable_mcp_names(self):
        with codex_home('[mcp_servers."a b"]\ncommand="x"\n'):     # locked turns refuse this name
            self.assertFalse([a for a in agents._codex_argv(Path("/w"), None, owner=True) if "mcp_servers" in a])

    def test_locked_turns_are_unchanged(self):
        claude = agents._claude_argv(Path("/w"), None)
        self.assertEqual(claude[claude.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "default")
        build = agents._claude_argv(Path("/w"), None, write=True)
        self.assertEqual(build[build.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")
        with codex_home():
            self.assertIn('sandbox_mode="read-only"', agents._codex_argv(Path("/w"), None))
            self.assertIn('sandbox_mode="workspace-write"', agents._codex_argv(Path("/w"), None, write=True))
            self.assertIn('approval_policy="never"', agents._codex_argv(Path("/w"), None))


def fake_cli(stdout: bytes, seen: dict):
    async def fake_exec(*argv, **kw):
        seen.update(argv=argv, cwd=kw.get("cwd"))
        proc = mock.Mock(returncode=0)

        async def communicate(data):
            seen["stdin"] = data
            return stdout, b""
        proc.communicate = communicate
        return proc
    return mock.patch("asyncio.create_subprocess_exec", fake_exec)


class OwnerTurn(unittest.TestCase):
    def run_claude(self, payload: dict, owner=True):
        seen, d = {}, Path(tempfile.mkdtemp())
        with fake_cli(json.dumps(payload).encode(), seen):
            text = asyncio.run(agents.run_turn("claude", d, CHAT, owner=owner))
        return text, seen, d

    def test_prompt_on_stdin_never_in_argv_and_cwd_is_the_work_folder(self):
        text, seen, d = self.run_claude({"result": "hi", "session_id": "abcdef12-1"})
        self.assertEqual(text, "hi")
        self.assertEqual(seen["stdin"], CHAT.encode())
        self.assertFalse([a for a in seen["argv"] if CHAT in a or "rm -rf" in a])
        self.assertEqual(seen["cwd"], d)

    def test_denied_tools_are_said_plainly_by_name_only(self):
        denials = [{"tool_name": "WebSearch", "tool_input": {"query": "ghp_secretsecret"}},
                   {"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_rsa"}},
                   {"tool_name": "WebSearch"}, {"tool_name": "bad name;rm"}, {"tool_name": "Bash\nrm -rf"},
                   {"tool_name": "Edit\n"}, "junk"]
        text, _, _ = self.run_claude({"result": "I couldn't search.", "session_id": "abcdef12-1",
                                      "permission_denials": denials})
        self.assertTrue(text.startswith("I couldn't search."))
        self.assertIn("I wasn't allowed to use web search or the command line: a setting on your computer blocks it.", text)
        self.assertNotIn("file editing", text)              # "Edit\n" isn't a tool name (GPT review)
        for leaked in ("ghp_", "id_rsa", "bad name", "rm -rf"):
            self.assertNotIn(leaked, text)

    def test_a_tool_outside_the_allowed_set_says_how_to_allow_it(self):
        text, _, _ = self.run_claude({"result": "no", "permission_denials": [{"tool_name": "mcp__x__y"}]})
        self.assertIn("I wasn't allowed to use x from this chat: it needs your OK first", text)
        self.assertIn(tool_refusal.HOW_TO_URL, text)
        self.assertNotIn("settings.json", text)                  # the how-to is on the docs page

    def test_only_denials_is_a_reply_not_an_empty_failure(self):
        text, _, _ = self.run_claude({"result": "", "permission_denials": [{"tool_name": "WebFetch"}]})
        self.assertIn("web pages", text)

    def test_empty_reply_is_still_a_failure(self):
        with self.assertRaises(agents.AgentError):
            self.run_claude({"result": "  "})


class BridgeAnswer(unittest.TestCase):
    """Only the owner's own words get the owner-configured CLI; replies are scrubbed of secrets."""
    def answer(self, sender, reply="ok"):
        b = bridge.Bridge("anna", "claude", allow_members=True)
        b.client.access_token = "syt_bridge_own_token_value"
        calls, sent = [], []

        async def run_turn(backend, workdir, prompt, **kw):
            calls.append(SimpleNamespace(workdir=workdir, prompt=prompt, **kw))
            if isinstance(reply, Exception):
                raise reply
            return reply

        async def send(room_id, body, reply_to=None, **kw):
            sent.append(body)
        b.send, b.keep_typing = send, mock.AsyncMock()
        b.client.room_typing = mock.AsyncMock()
        work = Path(tempfile.mkdtemp())
        with mock.patch.object(bridge.agents, "run_turn", run_turn), \
                mock.patch.object(bridge.room_context, "snapshot", lambda *a: {"status": "", "conversation": ""}), \
                mock.patch.object(workdirs, "WORK_HOME", work):
            room = SimpleNamespace(room_id="!r:x", display_name="Lounge")
            asyncio.run(b.answer(room, SimpleNamespace(sender=sender), f"x ({sender}): hi"))
        return calls[0], sent[0], work

    def test_owner_message_runs_the_owners_cli_outside_the_app(self):
        call, _, work = self.answer(OWNER)
        self.assertTrue(call.owner)
        self.assertEqual(call.workdir.parent, work / "anna")       # ~/.hagency/work/<owner>/<room>
        self.assertNotIn(Path(bridge.ROOT).resolve(), call.workdir.resolve().parents)

    def test_a_members_message_under_allow_members_stays_locked(self):
        call, _, _ = self.answer(MEMBER)
        self.assertFalse(call.owner)

    def test_reply_never_carries_secrets(self):
        _, sent, _ = self.answer(OWNER, reply="token syt_bridge_own_token_value key "
                                              "sk-ant-api03-abcdefghijklmnopqrstuv gh ghp_abcdefgh123")
        for secret in ("syt_bridge_own_token_value", "sk-ant-api03", "ghp_abcdefgh123"):
            self.assertNotIn(secret, sent)
        self.assertEqual(sent.count("<redacted>"), 3)

    def test_an_error_is_scrubbed_before_it_is_cut_short(self):
        """GPT review: room_safe's 300-char cut could split a token so the pattern no longer matched."""
        token = "ghp_" + "A" * 36
        _, sent, _ = self.answer(OWNER, reply=agents.AgentError("x" * 270 + token))
        self.assertNotIn("ghp_AAA", sent)


class LockedSessions(unittest.TestCase):
    """GPT review: a locked turn (a member's question) must not resume the owner's conversation."""
    def turn(self, d: Path, owner: bool, payload: dict) -> tuple[str, dict]:
        seen = {}
        with fake_cli(json.dumps(payload).encode(), seen):
            return asyncio.run(agents.run_turn("claude", d, "q", owner=owner)), seen

    def test_owner_and_locked_turns_keep_separate_sessions(self):
        d = Path(tempfile.mkdtemp())
        self.turn(d, True, {"result": "a", "session_id": "owner123-1"})
        _, seen = self.turn(d, False, {"result": "b", "session_id": "member12-1"})
        self.assertNotIn("owner123-1", seen["argv"])
        _, seen = self.turn(d, True, {"result": "c", "session_id": "owner123-1"})
        self.assertIn("owner123-1", seen["argv"])
        self.assertEqual(json.loads((d / workdirs.SESSION_FILE).read_text()),
                         {"claude": "owner123-1", "claude-locked": "member12-1"})

    def test_a_locked_turns_output_is_left_exactly_as_the_cli_gave_it(self):
        """A review's final VERDICT line (tasks.parse_verdict) must stay the last line."""
        from adapters.agents import tasks
        text, _ = self.turn(Path(tempfile.mkdtemp()), False,
                            {"result": "ok\nVERDICT: APPROVE", "permission_denials": [{"tool_name": "Bash"}]})
        self.assertEqual(tasks.parse_verdict(text), "APPROVE")

    def test_a_state_file_that_isnt_an_object_starts_fresh(self):
        d = Path(tempfile.mkdtemp())
        (d / workdirs.SESSION_FILE).write_text("[1, 2]")
        self.assertEqual(self.turn(d, True, {"result": "hi", "session_id": "abcdef12-1"})[0], "hi")

    def test_locked_codex_builds_never_inherit_network_or_extra_roots(self):
        with codex_home('[sandbox_workspace_write]\nnetwork_access = true\nwritable_roots = ["/"]\n'):
            argv = agents._codex_argv(Path("/w"), None, write=True)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)
        self.assertIn("sandbox_workspace_write.writable_roots=[]", argv)


class SecretSafe(unittest.TestCase):
    def test_a_whole_private_key_is_cut_not_just_its_header(self):
        from common.redact import secret_safe
        pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----"
        self.assertEqual(secret_safe(f"here: {pem} done"), "here: <redacted> done")
        self.assertEqual(secret_safe("plain words, sk-short, AKIA"), "plain words, sk-short, AKIA")
        cut = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA"          # no END line: the body goes too
        self.assertEqual(secret_safe(f"key {cut}"), "key <redacted>")


class LockedCallers(unittest.TestCase):
    def test_task_brain_and_code_change_turns_never_ask_for_owner_powers(self):
        """Loop builds/reviews/plans (tasks.py), @pm's brain and code changes run on untrusted text."""
        for name in ("adapters/agents/tasks.py", "server/pm_brain.py", "adapters/agents/bridge_custom.py",
                     "adapters/agents/understand_task.py"):   # @pm reads with the owner's agent: no tools (ADR 0022)
            src = (ROOT / name).read_text()
            self.assertIn("run_turn(", src, name)
            self.assertNotIn("owner=", src, name)

    def test_task_folders_are_unchanged(self):
        self.assertIn('ROOT / "rooms" / self.owner / "work"', inspect.getsource(bridge.Bridge.run_task))


if __name__ == "__main__":
    unittest.main()
