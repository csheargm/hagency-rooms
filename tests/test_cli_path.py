"""Unit tests: starting an agent CLI without a shell on Windows (cli_path.py), with the platform
mocked. Run: .venv/bin/python -m unittest tests.test_cli_path"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.agents import agents
from adapters.agents import cli_path
from client import connect

# npm's cmd-shim output, as `npm i -g @openai/codex` writes it (its last line is what matters).
CODEX_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
'''
EXE_SHIM = '@ECHO off\r\n"%~dp0\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n'


class NpmFolder:
    """A fake %APPDATA%\\npm with codex.cmd (a node script) and claude.cmd (a package's .exe)."""
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "codex.cmd").write_text(CODEX_SHIM)
        (self.dir / "claude.cmd").write_text(EXE_SHIM)
        for part in (("@openai", "codex", "bin", "codex.js"), ("@anthropic-ai", "claude-code", "bin", "claude.exe")):
            target = self.dir.joinpath("node_modules", *part)
            target.parent.mkdir(parents=True)
            target.write_text("")

    def which(self, name):
        if name == "node":
            return r"C:\Program Files\nodejs\node.EXE"
        found = self.dir / f"{name}.cmd"
        return str(found) if found.exists() else None


def on_windows(npm: NpmFolder):
    return mock.patch.object(cli_path, "windows", lambda: True), mock.patch.object(cli_path.shutil, "which", npm.which)


