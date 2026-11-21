"""@pm's reads and a room's repo binding, through the GitHub App (fake GitHub API: tests/fake_github_api.py).
Run: .venv/bin/python -m unittest tests.test_github_app_flows"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import RoomPutStateResponse

from common import github
from common import github_app
from server import pm_brain
from server import pm_settings
from server import repo_refusals
from common import room_state
from server.loop import Loop
from tests.test_github_app import OTHER, REPO, AppCase, issue

PM, ANNA = "@pm:bridge.localhost", "@anna:bridge.localhost"


class Reads(AppCase):
    """The same JSON gh gives, for each read @pm makes (loop, loop_progress, pm_plan, github_setup)."""

    def gh(self, *args):
        return asyncio.run(github.run_json(list(args)))

    def test_issue_list_is_issues_only_with_the_label(self):
        self.fake.issues[REPO] = [issue(1, "one"), issue(2, "a PR", pr=True), issue(3, "other label", labels=("x",)),
                                  issue(4, "closed", state="closed")]
        out = self.gh("issue", "list", "-R", REPO, "--label", github.LABEL, "--state", "open",
                      "--json", "number,title", "--limit", "50")
        self.assertEqual(out, [{"number": 1, "title": "one"}])

    def test_issue_list_pages_up_to_the_limit(self):
        self.fake.issues[REPO] = [issue(n, f"t{n}") for n in range(1, 251)]
        out = self.gh("issue", "list", "-R", REPO, "--json", "title", "--limit", "120")
        self.assertEqual(len(out), 120)
        self.assertEqual(out[-1], {"title": "t120"})

    def test_prs_on_the_first_page_dont_use_up_the_limit(self):
        """GPT review, GitHub App: the limit counted PRs (REST lists them as issues) before they were dropped."""
        self.fake.issues[REPO] = [issue(n, f"pr{n}", pr=True) for n in range(1, 101)] + [issue(101, "real")]
        out = self.gh("issue", "list", "-R", REPO, "--json", "number", "--limit", "50")
        self.assertEqual(out, [{"number": 101}])

    def test_a_pr_without_a_review_decision_reads_as_gh_prints_it(self):
        self.fake.prs[(REPO, 3)] = {"state": "OPEN", "reviewDecision": None}
        self.assertEqual(self.gh("pr", "view", "3", "-R", REPO, "--json", "state,reviewDecision"),
                         {"state": "OPEN", "reviewDecision": ""})

    def test_issue_view_state_is_upper_case_like_gh(self):
        self.fake.issues[REPO] = [issue(5, "done", state="closed")]
        self.assertEqual(self.gh("issue", "view", "5", "-R", REPO, "--json", "state"), {"state": "CLOSED"})

    def test_merge_ready_reads_githubs_verdict(self):
        self.fake.prs[(REPO, 9)] = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
                                    "reviewDecision": "APPROVED", "headRefOid": "abc", "headRefName": "agent/x"}
        ok, info = asyncio.run(github.merge_ready(9, repo=REPO))
        self.assertTrue(ok)
        self.assertEqual(info["headRefOid"], "abc")
        self.assertNotIn("headRefName", info)                       # only the fields asked for
        self.assertEqual(self.gh("pr", "view", "9", "-R", REPO, "--json", "headRefName,state"),
                         {"headRefName": "agent/x", "state": "OPEN"})

    def test_labels_and_repo_paths(self):
        self.fake.labels[REPO] = [{"name": f"l{n}", "color": "fff"} for n in range(150)]
        self.assertEqual(len(self.gh("label", "list", "-R", REPO, "--json", "name", "--limit", "200")), 150)
        self.assertEqual(self.gh("api", f"repos/{REPO}")["default_branch"], "main")
        self.assertEqual(self.gh("api", f"repos/{REPO}/rules/branches/main")[0]["type"], "pull_request")
        with self.assertRaises(github.GitHubError):
            self.gh("api", f"repos/{REPO}/pages")                  # 404: no Pages site (where_to_try falls back)

    def test_writes_and_unknown_reads_are_refused_before_any_request(self):
        for args in (["issue", "create", "-R", REPO, "--title", "x"], ["pr", "merge", "1", "-R", REPO],
                     ["api", "-X", "POST", f"repos/{REPO}/rulesets"], ["api", "user"],
                     ["api", f"repos/{REPO}/../../orgs/x"], ["api", f"repos/{REPO}/%2e%2e/%2E%2E/orgs/x"],
                     ["api", f"repos/{REPO}//x"], ["api", f"repos/{REPO}/x%2F..%2F..%2Forgs"],["issue", "list", "--json", "number"],
                     ["pr", "view", "1", "-R", REPO, "--json", "body"]):
            with self.assertRaises(github.GitHubError, msg=args):
                self.gh(*args)
        self.assertEqual(self.fake.requests, [])

    def test_each_repo_gets_its_own_token(self):
        self.fake.installations[OTHER.lower()] = 8
        self.gh("api", f"repos/{REPO}")
        self.gh("api", f"repos/{OTHER}")
        self.assertEqual(len(self.minted()), 2)
        self.assertEqual(sorted(map(sorted, self.fake.tokens.values())), [[REPO.lower()], [OTHER.lower()]])


class BindingSetup(AppCase):
    """A room Anna moderates, its Matrix state faked; she is connected and may write (test_github_link
    builds on this with the real checks)."""

    def setUp(self):
        super().setUp()
        for target, name, value in ((pm_brain, "BRAIN", "off"), (pm_settings, "INSTALL_RECHECK_S", 0.01)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in (("server.pm_settings.may_write", lambda user, repo: asyncio.sleep(0, True)),
                              ("server.github_link.link_of", lambda user: {"login": "anna", "id": 1})):   # connected
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.repo, self.said, self.written = None, [], []
        lp = self.lp = Loop.__new__(Loop)
        lp.user_id, lp.pending, lp.boards, lp.state, lp.generation, lp.busy = PM, {}, {}, {}, {}, set()
        lp.room_repos, lp.plans, lp.last_plans, lp.lock, lp.install_watches = {}, {}, {}, asyncio.Lock(), {}
        lp.kick, lp.level = (lambda: None), mock.AsyncMock(return_value="off")

        async def say(room_id, body, *a, **k):
            self.said.append(body)

        async def put_state(room_id, kind, content):
            self.written.append(content)
            self.repo = room_state.repo_in(content)
            return RoomPutStateResponse("$s", room_id)
        lp.say, lp.client = say, SimpleNamespace(room_put_state=put_state, rooms={})
        v = mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=False))   # pm_projects.py
        v.start()
        self.addCleanup(v.stop)
        for name, fn in (("is_moderator", lambda c, r, u: asyncio.sleep(0, u == ANNA)),
                         ("joined_moderator", lambda c, rid, u: asyncio.sleep(0, u == ANNA)),   # at the write
                         ("repo_of", lambda c, rid: asyncio.sleep(0, self.repo)),
                         ("binding_of", lambda c, rid: asyncio.sleep(0, (self.repo, None)))):
            p = mock.patch.object(room_state, name, fn)
            p.start()
            self.addCleanup(p.stop)
        self.room = SimpleNamespace(room_id="!r", users={ANNA: None}, invited_users={}, display_name="r")


class Binding(BindingSetup):
    """A moderator binds a room to a repo the App isn't on yet: the install link, then @pm finishes."""

    def test_install_link_then_the_room_is_set_up_once_installed(self):
        async def flow():
            answer = await self.lp.set_repo(self.room, ANNA, OTHER)
            self.assertIn("https://github.com/apps/hagency-rooms/installations/new", answer)
            self.assertEqual(self.written, [])                     # nothing bound yet
            _, task = self.lp.install_watches["!r"]
            await asyncio.sleep(0.05)                               # it looks again, still not there
            self.assertFalse(task.done())
            self.fake.installations[OTHER.lower()] = 8              # the owner clicks Install
            await asyncio.wait_for(task, 5)
        asyncio.run(flow())
        self.assertEqual(self.written, [room_state.content("off", OTHER)])
        self.assertIn("now works on **octo/other**", self.said[-1])
        self.assertEqual(self.lp.install_watches, {})

    def test_after_ten_minutes_it_stops_looking_quietly(self):
        """GPT review, batch 12–14: a closing "still not installed" line (and silence for a private repo
        that has the App) would tell the two apart; the install step was said once, at the start."""
        async def flow():
            await self.lp.set_repo(self.room, ANNA, OTHER)
            await asyncio.wait_for(self.lp.install_watches["!r"][1], 5)
        asyncio.run(flow())
        self.assertEqual(self.written, [])
        self.assertEqual(len(self.said), 1)
        self.assertIn("Install the Hagency Rooms app on octo/other", self.said[-1])

    def test_a_newer_choice_ends_the_older_wait(self):
        async def flow():
            await self.lp.set_repo(self.room, ANNA, OTHER)
            _, waiting = self.lp.install_watches["!r"]
            await self.lp.set_repo(self.room, ANNA, REPO)           # installed: bound now
            await asyncio.sleep(0.05)
            self.assertTrue(waiting.cancelled())
            self.fake.installations[OTHER.lower()] = 8
            await asyncio.sleep(0.05)
        asyncio.run(flow())
        self.assertEqual(self.written, [room_state.content("off", REPO)])   # never swapped back to OTHER

    def test_a_newer_choice_cancels_the_wait_even_during_its_last_bind(self):
        """GPT review, GitHub App: the wait unregistered itself before its final set_repo, so a slow write
        check there let it overwrite a newer binding."""
        gate = asyncio.Event()
        slow = {"on": False}

        async def may_write(user, repo):
            if slow["on"] and repo == OTHER:
                await gate.wait()                                   # the wait's own check, stalled
            return True

        async def flow():
            with mock.patch("server.pm_settings.may_write", may_write):
                await self.lp.set_repo(self.room, ANNA, OTHER)
                _, waiting = self.lp.install_watches["!r"]
                slow["on"] = True
                self.fake.installations[OTHER.lower()] = 8          # installed: the wait starts its bind
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if self.lp.install_watches.get("!r", (None, None))[1] is waiting and len(self.fake.requests) > 3:
                        break
                await self.lp.set_repo(self.room, ANNA, REPO)       # a newer choice meanwhile
                gate.set()
                await asyncio.sleep(0.05)
                self.assertTrue(waiting.cancelled())
        asyncio.run(flow())
        self.assertEqual(self.written, [room_state.content("off", REPO)])

    def test_a_newer_install_wait_cancels_an_older_one_queued_for_the_lock(self):
        """GPT review, GitHub App (2nd pass): past its checks, the older wait unregistered itself
        while it queued for the lock, so a newer wait for another uninstalled repo couldn't stop it."""
        async def flow():
            await self.lp.set_repo(self.room, ANNA, OTHER)
            _, older = self.lp.install_watches["!r"]
            async with self.lp.lock:                                 # a poll holds the lock
                self.fake.installations[OTHER.lower()] = 8
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if any(r[1].endswith("/access_tokens") for r in self.fake.requests[-3:]):
                        break                                        # the older wait is past its checks
                await asyncio.sleep(0.05)
                del self.fake.installations[REPO.lower()]
                answer = await self.lp.set_repo(self.room, ANNA, REPO)   # newer choice, App not there either
                self.assertIn("installations/new", answer)
            await asyncio.sleep(0.05)
            self.assertTrue(older.cancelled())
            self.lp.stop_install_watch("!r")
        asyncio.run(flow())
        self.assertEqual(self.written, [])

    def test_a_wait_cancelled_mid_write_finishes_that_bind_whole(self):
        """GPT review, GitHub App (3rd pass): cancelled while Matrix stored the binding, @pm's own
        records (room_repos, plans) were skipped. The bind now completes, still under the lock."""
        gate, put = asyncio.Event(), self.lp.client.room_put_state

        async def slow_put(room_id, kind, content):
            await gate.wait()
            return await put(room_id, kind, content)

        async def flow():
            self.lp.client.room_put_state = slow_put
            await self.lp.set_repo(self.room, ANNA, OTHER)
            _, waiting = self.lp.install_watches["!r"]
            self.fake.installations[OTHER.lower()] = 8
            for _ in range(200):
                await asyncio.sleep(0.01)
                if self.lp.lock.locked():
                    break                                           # the wait is writing the binding
            self.assertTrue(self.lp.lock.locked())
            waiting.cancel()
            await asyncio.sleep(0.02)
            self.assertTrue(self.lp.lock.locked())                  # not released while the write runs
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertFalse(self.lp.lock.locked())
        asyncio.run(flow())
        self.assertEqual(self.written, [room_state.content("off", OTHER)])
        self.assertEqual(self.lp.room_repos, {"!r": OTHER})          # @pm's records match Matrix

    def test_only_moderators_and_allowed_repos_reach_the_install_step(self):
        """With an operator list set (HAGENCY_REPOS): an unlisted repo is refused before GitHub is asked."""
        with mock.patch.object(github, "OPERATOR_REPOS", frozenset({OTHER})), \
                mock.patch.object(github, "ALLOWED_REPOS", frozenset({OTHER})):
            asyncio.run(self.lp.set_repo(self.room, "@mallory:bridge.localhost", OTHER))
            asyncio.run(self.lp.set_repo(self.room, ANNA, "someone/private"))
        self.assertEqual(self.lp.install_watches, {})
        self.assertEqual(self.said[-1], repo_refusals.not_listed("someone/private"))
        self.assertFalse(any("installation" in r[1] for r in self.fake.requests))

    def test_unconfigured_binds_as_before(self):
        with mock.patch.object(github_app, "APP_ID", ""):
            asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER))
        self.assertEqual(self.written, [room_state.content("off", OTHER)])
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
