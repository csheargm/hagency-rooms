"""Hermes changing its own code through the hagency MCP server (mcp_custom.py), its button helper
(bridge.py --buttons-only) and the supervisor/profile side. Run: .venv/bin/python -m unittest tests.test_mcp_custom"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HAGENCY_AGENT", "anna-hermes-agent")
os.environ.setdefault("HAGENCY_ROOM", "!r:bridge.localhost")
from adapters.agents import bridge  # noqa: E402
from common import customize  # noqa: E402
from adapters.agents import hermes_profile  # noqa: E402
from mcp_tools import mcp_core as core  # noqa: E402
from mcp_tools import mcp_custom  # noqa: E402
from client import supervise  # noqa: E402

ANNA = "@anna:bridge.localhost"
PRIVATE, SHARED = "!dm:bridge.localhost", "!lounge:bridge.localhost"


class Matrix:
    """Just enough homeserver: two rooms, and what the tools send."""
    def __init__(self, members=None, rule="invite"):
        self.members = members or {PRIVATE: {ANNA, core.USER_ID}, SHARED: {ANNA, core.USER_ID, "@betty:bridge.localhost"}}
        self.rule, self.sent = rule, []

    def api(self, method, path, body=None, token=None):
        if path.endswith("/joined_members"):
            room = next(r for r in self.members if r.replace("!", "%21").replace(":", "%3A") in path or r in path)
            return {"joined": {u: {} for u in self.members[room]}}
        if "/m.room.join_rules/" in path:
            return {"join_rule": self.rule}
        self.sent.append((path, body))
        return {"event_id": f"$e{len(self.sent)}"}


class HermesTools(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.work = self.tmp / "work"
        (self.work / "docs").mkdir(parents=True)
        (self.work / "pm_voice.py").write_text("CAPTION = 'x'\n")
        (self.work / "server" / "__pycache__").mkdir(parents=True)
        (self.work / "server" / "pm_find.py").write_text("X = 1\n")
        (self.work / "server" / "__pycache__" / "pm_find.cpython-312.pyc").write_bytes(b"")
        self.matrix = Matrix()
        self.offers = {}
        for target, name, value in (
                (core, "api", self.matrix.api), (core, "token", lambda: "t"), (core, "ROOM", None),
                (core, "rooms", lambda: list(self.matrix.members)), (mcp_custom, "owner", lambda: "anna"),
                (customize, "work_dir", lambda owner: self.work), (customize, "why_not_use", lambda owner: None),
                (customize, "prepare", mock.AsyncMock(return_value=self.work)),
                (mcp_custom, "SavedDict", lambda path: self.offers)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        mcp_custom._change.clear()

    def test_the_file_list_shows_the_code_in_package_folders(self):
        """GPT review (ADR 0011): only the root's files were listed, so moved code was invisible."""
        files = asyncio.run(mcp_custom.start_code_change("shorter caption"))["files"]
        self.assertIn("server/pm_find.py", files)
        self.assertIn("pm_voice.py", files)
        self.assertFalse([f for f in files if "__pycache__" in f])

    def test_the_offer_lands_in_the_private_room_with_buttons(self):
        started = asyncio.run(mcp_custom.start_code_change("change your code so the caption is shorter"))
        self.assertIn("pm_voice.py", started["files"])
        self.assertEqual(mcp_custom.read_my_code("pm_voice.py")["content"], "CAPTION = 'x'\n")
        self.assertIn("written", mcp_custom.write_my_code({"pm_voice.py": "CAPTION = 'y'\n"}))
        with mock.patch.object(customize, "commit", mock.AsyncMock(return_value="a" * 40)), \
                mock.patch.object(customize, "changed_files", mock.AsyncMock(return_value=["pm_voice.py"])):
            done = asyncio.run(mcp_custom.finish_code_change("Shorter caption."))
        self.assertEqual(done["offered"], "a" * 7)
        message, *reactions = self.matrix.sent
        self.assertIn("%21dm", message[0])                                    # the private room, not the lounge
        self.assertIn("Your version is ready", message[1]["body"])
        self.assertEqual([r[1]["m.relates_to"]["key"] for r in reactions], ["👍", "📤", "🗑️"])
        self.assertEqual(self.offers["$e1"]["room"], PRIVATE)               # the button helper handles the clicks

    def test_no_private_room_no_change(self):
        self.matrix.members.pop(PRIVATE)
        self.assertIn("no private room", asyncio.run(mcp_custom.start_code_change("x"))["error"])

    def test_a_room_others_can_join_isnt_private(self):
        self.matrix.rule = "public"
        self.assertIsNone(mcp_custom.private_room("anna"))

    def test_paths_stay_inside_the_copy(self):
        asyncio.run(mcp_custom.start_code_change("x"))
        for bad in ("../.env", "/etc/passwd", ".git/config", "custom/betty/x.py", ".gitattributes", "a.pth"):
            self.assertIn("error", mcp_custom.write_my_code({bad: "x"}), bad)
            self.assertIn("error", mcp_custom.read_my_code(bad), bad)

    def test_a_symlink_to_git_metadata_is_refused(self):
        """GPT review 20: names were checked before resolving, so a link could reach .git."""
        (self.work / ".git").mkdir()
        (self.work / "meta").symlink_to(self.work / ".git")
        asyncio.run(mcp_custom.start_code_change("x"))
        self.assertIn("error", mcp_custom.write_my_code({"meta/config": "x"}))
        self.assertFalse((self.work / ".git" / "config").exists())

    def test_reserved_names_in_any_case_are_refused(self):
        """GPT review 20: this Mac's disk ignores case, so .GIT is .git."""
        asyncio.run(mcp_custom.start_code_change("x"))
        for bad in (".GIT/config", "Custom/x.py", ".GitAttributes", "a.PTH"):
            self.assertIn("error", mcp_custom.write_my_code({bad: "x"}), bad)

    def test_an_empty_request_is_refused(self):
        """GPT review 20: an empty request crashed the commit message (splitlines()[0])."""
        self.assertIn("error", asyncio.run(mcp_custom.start_code_change("  ")))

    def test_reads_stop_at_the_limit(self):
        (self.work / "big.py").write_text("x" * (mcp_custom.MAX_READ + 10))
        self.assertEqual(len(mcp_custom.read_my_code("big.py")["content"]), mcp_custom.MAX_READ)

    def test_writing_needs_a_started_change(self):
        self.assertIn("error", mcp_custom.write_my_code({"pm_voice.py": "x"}))

    def test_go_back(self):
        with mock.patch.object(customize, "go_back", return_value=True) as back:
            self.assertTrue(mcp_custom.go_back_to_official()["done"])
        back.assert_called_once_with("anna")


