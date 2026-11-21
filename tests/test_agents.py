"""Unit tests for agent CLI argv/state handling. Run: .venv/bin/python -m unittest tests.test_agents"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.agents import agents


class Argv(unittest.TestCase):
    def test_locked_codex_is_read_only_and_reads_prompt_from_stdin(self):
        argv = agents._codex_argv(Path("/w"), None)
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertEqual(argv[-1], "-")

    def test_locked_claude_limits_tools_and_mcp(self):
        argv = agents._claude_argv(Path("/w"), None)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--strict-mcp-config", argv)

    def test_malformed_saved_session_id_is_not_passed_to_cli(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".bridge-session.json").write_text(json.dumps({"codex": "--dangerously-x"}))
            seen = {}

            async def fake_exec(*argv, **kw):
                seen["argv"] = argv
                proc = mock.Mock(returncode=0)
                out = b'{"type":"thread.started","thread_id":"0123abcd-0000"}\n' \
                      b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'
                proc.communicate = mock.AsyncMock(return_value=(out, b""))
                return proc

            with mock.patch("asyncio.create_subprocess_exec", fake_exec):
                self.assertEqual(asyncio.run(agents.run_turn("codex", Path(d), "x", owner=True)), "hi")
            self.assertNotIn("--dangerously-x", seen["argv"])
            self.assertNotIn("resume", seen["argv"])
            saved = json.loads((Path(d) / ".bridge-session.json").read_text())
            self.assertEqual(saved["codex"], "0123abcd-0000")


class GitFence(unittest.TestCase):
    def test_agent_git_does_not_find_the_enclosing_project_repo(self):
        import subprocess
        import tempfile
        root = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        tree = root / "rooms" / "anna" / "work" / "issue-1" / "tree"
        tree.mkdir(parents=True)
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=tree, env=agents.agent_env(tree),
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, f"git escaped to {r.stdout.strip()}")


class CodexLockdown(unittest.TestCase):
    def home(self, toml):
        d = Path(tempfile.mkdtemp())
        (d / "config.toml").write_text(toml)
        return mock.patch.dict("os.environ", {"CODEX_HOME": str(d)})

    def test_connectors_plugins_web_and_computer_use_off(self):
        with self.home(""):
            argv = " ".join(agents._codex_argv(Path("/tmp/w"), None))
        for flag in ("features.apps=false", "features.plugins=false", "features.computer_use=false",
                     "features.browser_use=false", 'web_search="disabled"'):
            self.assertIn(flag, argv)

    def test_every_configured_mcp_server_switched_off(self):
        with self.home('[mcp_servers.node_repl]\ncommand="x"\n[mcp_servers.computer-use]\ncommand="y"\n'):
            argv = agents._codex_argv(Path("/tmp/w"), "abcdef12-session", write=True)
        self.assertIn("mcp_servers.node_repl.enabled=false", argv)
        self.assertIn("mcp_servers.computer-use.enabled=false", argv)

    def test_unswitchable_server_name_refuses_to_run(self):
        with self.home('[mcp_servers."a b"]\ncommand="x"\n'):
            with self.assertRaises(agents.AgentError):
                agents._codex_argv(Path("/tmp/w"), None)

    def test_unreadable_config_refuses_to_run(self):
        with self.home("not = [valid"):
            with self.assertRaises(agents.AgentError):
                agents._codex_argv(Path("/tmp/w"), None)


if __name__ == "__main__":
    unittest.main()
