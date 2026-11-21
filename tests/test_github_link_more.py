"""Connect GitHub, continued (ADR 0007): the GitHub card in node, and the regressions from its GPT
review (REVIEWS.md). GitHub is the local fake (tests/fake_github_api.py).
Run: .venv/bin/python -m unittest tests.test_github_link_more"""
import asyncio
import json
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from common import github_app
from server import github_link
from server import panel
from server import pm_github
from server import pm_settings
from server import repo_refusals
from common import room_state
from tests.test_github_app import OTHER, REPO
from tests.test_github_app_flows import PM
from tests.test_github_link import ANNA, ANNA_ID, BindCase, LinkCase, message

REAL_JOINED_MODERATOR = room_state.joined_moderator      # BindingSetup fakes it; these tests read "standing"


@unittest.skipUnless(shutil.which("node"), "node isn't installed: the card's script is not run")
class PanelCard(unittest.TestCase):
    """The GitHub card (static/panel_github.js) for each state, run in node; every value escaped."""

    def card(self, github_state):
        esc = """const esc = t => String(t).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));"""
        script = f"{esc}\n{panel.SCRIPTS["panel-github.js"].read_text()}\nprocess.stdout.write(githubCard({json.dumps({'github': github_state})}));"
        done = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return done.stdout

    def test_each_state(self):
        self.assertEqual(self.card(None), "")                                      # off on this server
        self.assertIn('data-a="github" data-v="connect"', self.card({"login": None, "code": None, "url": None}))
        waiting = self.card({"login": None, "code": "ABCD-1000", "url": "https://github.com/login/device"})
        self.assertIn("ABCD-1000", waiting)
        self.assertNotIn('data-v="connect"', waiting)
        done = self.card({"login": "<anna>", "code": None, "url": None})
        self.assertIn("GitHub: <b>@&lt;anna&gt;</b> ✓", done)
        self.assertIn('data-v="disconnect">Disconnect GitHub', done)


