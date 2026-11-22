"""Changing your agent's own code from your private room (customize.py, bridge_custom.py).
Run: .venv/bin/python -m unittest tests.test_customize
TODO(R4): over 300 lines; split on the seam between Sandbox/PrivateCopy (customize.py) and
PullRequest/InChat/Supervisor (bridge_custom.py, supervise.py)."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.agents import bridge
from adapters.agents import bridge_custom
from common import customize
from common import registry
from client import supervise

BETTY, AGENT, ANNA = "@betty:x", "@betty-agent:x", "@anna:x"
PASSING = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


class Sandbox(unittest.TestCase):
    """A throwaway "official" repo standing in for this folder; betty is its only owner."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        root = self.tmp / "official"
        root.mkdir()
        for name, text in (("test_x.py", PASSING), ("caption.py", "CAPTION = 'Transcript'\n"), ("bin/bridge.py", ""),
                           (".gitignore", ".env\ndata/\ncustom/\nagents.json\n"), (".env", "PW=secret\n"),
                           ("agents.json", json.dumps({"betty": {}}))):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        (root / "data").mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        self.official_commit("init")
        self.root = root
        for target, name, value in ((customize, "ROOT", root), (customize, "COPIES", root / "custom"),
                                    (customize, "USE_FILE", root / "data" / "custom-use.json"),
                                    (customize, "PY", sys.executable), (registry, "PATH", root / "agents.json")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def official_commit(self, msg):
        subprocess.run([*GIT, "-C", str(self.tmp / "official"), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(self.tmp / "official"), "commit", "-qm", msg], check=True)

    def change(self, text: str, test: str = PASSING) -> str:
        work = asyncio.run(customize.prepare("betty"))
        (work / "caption.py").write_text(text)
        (work / "test_x.py").write_text(test)
        return asyncio.run(customize.commit("betty", "make the caption shorter"))


class PrivateCopy(Sandbox):
    def test_the_agent_edits_a_copy_and_nothing_runs_until_chosen(self):
        sha = self.change("CAPTION = '📝'\n")
        self.assertEqual(len(sha), 40)
        self.assertEqual((self.root / "caption.py").read_text(), "CAPTION = 'Transcript'\n")   # official untouched
        self.assertEqual(asyncio.run(customize.changed_files("betty")), ["caption.py"])
        self.assertEqual(customize.code_dir("betty"), (self.root, None))       # still the official version

    def test_the_copy_follows_the_official_version(self):
        """Seen live: a copy made before a feature was committed ran without that feature."""
        self.change("CAPTION = 'mine'\n")
        (self.root / "new.py").write_text("NEW = 1\n")
        self.official_commit("official moves on")
        work = asyncio.run(customize.prepare("betty"))
        self.assertTrue((work / "new.py").exists())
        self.assertEqual((work / "caption.py").read_text(), "CAPTION = 'mine'\n")
        self.assertEqual(asyncio.run(customize.changed_files("betty")), ["caption.py"])

    def test_nothing_changed_is_no_commit(self):
        asyncio.run(customize.prepare("betty"))
        self.assertIsNone(asyncio.run(customize.commit("betty", "do nothing")))

    def test_links_attributes_and_pth_files_are_refused(self):
        """GPT review 17: git or Python could act on these by themselves."""
        work = asyncio.run(customize.prepare("betty"))
        for name in (".gitattributes", "evil.pth"):
            (work / name).write_text("x")
            with self.assertRaises(customize.CustomError):
                asyncio.run(customize.commit("betty", "x"))
            (work / name).unlink()
        (work / "secrets").symlink_to(self.root / ".env")
        with self.assertRaises(customize.CustomError):
            asyncio.run(customize.commit("betty", "x"))

    def test_a_tampered_git_file_is_not_followed(self):
        """GPT review 17: the copy's own .git file is agent-writable; our git names its metadata itself."""
        work = asyncio.run(customize.prepare("betty"))
        (work / ".git").write_text("gitdir: /nonexistent\n")
        (work / "caption.py").write_text("CAPTION = 'x'\n")
        self.assertEqual(len(asyncio.run(customize.commit("betty", "x"))), 40)

    def test_use_tests_then_runs_that_commit_from_its_own_folder(self):
        sha = self.change("CAPTION = '📝'\n")
        ok, summary = asyncio.run(customize.use("betty", sha))
        self.assertTrue(ok, summary)
        self.assertEqual(summary, "all 1 tests pass")
        live, chosen = customize.code_dir("betty")
        self.assertEqual((live, chosen), (customize.live_dir("betty", sha), sha))
        self.assertEqual((live / "caption.py").read_text(), "CAPTION = '📝'\n")
        self.assertTrue((live / ".env").is_symlink())                       # shares passwords and state
        (customize.work_dir("betty") / "caption.py").write_text("CAPTION = 'later edit'\n")
        self.assertEqual((live / "caption.py").read_text(), "CAPTION = '📝'\n")   # later edits never go live

    def test_a_version_from_before_the_launchers_moved_runs_official(self):
        """ADR 0012: a chosen version with no bin/bridge.py (made before the move) can't be started
        as bin/bridge.py; the official version runs instead of a crash loop."""
        sha = self.change("CAPTION = 'old'\n")
        asyncio.run(customize.use("betty", sha))
        (customize.live_dir("betty", sha) / "bin" / "bridge.py").unlink()
        self.assertEqual(customize.code_dir("betty"), (customize.ROOT, None))

    def test_a_new_version_gets_a_new_folder_and_old_ones_are_cleaned(self):
        first = self.change("CAPTION = 'one'\n")
        asyncio.run(customize.use("betty", first))
        second = self.change("CAPTION = 'two'\n")
        asyncio.run(customize.use("betty", second))
        third = self.change("CAPTION = 'three'\n")
        asyncio.run(customize.use("betty", third))
        self.assertEqual((customize.live_dir("betty", second) / "caption.py").read_text(), "CAPTION = 'two'\n")
        self.assertFalse(customize.live_dir("betty", first).exists())       # neither chosen nor the previous one

    def test_tests_get_no_environment(self):
        probe = PASSING.replace("self.assertTrue(True)",
                                "import os; self.assertNotIn('GH_TOKEN', os.environ); self.assertFalse(os.path.exists('.env'))")
        with mock.patch.dict("os.environ", {"GH_TOKEN": "x"}):
            ok, summary = asyncio.run(customize.use("betty", self.change("CAPTION = 'x'\n", probe)))
        self.assertTrue(ok, summary)

    def test_failing_or_missing_tests_keep_the_current_version(self):
        failing = PASSING.replace("assertTrue(True)", "assertTrue(False)")
        self.assertEqual(asyncio.run(customize.use("betty", self.change("CAPTION = 'x'\n", failing))),
                         (False, "1 tests ran and some failed"))
        self.assertEqual(asyncio.run(customize.use("betty", self.change("CAPTION = 'y'\n", "X = 1\n"))),
                         (False, "no tests ran"))                         # zero tests is not a pass
        self.assertEqual(customize.in_use(), {})

    def test_not_on_a_computer_shared_with_other_owners(self):
        """GPT review 17: a custom version runs as this computer's account, with everyone's passwords."""
        (self.root / "agents.json").write_text(json.dumps({"betty": {}, "anna": {}}))
        sha = self.change("CAPTION = 'x'\n")
        with self.assertRaises(customize.CustomError):
            asyncio.run(customize.use("betty", sha))
        with mock.patch.dict("os.environ", {"HAGENCY_CUSTOM_SHARED_OK": "1"}):    # the operator opts in
            self.assertIsNone(customize.why_not_use("betty"))

    def test_moved_code_and_package_init_files_get_the_red_flag(self):
        """ADR 0011: code now sits in packages. A package's __init__.py runs on every import and
        common/paths.py says where the repo is, so both are flagged; moved files keep their flag."""
        changed = ["adapters/agents/bridge.py", "server/__init__.py", "common/paths.py", "bridge.py",
                   "server/pm_find.py"]
        self.assertEqual(customize.sensitive(changed), changed[:4])

    def test_a_chosen_version_stops_running_when_the_opt_in_is_withdrawn(self):
        """GPT review 20: the shared-computer check ran only when choosing, not at each launch."""
        self.assertIn("mcp_custom.py", customize.SENSITIVE)     # the Hermes tools get the red flag too
        (self.root / "agents.json").write_text(json.dumps({"betty": {}, "anna": {}}))
        sha = self.change("CAPTION = 'x'\n")
        with mock.patch.dict("os.environ", {"HAGENCY_CUSTOM_SHARED_OK": "1"}):
            self.assertTrue(asyncio.run(customize.use("betty", sha))[0])
            self.assertEqual(customize.code_dir("betty")[1], sha)
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("HAGENCY_CUSTOM_SHARED_OK", None)
            self.assertEqual(customize.code_dir("betty"), (self.root, None))

    def test_go_back_only_from_the_version_meant(self):
        sha = self.change("CAPTION = 'x'\n")
        asyncio.run(customize.use("betty", sha))
        self.assertFalse(customize.go_back("betty", only_if="0" * 40))       # an old button: nothing happens
        self.assertTrue(customize.go_back("betty", only_if=sha))
        self.assertEqual(customize.code_dir("betty"), (self.root, None))
        self.assertFalse(customize.go_back("betty"))

    def test_discard_only_the_newest(self):
        sha = self.change("CAPTION = 'x'\n")
        self.assertTrue(asyncio.run(customize.discard("betty", sha)))
        self.assertEqual(asyncio.run(customize.changed_files("betty")), [])
        self.assertFalse(asyncio.run(customize.discard("betty", sha)))

    def test_safety_files_are_flagged(self):
        self.assertEqual(customize.sensitive(["caption.py", "bridge.py", "room_context.py", ".github/workflows/t.yml"]),
                         ["bridge.py", "room_context.py", ".github/workflows/t.yml"])

    def test_bad_names_and_commits_are_refused(self):
        with self.assertRaises(customize.CustomError):
            customize.work_dir("../anna")
        with self.assertRaises(customize.CustomError):
            asyncio.run(customize.use("betty", "--help"))
        with self.assertRaises(customize.CustomError):
            asyncio.run(customize.send_pr("betty", "--force", "t", "b"))


    def test_a_pr_must_be_the_owners_own_change(self):
        """GPT review 18: an official commit is on her branch too, but isn't hers to send."""
        official = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout.strip()
        self.change("CAPTION = 'x'\n")
        with self.assertRaisesRegex(customize.CustomError, "already part of the official"):
            asyncio.run(customize.send_pr("betty", official, "t", "b"))
        with self.assertRaisesRegex(customize.CustomError, "updated since"):
            asyncio.run(customize.send_pr("betty", "e" * 40, "t", "b"))


class PullRequest(unittest.TestCase):
    @mock.patch.object(customize, "_git", mock.AsyncMock(side_effect=["", customize.github.GitHubError("no")]))
    def test_forks_pushes_from_the_official_repo_and_opens_a_pr_as_the_agent(self):
        calls = []

        async def run(args, agent=None, cwd=None, program=None, extra_env=None):
            calls.append((args, agent))
            if args[:2] == ["api", "user"]:
                return "second-account\n"
            return "https://github.com/csheargm/hagency-rooms/pull/7\n" if args[0] == "pr" else ""
        with mock.patch.object(customize.github, "run", run), \
                mock.patch.object(customize.github, "git", mock.AsyncMock()) as git:
            url = asyncio.run(customize.send_pr("betty", "a" * 40, "shorter caption", "body"))
        self.assertEqual(url, "https://github.com/csheargm/hagency-rooms/pull/7")
        self.assertIn((["repo", "fork", "csheargm/hagency-rooms", "--clone=false"], "betty-agent"), calls)
        self.assertEqual(git.call_args.args[0][1], "https://github.com/second-account/hagency-rooms.git")
        self.assertEqual(git.call_args.args[2], customize.ROOT)               # pushed from the official repo


def private_room(joined=2, rule="invite", room_id="!dm"):
    return SimpleNamespace(room_id=room_id, joined_count=joined, invited_count=0, join_rule=rule)


def msg(body, sender=BETTY):
    return SimpleNamespace(body=body, sender=sender, event_id="$m", source={"content": {}})


class InChat(unittest.TestCase):
    def setUp(self):
        self.b = bridge.Bridge.__new__(bridge.Bridge)
        self.b.owner, self.b.owner_id, self.b.user_id, self.b.backend = "betty", BETTY, AGENT, "claude"
        self.b.tasks, self.b.started_ms, self.b.last_message = set(), 0, {}
        self.b.sync_point = SimpleNamespace(first_time=lambda _id: True)
        self.sent, self.reactions, self.store = [], [], {}

        async def send(room_id, body, reply_to=None, **kw):
            self.sent.append(body)
            return f"$s{len(self.sent)}"

        async def room_send(room_id, kind, content):
            self.reactions.append((content["m.relates_to"]["event_id"], content["m.relates_to"]["key"]))
        self.b.send = send
        self.b.client = SimpleNamespace(room_send=room_send)
        self.b.offers = lambda: self.store
        p = mock.patch.object(customize, "why_not_use", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_what_counts_as_a_change_request(self):
        yes = ["change your code so the transcript caption is shorter", "Please add a weather command to the bridge",
               "go back to the official version", "change your agent adapter to answer in French",
               "add a weather command to the agent adapter"]
        no = ["what does your code do?", "change the deadline to Friday", "make me a summary"]
        for text in yes:
            self.assertTrue(self.b.wants_change(private_room(), msg(text)), text)
        for text in no:
            self.assertFalse(self.b.wants_change(private_room(), msg(text)), text)
        self.assertFalse(self.b.wants_change(private_room(joined=3), msg(yes[0])))   # only the private room
        self.assertFalse(self.b.wants_change(private_room(rule="public"), msg(yes[0])))   # GPT review 17
        self.assertFalse(self.b.wants_change(private_room(), msg(yes[0], ANNA)))     # only the owner

    def ask(self, files):
        with mock.patch.object(customize, "prepare", mock.AsyncMock(return_value=Path("/w"))), \
                mock.patch.object(bridge_custom.agents, "run_turn", mock.AsyncMock(return_value="Shorter caption.")), \
                mock.patch.object(customize, "commit", mock.AsyncMock(return_value="b" * 40)), \
                mock.patch.object(customize, "changed_files", mock.AsyncMock(return_value=files)):
            asyncio.run(self.b.change_code(private_room(), msg("change your code so the caption is shorter")))

    def test_a_change_is_offered_with_three_buttons(self):
        self.ask(["pm_voice.py"])
        offer = self.sent[-1]
        self.assertIn("Your version is ready", offer)
        self.assertIn("“change your code so the caption is shorter”", offer)   # the PR title, shown before sending
        self.assertNotIn("safety checks", offer)
        self.assertEqual([k for _, k in self.reactions], ["👍", "📤", "🗑️"])
        self.assertEqual(self.store[self.reactions[0][0]]["room"], "!dm")

    def test_on_a_shared_computer_only_the_pr_is_offered(self):
        with mock.patch.object(customize, "why_not_use", return_value="other people's agents run here"):
            self.ask(["pm_voice.py"])
        self.assertEqual([k for _, k in self.reactions], ["📤", "🗑️"])
        self.assertIn("other people's agents run here", self.sent[-1])

    def test_a_change_to_the_safety_checks_is_flagged(self):
        self.ask(["bridge.py"])
        self.assertIn("This touches the safety checks", self.sent[-1])

    def click(self, key, sender=BETTY, on="$offer", room=None):
        ev = SimpleNamespace(sender=sender, reacts_to=on, key=key, event_id=f"$r{time.time_ns()}", server_timestamp=10**13)

        async def go():
            await self.b.on_custom_reaction(room or private_room(), ev)
            await asyncio.gather(*self.b.tasks)
        asyncio.run(go())

    def offer(self):
        self.store["$offer"] = {"room": "!dm", "sha": "c" * 40, "request": "r", "summary": "s", "risky": []}

    def test_only_the_owners_click_counts_and_only_once(self):
        self.offer()
        with mock.patch.object(customize, "use", mock.AsyncMock(return_value=(True, "all 3 tests pass"))) as use:
            self.click("👍", sender=ANNA)
            use.assert_not_called()
            self.click("👍")
            self.click("👍")                                                 # GPT review 17: not twice
        use.assert_awaited_once_with("betty", "c" * 40)
        self.assertIn("Switching to your version", self.sent[-1])
        self.assertEqual(self.reactions[-1][1], "↩️")
        with mock.patch.object(customize, "go_back", return_value=True) as back:
            self.click("↩️", on=self.reactions[-1][0])
        back.assert_called_once_with("betty", only_if="c" * 40)               # an old ↩️ can't undo a newer choice

    def test_a_click_elsewhere_or_after_others_joined_does_nothing(self):
        self.offer()
        with mock.patch.object(customize, "use", mock.AsyncMock()) as use:
            self.click("👍", room=private_room(room_id="!other"))
            self.click("👍", room=private_room(joined=3))
        use.assert_not_called()

    def test_failing_tests_say_so_and_the_button_still_works(self):
        self.offer()
        with mock.patch.object(customize, "use", mock.AsyncMock(return_value=(False, "no tests ran"))):
            self.click("👍")
        self.assertIn("Not switching: no tests ran", self.sent[-1])
        self.assertNotIn("done", self.store["$offer"])

    def test_send_as_pull_request(self):
        self.store["$offer"] = {"room": "!dm", "sha": "c" * 40, "request": "shorter caption", "summary": "s",
                                "risky": ["bridge.py"]}
        with mock.patch.object(customize, "send_pr", mock.AsyncMock(return_value="https://github.com/o/r/pull/1")) as pr:
            self.click("📤")
        self.assertIn("bridge.py", pr.call_args.args[3])                      # reviewers are told too
        self.assertIn("https://github.com/o/r/pull/1", self.sent[-1])


class Supervisor(unittest.TestCase):
    def test_a_bridge_runs_from_its_owners_chosen_version(self):
        kid = supervise.Child("betty-agent", ["x"], owner="betty")
        with mock.patch.object(customize, "code_dir", return_value=(Path("/live"), "d" * 40)), \
                mock.patch("asyncio.create_subprocess_exec", mock.AsyncMock()) as spawn, \
                mock.patch.object(supervise.Child, "pump", mock.MagicMock()), \
                mock.patch("asyncio.create_task"):
            asyncio.run(kid.start())
        self.assertEqual((spawn.call_args.kwargs["cwd"], kid.launched), (Path("/live"), "d" * 40))
        self.assertEqual(spawn.call_args.kwargs["env"]["HAGENCY_HOME"], str(supervise.ROOT))

    def test_a_crashing_own_version_goes_back_to_official(self):
        kid = supervise.Child("betty-agent", ["x"], owner="betty")
        kid.launched, kid.crashes, kid.started = "d" * 40, 1, time.time()   # crashed right after starting
        kid.proc, kid.start = SimpleNamespace(returncode=1), mock.AsyncMock()
        kid.claim()
        with mock.patch.object(supervise, "BACKOFF_S", (0,)), \
                mock.patch.object(customize, "go_back", return_value=True) as back:
            asyncio.run(supervise.revive(kid))
        back.assert_called_once_with("betty", only_if="d" * 40)              # never a newer choice


if __name__ == "__main__":
    unittest.main()