class Windows(unittest.TestCase):
    def setUp(self):
        self.npm = NpmFolder()
        for p in on_windows(self.npm):
            p.start()
            self.addCleanup(p.stop)

    def test_an_npm_node_wrapper_starts_node_with_its_script(self):
        script = self.npm.dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        self.assertEqual(cli_path.command("codex"), [r"C:\Program Files\nodejs\node.EXE", str(script.resolve())])

    def test_a_relative_path_entry_still_gives_the_absolute_checked_script(self):
        """GPT review (batch 5+6): which() through a relative PATH entry gave a relative script, which
        node then read from the turn's cwd (the checkout): not found, or a checkout file run instead."""
        script = (self.npm.dir / "node_modules" / "@openai" / "codex" / "bin" / "codex.js").resolve()
        here = os.getcwd()
        os.chdir(self.npm.dir.parent)
        self.addCleanup(os.chdir, here)
        relative = lambda n: os.path.join(self.npm.dir.name, f"{n}.cmd") if n != "node" else self.npm.which(n)
        with mock.patch.object(cli_path.shutil, "which", relative):
            node, found = cli_path.command("codex")
            exe = cli_path.command("claude")[0]
        self.assertEqual(found, str(script))
        self.assertTrue(Path(exe).is_absolute(), exe)
        with mock.patch.object(cli_path.shutil, "which", lambda n: r"bin\node.exe" if n == "node" else self.npm.which(n)):
            self.assertEqual(cli_path.command("codex")[0], os.path.abspath(r"bin\node.exe"))   # GPT re-check: node too

    def test_the_wrappers_own_node_wins(self):
        (self.npm.dir / "node.exe").write_text("")
        self.assertEqual(cli_path.command("codex")[0], str((self.npm.dir / "node.exe").resolve()))

    def test_an_npm_wrapper_for_a_program_starts_that_program(self):
        exe = self.npm.dir / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        self.assertEqual(cli_path.command("claude"), [str(exe.resolve())])

    def test_a_real_program_or_nothing_is_left_as_it_is(self):
        with mock.patch.object(cli_path.shutil, "which", lambda n: r"C:\Users\a\.local\bin\claude.exe"):
            self.assertEqual(cli_path.command("claude"), [r"C:\Users\a\.local\bin\claude.exe"])
        with mock.patch.object(cli_path.shutil, "which", lambda n: None):
            self.assertEqual(cli_path.command("codex"), ["codex"])     # the start then fails as before

    def test_an_unreadable_or_escaping_wrapper_is_refused(self):
        for text in ("@echo off\r\ncall something-else %*\r\n", '"%dp0%\\..\\..\\evil.js" %*'):
            (self.npm.dir / "codex.cmd").write_text(text)
            with self.assertRaises(FileNotFoundError):
                cli_path.command("codex")
            self.assertFalse(cli_path.startable("codex"))

    def test_no_way_out_of_the_wrappers_folder_and_never_another_wrapper(self):
        """GPT review: a drive-qualified part, a link out, a node.cmd, or a decoy line above."""
        outside = Path(tempfile.mkdtemp()) / "evil.js"
        outside.write_text("")
        (self.npm.dir / "node_modules" / "link.js").symlink_to(outside)
        for text in ('"%dp0%\\C:\\evil.js" %*', '"%dp0%\\node_modules\\link.js" %*'):
            (self.npm.dir / "codex.cmd").write_text(text)
            with self.assertRaises(FileNotFoundError, msg=text):
                cli_path.command("codex")
        (self.npm.dir / "codex.cmd").write_text('rem "%dp0%\\node_modules\\link.js" %*\r\n' + CODEX_SHIM)
        self.assertTrue(cli_path.command("codex")[1].endswith("codex.js"))   # the last command line counts
        (self.npm.dir / "codex.cmd").write_text(CODEX_SHIM + 'rem "%dp0%\\node_modules\\link.js" %*\r\n')
        self.assertTrue(cli_path.command("codex")[1].endswith("codex.js"))   # GPT re-check: nor a comment below
        (self.npm.dir / "codex.cmd").write_text(':: "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n'
                                                'start-something-else %*\r\n')
        with self.assertRaises(FileNotFoundError):
            cli_path.command("codex")                          # a comment never makes a wrapper look valid
        (self.npm.dir / "codex.cmd").write_text(CODEX_SHIM)
        with mock.patch.object(cli_path.shutil, "which", lambda n: r"C:\nvm\node.CMD" if n == "node" else self.npm.which(n)), \
                self.assertRaisesRegex(FileNotFoundError, "node.exe"):
            cli_path.command("codex")

    def test_a_turn_starts_the_wrapped_program_with_no_shell_and_the_prompt_on_stdin(self):
        """Windows: `codex` by bare name isn't found and a .cmd would run through cmd.exe."""
        seen = {}

        async def fake_exec(*argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            proc = mock.Mock(returncode=0)
            out = b'{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}\n'

            async def communicate(data=None):
                seen["stdin"] = data
                return out, b""
            proc.communicate = communicate
            return proc
        prompt = 'what is 2+2? & del /q C:\\ | "%PATH%"'
        with tempfile.TemporaryDirectory() as d, mock.patch("asyncio.create_subprocess_exec", fake_exec), \
                mock.patch.dict("os.environ", {"CODEX_HOME": tempfile.mkdtemp()}):
            self.assertEqual(asyncio.run(agents.run_turn("codex", Path(d), prompt, fresh=True, owner=True)), "hi")
        self.assertTrue(seen["argv"][0].endswith("node.EXE") and seen["argv"][1].endswith("codex.js"), seen["argv"])
        self.assertEqual(seen["argv"][2], "exec")
        self.assertNotIn("shell", seen["kw"])
        self.assertFalse(any(a.lower().endswith((".cmd", ".bat")) for a in seen["argv"]))
        self.assertFalse(any("2+2" in a for a in seen["argv"]))                     # chat text never in argv
        self.assertEqual(seen["stdin"], prompt.encode())

    def test_connect_accepts_an_npm_install(self):
        self.assertEqual(connect.pick_backend("codex", None), "codex")


class ElsewhereNothingChanges(unittest.TestCase):
    def test_macos_and_linux_start_the_bare_name(self):
        with mock.patch.object(cli_path, "windows", lambda: False), \
                mock.patch.object(cli_path.shutil, "which", lambda n: "/usr/local/bin/codex"):
            self.assertEqual(cli_path.command("codex"), ["codex"])


if __name__ == "__main__":
    unittest.main()
