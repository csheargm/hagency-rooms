"""The agent's work folders live outside the app (~/.hagency/work/<owner>/), old ones move there with
their sessions, and the owner's Hermes gets their own tool settings (ADR 0006).
Run: .venv/bin/python -m unittest tests.test_workdirs"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.agents import hermes_profile
from adapters.agents import workdirs

ROOM = "!gkon9:bridge.localhost"
CODEX_ID, CLAUDE_ID = "0123abcd-codex", "4567efab-claude"


class FolderSetup(unittest.TestCase):
    """A temporary app and work home; no tests of its own, so subclasses don't run each other's twice."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.app = self.tmp / "app"
        for p in (mock.patch.object(workdirs, "WORK_HOME", self.tmp / "home" / ".hagency" / "work"),
                  mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude")})):
            p.start()
            self.addCleanup(p.stop)

    def old_folder(self, state: dict | None) -> Path:
        old = self.app / "rooms" / "anna" / workdirs.room_key(ROOM)
        (old / "projects" / "repo").mkdir(parents=True)
        (old / "projects" / "repo" / "README.md").write_text("hi")
        if state is not None:
            (old / workdirs.SESSION_FILE).write_text(json.dumps(state))
        return old


class Folders(FolderSetup):
    def test_new_folder_is_outside_the_app(self):
        d = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertEqual(d, workdirs.WORK_HOME / "anna" / workdirs.room_key(ROOM))
        self.assertNotIn(self.app, d.parents)

    def test_ids_that_sanitize_alike_stay_apart(self):
        self.assertNotEqual(workdirs.room_key("!a:b.c"), workdirs.room_key("!a_b:c"))

    def test_an_old_folder_moves_with_its_sessions(self):
        old = self.old_folder({"codex": CODEX_ID, "claude": CLAUDE_ID})
        src = workdirs.claude_project(old)
        src.mkdir(parents=True)
        (src / f"{CLAUDE_ID}.jsonl").write_text('{"x":1}\n')
        new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertFalse(old.exists())
        self.assertEqual((new / "projects" / "repo" / "README.md").read_text(), "hi")
        self.assertEqual(json.loads((new / workdirs.SESSION_FILE).read_text()),
                         {"codex": CODEX_ID, "claude": CLAUDE_ID})
        self.assertEqual((workdirs.claude_project(new) / f"{CLAUDE_ID}.jsonl").read_text(), '{"x":1}\n')

    def test_a_claude_session_without_its_transcript_is_dropped_not_broken(self):
        self.old_folder({"codex": CODEX_ID, "claude": CLAUDE_ID})
        new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertEqual(json.loads((new / workdirs.SESSION_FILE).read_text()), {"codex": CODEX_ID})

    def test_a_malformed_session_id_is_never_used_as_a_path(self):
        self.old_folder({"claude": "../../etc/passwd"})
        new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertEqual(json.loads((new / workdirs.SESSION_FILE).read_text()), {"claude": "../../etc/passwd"})
        self.assertFalse((self.tmp / "claude").exists())     # nothing copied anywhere

    def test_moves_once_and_never_over_a_newer_folder(self):
        old = self.old_folder({"codex": CODEX_ID})
        newer = workdirs.WORK_HOME / "anna" / workdirs.room_key(ROOM)
        newer.mkdir(parents=True)
        self.assertFalse(workdirs.migrate(old, newer))
        self.assertTrue(old.exists())
        self.assertFalse((newer / workdirs.SESSION_FILE).exists())

    def test_a_move_across_disks_copies_then_swaps_in(self):
        old = self.old_folder({"codex": CODEX_ID})
        real_rename = os.rename
        calls = []

        def rename(a, b):
            calls.append(a)
            if Path(a) == old:
                raise OSError(18, "Invalid cross-device link")
            return real_rename(a, b)
        with mock.patch.object(workdirs.os, "rename", rename):
            new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertEqual(json.loads((new / workdirs.SESSION_FILE).read_text()), {"codex": CODEX_ID})
        self.assertFalse(old.exists())
        self.assertFalse(new.with_name(new.name + ".moving").exists())

    def test_a_failed_move_starts_fresh_and_keeps_the_old_folder(self):
        old = self.old_folder({"codex": CODEX_ID})
        with mock.patch.object(workdirs, "_move", side_effect=OSError("disk full")):
            new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertTrue(old.exists())
        self.assertFalse(new.exists())


class MigrationEdges(FolderSetup):
    """GPT review: symlinks, non-cross-disk errors, session subfolders, odd state, relative roots."""
    def test_a_symlinked_old_folder_is_never_adopted(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        link = self.app / "rooms" / "anna" / workdirs.room_key(ROOM)
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertFalse(new.exists())
        self.assertTrue(link.is_symlink())

    def test_only_a_cross_disk_error_copies(self):
        old = self.old_folder({"codex": CODEX_ID})
        with mock.patch.object(workdirs.os, "rename", side_effect=PermissionError(13, "denied")), \
                mock.patch.object(workdirs.shutil, "copytree") as copy:
            self.assertFalse(workdirs.migrate(old, workdirs.WORK_HOME / "anna" / "x"))
        copy.assert_not_called()
        self.assertTrue(old.exists())

    def test_the_sessions_own_folder_comes_along(self):
        old = self.old_folder({"claude": CLAUDE_ID})
        src = workdirs.claude_project(old)
        (src / CLAUDE_ID / "subagents").mkdir(parents=True)
        (src / f"{CLAUDE_ID}.jsonl").write_text("{}\n")
        (src / CLAUDE_ID / "subagents" / "a.jsonl").write_text("{}\n")
        new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertTrue((workdirs.claude_project(new) / CLAUDE_ID / "subagents" / "a.jsonl").exists())

    def test_a_state_file_that_isnt_an_object_is_dropped(self):
        old = self.old_folder(None)
        (old / workdirs.SESSION_FILE).write_text("[1]")
        new = workdirs.room_dir("anna", ROOM, legacy_root=self.app)
        self.assertFalse((new / workdirs.SESSION_FILE).exists())

    def test_the_work_home_is_absolute(self):
        import importlib
        with mock.patch.dict("os.environ", {"HAGENCY_DIR": "rel/.hagency"}):
            self.assertTrue(importlib.reload(workdirs).WORK_HOME.is_absolute())
        importlib.reload(workdirs)


class HermesOwnerTools(unittest.TestCase):
    def tools(self, config: str | None) -> str:
        home = Path(tempfile.mkdtemp())
        if config is not None:
            (home / "config.yaml").write_text(config)
        with mock.patch.object(hermes_profile, "HERMES_HOME", home), \
                mock.patch.object(workdirs, "WORK_HOME", Path("/w")):
            return hermes_profile.owner_tools("anna")

    def test_the_owners_own_settings_come_along(self):
        out = self.tools("toolsets:\n- hermes-cli\n- web\nplatform_toolsets:\n  cli:\n  - hermes-cli\n  matrix:\n"
                         "  - hermes-matrix\n  - browser\n  slack:\n  - hermes-slack\napprovals:\n  mode: smart\n"
                         "  timeout: 60\nterminal:\n  backend: docker\n  cwd: .\n  timeout: 180\nother: 1\n")
        self.assertIn("toolsets: [hermes-cli, web]", out)
        self.assertIn("  matrix: [hermes-matrix, browser]", out)
        self.assertIn("approvals:\n  mode: smart\n  timeout: 60", out)
        self.assertIn("  backend: docker", out)
        self.assertNotIn("cwd: .", out)
        self.assertTrue(out.endswith("  cwd: '/w/anna/hermes'"))   # the terminal never works in the app
        self.assertNotIn("hermes-slack", out)

    def test_hermes_defaults_when_the_owner_set_nothing(self):
        out = self.tools(None)
        self.assertIn("toolsets: [hermes-cli]", out)
        self.assertIn("  matrix: [hermes-matrix]", out)
        self.assertIn("approvals:\n  mode: manual", out)

    def test_odd_toolset_names_never_reach_the_yaml(self):
        out = self.tools("platform_toolsets:\n  matrix: [hermes-matrix, \"x]\\nevil: 1\"]\n")
        self.assertNotIn("evil", out)
        self.assertIn("  matrix: [hermes-matrix]", out)

    def test_a_restrictive_choice_is_never_widened(self):
        """GPT review: an empty list, or one with a comment after it, fell back to hermes-matrix."""
        self.assertIn("  matrix: []", self.tools("platform_toolsets:\n  matrix: []\n"))
        out = self.tools("toolsets: [hermes-cli]  # mine\nplatform_toolsets:\n  matrix: [todo, memory]  # no terminal\n")
        self.assertIn("  matrix: [todo, memory]", out)
        self.assertIn("toolsets: [hermes-cli]", out)

    def test_inline_mappings_are_honoured(self):
        """GPT review 2: `platform_toolsets: {matrix: [todo]}` was read as unset and widened."""
        out = self.tools("platform_toolsets: {matrix: [todo], cli: [hermes-cli]}\nterminal: {backend: docker}\n")
        self.assertIn("  matrix: [todo]", out)
        self.assertIn("terminal: {backend: docker}", out)
        self.assertNotIn("cwd:", out)

    def test_inline_approvals_and_a_quote_in_the_path_stay_valid(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.yaml").write_text("approvals: {mode: smart}\n")
        with mock.patch.object(hermes_profile, "HERMES_HOME", home), \
                mock.patch.object(workdirs, "WORK_HOME", Path("/w/o'brien")):
            out = hermes_profile.owner_tools("anna")
        self.assertIn("approvals: {mode: smart}", out)
        self.assertNotIn("mode: manual", out)
        self.assertIn("cwd: '/w/o''brien/anna/hermes'", out)


if __name__ == "__main__":
    unittest.main()