class ReviewFixes(LinkCase):
    """GPT review, Connect GitHub: each point adopted, as a test."""

    def test_a_chat_the_person_opened_never_gets_the_code(self):
        """The code goes only to my own private chat (only I invite; history from the invite)."""
        theirs = SimpleNamespace(room_id="!theirs", users={ANNA: 0, PM: 0}, invited_users={})
        self.lp._direct_chats = {"!theirs": ANNA}                                  # a direct chat Anna opened
        self.fake.approve("anna-gh", ANNA_ID)
        self.run_(self.lp.on_github_text(theirs, message(ANNA, "connect my github")))
        self.assertEqual(self.in_room("!theirs"), ["I sent you the GitHub code in our private chat."])
        self.assertIn("ABCD-1000", self.in_room("!dm")[0])

    def test_disconnect_while_github_answers_gives_no_code(self):
        started = asyncio.Event()
        real = github_link.start

        async def slow_start():
            started.set()
            await asyncio.sleep(0.05)
            return await real()

        async def flow():
            with mock.patch.object(github_link, "start", slow_start):
                click = asyncio.create_task(self.lp.connect_github(ANNA))
                await started.wait()
                self.assertEqual(self.lp.disconnect_github(ANNA), pm_github.CANCELLED)
                self.assertEqual(await click, pm_github.CANCELLED)
        asyncio.run(flow())
        self.assertEqual(github_link.FLOWS, {})
        self.assertEqual(self.said, [])                                            # no code went anywhere

    def test_a_cancelled_click_never_takes_over_a_newer_one(self):
        """GPT re-check: the pending marker was per person, so click A (cancelled) consumed click B's."""
        real = github_link.start
        self.fake.approve("first", 1, answers=["authorization_pending"] * 50)
        self.fake.approve("second", 2, answers=["authorization_pending"] * 50)

        async def flow():
            first_gate, second_gate = asyncio.Event(), asyncio.Event()
            order = [first_gate, second_gate]

            async def start():
                await order.pop(0).wait()
                return await real()
            with mock.patch.object(github_link, "start", start):
                a = asyncio.create_task(self.lp.connect_github(ANNA))
                await asyncio.sleep(0)
                self.lp.disconnect_github(ANNA)
                b = asyncio.create_task(self.lp.connect_github(ANNA))
                await asyncio.sleep(0)
                first_gate.set()
                self.assertEqual(await a, pm_github.CANCELLED)                    # A's code is never shown
                second_gate.set()
                self.assertIn("ABCD-1001", await b)                                # B's sign-in goes on
                github_link.FLOWS[ANNA]["task"].cancel()
        asyncio.run(flow())
        self.assertFalse(any("ABCD-1000" in body for _, body in self.said))

    def test_no_code_is_sent_after_a_disconnect_while_opening_the_chat(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 50)

        async def opening(person):
            self.lp.disconnect_github(person)                                       # meanwhile
            return "!dm"
        self.lp.private_room = opening
        self.run_(self.lp.connect_github(ANNA))
        self.assertEqual(self.said, [])

    def test_an_error_that_isnt_text_is_unexpected(self):
        self.assertEqual(github_link._error({"error": {"x": "ghu_y"}}), "unexpected")
        self.assertEqual(github_link._error({"error": ["slow_down"]}), "unexpected")

    def test_repeat_clicks_dont_resend_the_code(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 3)

        async def flow():
            await self.lp.github_action(ANNA, "connect")
            await self.lp.github_action(ANNA, "connect")
            await github_link.FLOWS[ANNA]["task"]
        asyncio.run(flow())
        self.assertEqual(sum("ABCD-1000" in body for _, body in self.said), 1)

    def test_githubs_own_error_text_never_reaches_anyone(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["ghu_LeakedTokenShape123"])
        self.run_(self.lp.connect_github(ANNA))
        self.assertIn("(unexpected)", self.in_room("!dm")[-1])
        self.assertFalse(any("eaked" in body for _, body in self.said))

    def test_slow_down_takes_githubs_new_interval(self):
        answers = [{"error": "slow_down", "interval": 0.03}, {"access_token": "ghu_x"}]
        waits = []

        async def sleep(s):
            waits.append(s)
        with mock.patch.object(github_link, "post", lambda path, fields: asyncio.sleep(0, answers.pop(0))), \
                mock.patch.object(github_link, "who", lambda tok: asyncio.sleep(0, ("anna-gh", ANNA_ID))):
            flow = {"device_code": "d", "expires_at": 1e12, "interval": 0.02}
            asyncio.run(github_link.wait_for_login(flow, sleep))
        self.assertEqual(waits, [0.02, 0.03])

    def test_a_link_that_cant_be_saved_is_not_kept(self):
        self.fake.approve("anna-gh", ANNA_ID)
        with mock.patch.object(github_link.SavedDict, "_save", side_effect=OSError("disk full")):
            self.run_(self.lp.connect_github(ANNA))
            self.assertIsNone(github_link.link_of(ANNA))
            self.assertIn("couldn't save your GitHub link", self.in_room("!dm")[-1])
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        with mock.patch.object(github_link.SavedDict, "_save", side_effect=OSError("disk full")):
            self.assertIn("couldn't save", self.lp.disconnect_github(ANNA))
            self.assertIsNotNone(github_link.link_of(ANNA))                       # memory still matches the disk

    def test_disconnect_during_the_access_check_counts(self):
        self.fake.permissions[(REPO, "anna-gh")] = "write"
        self.fake.user_ids["anna-gh"] = ANNA_ID
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        real = github_app.permission

        async def meanwhile(*a):
            out = await real(*a)
            github_link.unlink(ANNA)                                                # Disconnect while GitHub answered
            return out
        with mock.patch.object(github_app, "permission", meanwhile):
            self.assertFalse(asyncio.run(github_link.may_write(ANNA, REPO)))

    def test_the_access_check_renews_a_refused_token(self):
        self.fake.permissions[(REPO, "anna-gh")] = "write"
        self.fake.user_ids["anna-gh"] = ANNA_ID
        github_app._tokens[REPO.lower()] = ("ghs_revoked", 1e12)
        self.assertEqual(asyncio.run(github_app.permission(REPO, "anna-gh", ANNA_ID)), "write")