class ButtonHelper(unittest.TestCase):
    def test_buttons_only_listens_to_nothing_but_clicks(self):
        b = bridge.Bridge("anna", "codex", buttons_only=True)
        callbacks = []
        b.client.add_event_callback = lambda fn, kind: callbacks.append(kind.__name__)
        with mock.patch.object(bridge.lifecycle, "first_sync", mock.AsyncMock(side_effect=lambda c, n, s, register, f: register())), \
                mock.patch.object(bridge.lifecycle, "serve", mock.AsyncMock()), \
                mock.patch.object(bridge, "agent_password", return_value="x"), \
                mock.patch.object(b.client, "login", mock.AsyncMock(return_value=bridge.LoginResponse("@anna-agent:x", "D", "t"))):
            asyncio.run(b.run())
        self.assertEqual(callbacks, ["ReactionEvent"])   # never answers messages or invites: Hermes does (GPT review 20)


class Supervisor(unittest.TestCase):
    def test_hermes_owners_get_a_button_helper_and_their_chosen_tools(self):
        home = Path(tempfile.mkdtemp())                    # both profiles set up (never this machine's ~/.hermes)
        for name in supervise.hermes_profile.profile_names("anna"):
            (home / "profiles" / name).mkdir(parents=True)
            (home / "profiles" / name / "config.yaml").write_text("model: {}\n")
        with mock.patch.object(supervise.registry, "load", return_value={"anna": {"agent": "hermes"}}), \
                mock.patch.object(supervise.hermes_profile, "HERMES_HOME", home), \
                mock.patch.object(supervise.hermes_profile, "set_repos"):
            kids = {k.name: k for k in supervise.children("codex", False)}
        self.assertEqual(kids["anna-buttons"].argv[-2:], ["anna", "--buttons-only"])
        self.assertIsNone(kids["anna-buttons"].owner)                       # the helper always runs official code
        hermes = kids["anna-agent"]
        with mock.patch.object(customize, "code_dir", return_value=(Path("/live-x"), "d" * 40)), \
                mock.patch.object(supervise.hermes_profile, "set_code_dir") as point, \
                mock.patch("asyncio.create_subprocess_exec", mock.AsyncMock()) as spawn, \
                mock.patch.object(supervise.Child, "pump", mock.MagicMock()), mock.patch("asyncio.create_task"):
            asyncio.run(hermes.start())
        point.assert_called_once_with("annaroom", Path("/live-x"))
        self.assertEqual((spawn.call_args.kwargs["cwd"], hermes.launched), (supervise.ROOT, "d" * 40))

    def test_the_operators_opt_in_reaches_hermes_tools(self):
        """Seen live: Hermes gives its MCP server only the env in its config, so the opt-in never arrived."""
        with mock.patch.dict(os.environ, {"HAGENCY_CUSTOM_SHARED_OK": "1"}):
            self.assertEqual(hermes_profile.repo_env()["HAGENCY_CUSTOM_SHARED_OK"], "1")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HAGENCY_CUSTOM_SHARED_OK", None)
            self.assertNotIn("HAGENCY_CUSTOM_SHARED_OK", hermes_profile.repo_env())

    def test_each_start_refreshes_the_room_rules(self):
        """Seen live: the code-change rule never reached a profile made before it existed."""
        home = Path(tempfile.mkdtemp())
        prof = home / "profiles" / "p"
        prof.mkdir(parents=True)
        (prof / "config.yaml").write_text("    env:\n      HAGENCY_AGENT: 'anna-agent'\n")
        (prof / "SOUL.md").write_text("I am Hermes.\n\nIn Hagency rooms: old rules\n")
        with mock.patch.object(hermes_profile, "HERMES_HOME", home):
            hermes_profile.set_repos("p")
        soul = (prof / "SOUL.md").read_text()
        self.assertTrue(soul.startswith("I am Hermes."))
        self.assertIn("start_code_change", soul)
        self.assertNotIn("old rules", soul)

    def test_a_withdrawn_opt_in_leaves_the_profile(self):
        """GPT review 20: unsetting HAGENCY_CUSTOM_SHARED_OK left the old value in the profile."""
        home = Path(tempfile.mkdtemp())
        cfg = home / "profiles" / "p" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("    env:\n      HAGENCY_AGENT: 'anna-agent'\n      HAGENCY_CUSTOM_SHARED_OK: '1'\n")
        with mock.patch.object(hermes_profile, "HERMES_HOME", home), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HAGENCY_CUSTOM_SHARED_OK", None)
            hermes_profile.set_repos("p")
        self.assertNotIn("HAGENCY_CUSTOM_SHARED_OK", cfg.read_text())
        self.assertIn("HAGENCY_AGENT: 'anna-agent'", cfg.read_text())

    def test_a_profile_hermes_re_saved_still_gets_pointed(self):
        """GPT review 20: Hermes re-saves args as a block list, which the inline-only match missed."""
        home = Path(tempfile.mkdtemp())
        cfg = home / "profiles" / "p" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mcp_servers:\n  hagency:\n    command: /py\n    args:\n    - /old/hagency_mcp.py\n"
                       "    timeout: 300\n    env:\n      HAGENCY_AGENT: anna-agent\n")
        with mock.patch.object(hermes_profile, "HERMES_HOME", home):
            self.assertTrue(hermes_profile.set_code_dir("p", Path("/new")))
        text = cfg.read_text()
        self.assertIn("    args: [/new/hagency_mcp.py]\n    timeout: 300", text)
        self.assertNotIn("/old/", text)
        cfg.write_text("mcp_servers:\n  other:\n    args: [x.py]\n")
        with mock.patch.object(hermes_profile, "HERMES_HOME", home):
            self.assertFalse(hermes_profile.set_code_dir("p", Path("/new")))   # nothing to point: said, not silent

    def test_profile_points_its_tools_at_a_version(self):
        home = Path(tempfile.mkdtemp())
        cfg = home / "profiles" / "p" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mcp_servers:\n  hagency:\n    command: /py\n    args: [/old/hagency_mcp.py]\n"
                       "    env:\n      HAGENCY_AGENT: 'anna-agent'\n")
        with mock.patch.object(hermes_profile, "HERMES_HOME", home):
            hermes_profile.set_code_dir("p", Path("/new"))
            hermes_profile.set_code_dir("p", Path("/new"))
        text = cfg.read_text()
        self.assertIn("args: [/new/hagency_mcp.py]", text)
        self.assertEqual(text.count("HAGENCY_HOME:"), 1)


if __name__ == "__main__":
    unittest.main()
