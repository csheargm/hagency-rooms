"""Unit tests: one room, one repo (pm_settings.py, room_state.py, loop_progress.migrate).
Run: .venv/bin/python -m unittest tests.test_room_repo"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomPutStateResponse

from server import console
from server import github_setup
from common import intents
from server import pm_brain
from common import room_state
from common import solo_review
from server.loop import Loop
from server.loop_progress import migrate
from server.pm_settings import NO_REPO
from server import repo_refusals

PM, ANNA, BETTY = "@pm:bridge.localhost", "@anna:bridge.localhost", "@betty:bridge.localhost"
HELLO = "your-name/hagency-hello"
_LISTED = frozenset({HELLO, "octocat/other", "a/x", "b/y"})   # an operator list set (HAGENCY_REPOS)
_allowed = (mock.patch("common.github.ALLOWED_REPOS", _LISTED), mock.patch("common.github.OPERATOR_REPOS", _LISTED))


def setUpModule():
    for p in _allowed:
        p.start()


def tearDownModule():
    for p in _allowed:
        p.stop()


class Parsing(unittest.TestCase):
    def test_the_repo_is_read_from_plain_words_or_a_url(self):
        for text, repo in (("this room works on your-name/hagency-hello", HELLO),
                           ("use https://github.com/octocat/Hello-World", "octocat/Hello-World"),
                           ("set the repo to a/b.git", "a/b"),
                           ("set the repo to a/foo.bar", "a/foo.bar"),                  # GPT review 11: was a/foo
                           ("use https://github.com/a/foo.bar.git", "a/foo.bar"),
                           ("the repo is a/b.", "a/b")):
            self.assertEqual(intents.classify(text), intents.SET_REPO, text)
            self.assertEqual(intents.repo_named(text), repo, text)   # (allowed or not is set_repo's call)
        for text in ("let's build it", "let's use docs/guide.md for that", "we use a/b", "use https://evil.com/a/b",
                     "please do not use https://github.com/a/b"):                # GPT review 11: negation
            self.assertNotEqual(intents.classify(text), intents.SET_REPO, text)
        # GPT review 10: the repo is the command's own argument, not the first owner/name in the message
        self.assertEqual(intents.repo_named("the room works on a/b, see x/y"), "a/b")
        self.assertIsNone(intents.repo_named("see x/y"))

    def test_stored_as_a_list_older_single_field_still_read(self):
        self.assertEqual(room_state.repo_in({"level": "off", "repos": [HELLO]}), HELLO)
        self.assertEqual(room_state.repo_in({"repos": ["YOUR-NAME/hagency-hello"]}), HELLO)
        self.assertEqual(room_state.repo_in({"level": "off", "repo": HELLO}), HELLO)
        for bad in ({}, {"repos": []}, {"repo": "../x"}, {"repos": ["no-slash"]}, "x"):
            self.assertIsNone(room_state.repo_in(bad), bad)
        self.assertEqual(room_state.content("assist", HELLO), {"level": "assist", "repos": [HELLO], "repo": HELLO})

    def test_a_room_without_a_repo_is_off(self):
        self.assertEqual(room_state.level_of({"level": "autonomous"}), "off")
        self.assertEqual(room_state.level_of({"level": "autonomous", "repos": [HELLO]}), "autonomous")

    def test_old_numeric_keys_move_under_their_repo(self):
        old = {"7": {"room": "!r", "stage": "done"}, "x/y#3": {"room": "!s", "repo": "x/y", "stage": "done"}}
        with mock.patch("common.github.REPO", HELLO):
            new = migrate(old)
        self.assertEqual(set(new), {f"{HELLO}#7", "x/y#3"})
        self.assertEqual(new[f"{HELLO}#7"]["repo"], HELLO)


class SetRepo(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(pm_brain, "BRAIN", "off")
        p.start()
        self.addCleanup(p.stop)
        self.repo = None                      # what the room's state says now
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.pending, self.lp.boards, self.lp.state = PM, {}, {}, {}
        self.lp.generation, self.lp.busy, self.lp.room_repos = {}, set(), {}
        self.lp.plans, self.lp.last_plans, self.lp.lock, self.lp.install_watches = {}, {}, asyncio.Lock(), {}
        self.lp.kick = lambda: None
        self.can_write = True                  # the requester's agent's GitHub access to the repo
        q = mock.patch("server.pm_settings.may_write", lambda user, repo: asyncio.sleep(0, self.can_write))
        q.start()
        self.addCleanup(q.stop)
        self.lp.level = mock.AsyncMock(return_value="off")
        self.said, self.written = [], []

        async def say(room_id, body, *a, **k):
            self.said.append(body)

        async def put_state(room_id, kind, content):
            self.written.append(content)
            self.repo = room_state.repo_in(content)
            return RoomPutStateResponse("$s", room_id)
        self.lp.say = say
        self.lp.client = SimpleNamespace(room_put_state=put_state, rooms={})
        v = mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=False))   # pm_projects.py
        v.start()
        self.addCleanup(v.stop)
        for name, fn in (("is_moderator", lambda c, r, u: asyncio.sleep(0, u == ANNA)),
                         ("joined_moderator", lambda c, rid, u: asyncio.sleep(0, u == ANNA)),   # at the write
                         ("repo_of", lambda c, rid: asyncio.sleep(0, self.repo)),
                         ("binding_of", lambda c, rid: asyncio.sleep(0, (self.repo, None)))):
            q = mock.patch.object(room_state, name, fn)
            q.start()
            self.addCleanup(q.stop)
        self.room = SimpleNamespace(room_id="!r", users={ANNA: None, BETTY: None}, invited_users={})

    def tell(self, sender, body):
        ev = SimpleNamespace(sender=sender, body=f"@pm {body}",
                             source={"content": {"body": f"@pm {body}", "m.mentions": {"user_ids": [PM]}}})

        async def go():
            await self.lp.on_human(self.room, ev)
            await asyncio.gather(*self.lp.busy)
        asyncio.run(go())

    def test_a_repo_the_server_doesnt_allow_is_refused_without_listing_others(self):
        self.tell(ANNA, "this room works on someone/private")
        self.assertEqual(self.written, [])
        self.assertEqual(self.said[-1], repo_refusals.not_listed("someone/private"))
        self.assertNotIn(HELLO, self.said[-1])

    def test_an_allowed_repo_still_needs_the_requesters_own_access(self):
        """GPT review 10: anyone can create (and own) a room, so the allowlist alone exposed every
        allowed repo. (Without the App, @pm checks their own agent's GitHub account.)"""
        self.can_write = False
        self.tell(ANNA, "this room works on octocat/other")
        self.assertEqual(self.written, [])
        self.assertEqual(self.said[-1], "Your agent's GitHub account can't write to octocat/other, so this room can't use it.")

    def test_a_plan_from_the_old_repo_is_never_filed_in_the_new_one(self):
        self.repo = HELLO
        pending = {"kind": "plan", "items": [{"title": "x"}], "agent": "@anna-agent:x", "by": ANNA, "repo": "octocat/other"}
        asyncio.run(self.lp.file_plan(self.room, pending, ANNA))
        self.assertIn("repo changed", self.said[-1])

    def test_the_owner_sets_it_and_the_room_starts_stopped(self):
        self.tell(ANNA, "this room works on your-name/hagency-hello")
        self.assertEqual(self.written, [room_state.content("off", HELLO)])
        self.assertEqual(self.lp.room_repos, {"!r": HELLO})
        self.assertIn("now works on **your-name/hagency-hello**", self.said[-1])
        self.lp.last_plans["!r"] = {"items": [{"title": "for hello"}]}
        self.tell(ANNA, "this room works on octocat/other")
        self.assertNotIn("!r", self.lp.last_plans)              # a new repo starts without the old plan

    def test_a_contributor_cant(self):
        self.tell(BETTY, "this room works on evil/repo")
        self.assertEqual(self.written, [])
        self.assertIn("Only the room owner", self.said[-1])

    def test_never_swapped_under_running_work(self):
        self.repo = HELLO
        self.lp.state = {f"{HELLO}#7": {"room": "!r", "repo": HELLO, "stage": "building"}}
        self.lp.last_plans = {"!r": {"items": []}}
        self.tell(ANNA, "this room works on octocat/other")
        self.assertEqual(self.written, [])
        self.assertIn("still running", self.said[-1])
        self.assertIn("!r", self.lp.last_plans)                 # refused: nothing cleared either

    def test_no_repo_means_no_mode_and_no_plan(self):
        self.assertEqual(asyncio.run(self.lp.write_level("!r", "autonomous")), NO_REPO)
        self.assertEqual(self.written, [])

    def test_without_power_pm_says_how_to_fix_it(self):
        """Seen live: in a room someone made, @pm joined at power 0 and showed a raw M_FORBIDDEN."""
        from nio import RoomPutStateError
        from server.pm_settings import NEED_POWER

        async def refused(room_id, kind, content):
            return RoomPutStateError("You don't have permission", "M_FORBIDDEN")
        self.lp.client.room_put_state = refused
        self.tell(ANNA, "this room works on your-name/hagency-hello")
        self.assertEqual(self.said[-1], NEED_POWER)
        self.assertEqual(self.lp.room_repos, {})

    def test_a_result_finds_its_own_rooms_item(self):
        """Two rooms' repos can both have an issue #7: the task id, not the number, decides."""
        self.lp.state = {"a/x#7": {"room": "!a", "repo": "a/x", "stage": "building", "task_ids": ["T1"]},
                         "b/y#7": {"room": "!b", "repo": "b/y", "stage": "building", "task_ids": ["T2"]}}
        self.assertEqual(self.lp.key_for_result("!b", {"issue": 7, "task_id": "T2"}), "b/y#7")
        self.assertIsNone(self.lp.key_for_result("!a", {"issue": 7, "task_id": "T2"}))


class Polling(unittest.TestCase):
    def test_one_unreadable_repo_never_stops_the_other_rooms(self):
        """Seen live: rooms left by the e2e tests on a fake repo made every tick fail, for every room."""
        from common import github
        lp = Loop.__new__(Loop)
        lp.room_repos, lp.state, lp.open_issues = {}, {}, {}
        lp.client = SimpleNamespace(rooms={"!a": None, "!b": None})
        lp.level = mock.AsyncMock(return_value="off")

        async def run_json(args, agent=None):
            if "a/x" in args:
                raise github.GitHubError("Could not resolve to a Repository")
            return [{"number": 3, "title": "t"}]
        with mock.patch.object(room_state, "repo_of", lambda c, rid: asyncio.sleep(0, {"!a": "a/x", "!b": "b/y"}[rid])), \
                mock.patch.object(github, "run_json", run_json), self.assertLogs("loop", "WARNING"):
            asyncio.run(lp.tick())
        self.assertEqual(lp.open_issues, {"!b": [{"number": 3, "title": "t"}]})


class OldRepoWork(unittest.TestCase):
    """GPT review 10: after a switch, an old repo's items must never be advanced or answered."""
    def test_a_result_for_the_old_repos_item_is_ignored(self):
        lp = Loop.__new__(Loop)
        lp.room_repos = {"!r": "b/y"}
        lp.state = {"a/x#7": {"room": "!r", "repo": "a/x", "stage": "building", "builder": "@b:x", "task_ids": ["T"]}}
        room = SimpleNamespace(room_id="!r")
        asyncio.run(lp.on_result(room, "@b:x", {"kind": "build", "issue": 7, "task_id": "T", "pr": 1}))
        self.assertEqual(lp.state["a/x#7"]["stage"], "building")

    def test_a_task_in_a_room_without_a_repo_is_refused(self):
        """None == None must not pass: the MCP claim needs a bound repo."""
        import os
        os.environ.setdefault("HAGENCY_AGENT", "anna-hermes-agent")
        from mcp_tools import mcp_core as core
        from adapters.agents import room_context
        t = {"task_id": "T", "kind": "build", "issue": 7, "_room": "!r"}            # no repo in the task either
        with mock.patch.object(core, "task", lambda task_id, kind: t), \
                mock.patch.object(core, "owner_in", lambda room: True), \
                mock.patch.object(core, "token", lambda: "t"), \
                mock.patch.object(room_context, "room_repo", lambda token, room: None):
            problem, task = core.claim("T", "build")
        self.assertIsNone(task)
        self.assertIn("wrong repo", problem["next"])


class RemoteUsers(unittest.TestCase):
    def test_a_same_named_user_from_another_server_has_no_access(self):
        """GPT review 11: @anna:elsewhere was checked against our anna's agent."""
        from server import pm_settings
        with mock.patch.object(pm_settings.github_setup, "identity", mock.AsyncMock(return_value={"push": True})), \
                mock.patch.object(pm_settings.registry, "owner_of", lambda agent: f"@anna:{pm_settings.registry.SERVER_NAME}"):
            self.assertFalse(asyncio.run(pm_settings.may_write("@anna:elsewhere.org", HELLO)))
            self.assertTrue(asyncio.run(pm_settings.may_write(f"@anna:{pm_settings.registry.SERVER_NAME}", HELLO)))


class ConsoleRepo(unittest.TestCase):
    """The console's Project repo section goes through the same set_repo; its check is read-only."""
    def setUp(self):
        self.room = SimpleNamespace(room_id="!r", users={ANNA: 0, BETTY: 0}, invited_users={})
        self.calls = []

        async def set_repo(room, user, value):
            self.calls.append((user, value))
            return "This room now works on **octocat/hello**."
        self.loop = SimpleNamespace(client=SimpleNamespace(rooms={"!r": self.room}), set_repo=set_repo,
                                    is_person=lambda r, u: u in r.users, agents_in=lambda r: ["@anna-agent:x"])
        for name, fn in (("is_moderator", lambda c, r, u: asyncio.sleep(0, u == ANNA)),
                         ("repo_of", lambda c, rid: asyncio.sleep(0, HELLO)),
                         ("discussion_of", lambda c, rid: asyncio.sleep(0, False))):
            q = mock.patch.object(room_state, name, fn)
            q.start()
            self.addCleanup(q.stop)
        self.solo = False
        q = mock.patch.object(solo_review, "read", lambda c, rid: asyncio.sleep(0, {
            **solo_review.content(self.solo, repo=HELLO, ok=True), "bound": HELLO}))
        q.start()
        self.addCleanup(q.stop)

    def act(self, user, action, value=""):
        return asyncio.run(console.act(self.loop, "!r", user, action, value))

    def test_setting_the_repo_uses_the_chat_path(self):
        self.assertIn("now works on", self.act(ANNA, "repo", " octocat/hello "))   # the console shows @pm's answer
        self.assertEqual(self.calls, [(ANNA, "octocat/hello")])      # set_repo itself checks the moderator

    def test_check_reports_problems_and_who_fixes_them(self):
        found = mock.AsyncMock(return_value=({}, ["no 'agent-ok' label on " + HELLO], []))
        self.solo = True
        with mock.patch.object(github_setup, "readiness", found):
            text = self.act(ANNA, "check")
        found.assert_awaited_once_with(HELLO, ["anna-agent"], detail=False, solo=True, bypass=True)   # no raw CLI errors here
        self.assertIn("✗ no 'agent-ok' label", text)
        self.assertNotIn("github_setup.py", text)            # GUI only: each problem says its own fix
        with mock.patch.object(github_setup, "readiness", mock.AsyncMock(return_value=({}, [], ["the label"]))):
            self.assertEqual(self.act(ANNA, "check"), f"✓ {HELLO} is ready. Checked:\n✓ the label")

    def test_only_moderators_run_the_check(self):
        with mock.patch.object(github_setup, "readiness", mock.AsyncMock()) as found:
            self.assertIn("only the room owner", self.act(BETTY, "check"))
        found.assert_not_awaited()


class Allowlist(unittest.TestCase):
    """GPT-style check: a room anyone can create must not become a window into any repo the host's gh sees."""
    def test_only_the_operators_repos(self):
        from common import github
        with mock.patch.object(github, "ALLOWED_REPOS", frozenset({HELLO})), \
                mock.patch.object(github, "OPERATOR_REPOS", frozenset({HELLO})):
            self.assertEqual(github.allowed_repo("Your-Name/Hagency-Hello"), HELLO)    # GitHub ignores case
            self.assertIsNone(github.allowed_repo("your-name/private-thing"))
            self.assertIsNone(room_state.repo_in({"level": "autonomous", "repos": ["your-name/private-thing"]}))


if __name__ == "__main__":
    unittest.main()