class InstallWait(BindCase):
    """The App isn't on the repo: @pm says so with the install link and waits; once it is there,
    every check runs again and @pm says the answer (owner decision, batch 12–14: name the one step)."""

    def setUp(self):
        super().setUp()
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.fake.permissions[(OTHER, "anna-gh")] = "read"                          # no write access

    def wait(self):
        async def flow():
            self.assertEqual(await self.lp.set_repo(self.room, ANNA, OTHER), repo_refusals.install(OTHER))
            return self.lp.install_watches["!r"][1]
        return flow

    def test_installed_but_no_access_then_says_why(self):
        async def flow():
            task = await self.wait()()
            self.fake.installations[OTHER.lower()] = 8
            await asyncio.wait_for(task, 5)
        asyncio.run(flow())
        self.assertEqual(self.said, [repo_refusals.install(OTHER),
                                     "Your GitHub account @anna-gh can't write to octo/other, so this room can't use it."])
        self.assertEqual(self.written, [])

    def test_a_disconnect_while_binding_refuses(self):
        """GPT re-check: the link was checked when GitHub answered, then the bind awaited more."""
        self.fake.installations[OTHER.lower()] = 8
        self.fake.permissions[(OTHER, "anna-gh")] = "write"
        real = self.lp.primary_room_for

        async def meanwhile(*a, **k):
            github_link.unlink(ANNA)                                                # Disconnect GitHub
            return await real(*a, **k)
        with mock.patch.object(self.lp, "primary_room_for", meanwhile, create=True):
            self.assertEqual(asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)), repo_refusals.LINK_CHANGED)
        self.assertEqual(self.written, [])

    def test_never_installed_ends_quietly(self):
        async def flow():
            await asyncio.wait_for(await self.wait()(), 5)
        with mock.patch.object(pm_settings, "INSTALL_RECHECKS", 2):
            asyncio.run(flow())
        self.assertEqual(self.said, [repo_refusals.install(OTHER)])
        self.assertEqual(self.written, [])

    def test_a_private_repo_with_the_app_reads_like_one_without(self):
        """GPT review, batch 12–14: the same words and the same quiet end, so no room can tell that a
        private repo of that name exists and has our App."""
        missing = "octo/never-made"
        self.fake.installations[OTHER.lower()] = 8
        self.fake.private.add(OTHER.lower())                                        # installed, private, read only

        async def flow():
            first = await self.lp.set_repo(self.room, ANNA, OTHER)
            await asyncio.wait_for(self.lp.install_watches["!r"][1], 5)
            second = await self.lp.set_repo(self.room, ANNA, missing)
            await asyncio.wait_for(self.lp.install_watches["!r"][1], 5)
            return first, second
        with mock.patch.object(pm_settings, "INSTALL_RECHECKS", 2):
            first, second = asyncio.run(flow())
        self.assertEqual(first, repo_refusals.install(OTHER))
        self.assertEqual(second, repo_refusals.install(missing))
        self.assertEqual(first.replace(OTHER, "X"), second.replace(missing, "X"))
        self.assertEqual(self.said, [first, second])                               # nothing more for either
        self.assertEqual(self.written, [])


class BindRaceCase(BindCase):
    """Anna connected, the App installed; her standing in the room as the server says it now (self.now)."""

    def setUp(self):
        super().setUp()
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.fake.installations[OTHER.lower()] = 8
        self.now = {"membership": "join", "event_id": "$m", "moderator": True}   # Anna, as the server says now
        for name, value in (("joined_moderator", REAL_JOINED_MODERATOR),
                            ("standing", lambda c, rid, u: asyncio.sleep(0, dict(self.now)))):
            p = mock.patch.object(room_state, name, value)
            p.start()
            self.addCleanup(p.stop)


class BindRace(BindRaceCase):
    """GPT review (batch 5+6) #2: the link and the moderator check were read before the privacy check
    and the state read, which await; the write must re-check both, read at that moment."""

    def during_privacy_check(self, change):
        real = self.lp.close_for_private

        async def meanwhile(*a):
            change()
            return await real(*a)
        return mock.patch.object(self.lp, "close_for_private", meanwhile, create=True)

    def test_disconnect_during_the_privacy_check_binds_nothing(self):
        with self.during_privacy_check(lambda: github_link.unlink(ANNA)):
            self.assertEqual(asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)), repo_refusals.LINK_CHANGED)
        self.assertEqual(self.written, [])

    def test_demoted_during_the_privacy_check_binds_nothing(self):
        with self.during_privacy_check(lambda: self.now.update(moderator=False)):
            self.assertEqual(asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)), pm_settings.NOT_MODERATOR)
        self.assertEqual(self.written, [])

    def test_an_install_wait_never_binds_for_someone_who_left(self):
        """Their power-level entry stays after they leave (or they created the room): not enough."""
        self.fake.installations.pop(OTHER.lower())

        async def flow():
            await self.lp.set_repo(self.room, ANNA, OTHER)                          # the install link
            _, task = self.lp.install_watches["!r"]
            self.now["membership"] = "leave"                                        # Anna leaves the room
            self.fake.installations[OTHER.lower()] = 8                              # then the App is installed
            await asyncio.wait_for(task, 5)
        asyncio.run(flow())
        self.assertEqual(self.written, [])
        self.assertEqual(self.said[-1], pm_settings.NOT_MODERATOR)

    def test_still_joined_moderator_and_connected_binds(self):
        self.assertIn("now works on **octo/other**", asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)))


if __name__ == "__main__":
    unittest.main()
