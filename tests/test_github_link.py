"""Connect GitHub (ADR 0007): a person proves which GitHub account is theirs with GitHub's device flow,
then @pm checks their write access to a repo through that link. GitHub (github.com's sign-in and the
API) is the local fake in tests/fake_github_api.py. Run: .venv/bin/python -m unittest tests.test_github_link"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import console
from common import github_app
from server import github_link
from server import pm_github
from server import pm_settings
from server import repo_refusals
from server import project_rooms
from common import room_state
from server.loop import Loop
from tests.test_github_app import OTHER, REPO, AppCase
from tests.test_github_app_flows import ANNA, PM, BindingSetup

REAL_MAY_WRITE, REAL_LINK_OF = pm_settings.may_write, github_link.link_of   # before Binding patches them
ANNA_ID = 583231


def message(sender, body):
    return SimpleNamespace(sender=sender, body=body, source={"content": {"body": body}})


class LinkCase(AppCase):
    """Connect GitHub configured (Client ID, links file, no waiting between polls) and a Loop for Anna."""

    def setUp(self):
        super().setUp()
        links = Path(tempfile.mkdtemp()) / "github-links.json"
        for name, value in (("CLIENT_ID", self.fake.client_id), ("WEB", self.fake.url), ("LINKS_FILE", links),
                            ("_links", None), ("FLOWS", {}), ("MIN_INTERVAL_S", 0), ("SLOW_DOWN_S", 0.01)):
            p = mock.patch.object(github_link, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.fake.interval = 0
        self.said = []
        lp = self.lp = Loop.__new__(Loop)
        lp.user_id, lp.dm_rooms, lp._welcomes, lp._direct_chats, lp.busy = PM, {ANNA: "!dm"}, {}, {}, set()
        self.room = SimpleNamespace(room_id="!r", users={ANNA: 0, PM: 0, "@betty:bridge.localhost": 0},
                                    invited_users={}, display_name="Hello")
        self.dm = SimpleNamespace(room_id="!dm", users={ANNA: 0, PM: 0}, invited_users={})
        lp.client = SimpleNamespace(rooms={"!r": self.room, "!dm": self.dm})
        lp.is_person = lambda room, user: user != PM

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
            return f"$m{len(self.said)}"
        lp.say, lp.private_room = say, mock.AsyncMock(return_value="!dm")

    def run_(self, coro):
        async def go():
            out = await coro
            flows = [f["task"] for f in github_link.FLOWS.values()]                      # @pm's poll
            await asyncio.wait_for(asyncio.gather(*flows, return_exceptions=True), 5)
            return out
        return asyncio.run(go())

    def in_room(self, room_id):
        return [body for rid, body in self.said if rid == room_id]

    def assert_no_token_shown(self):
        for _, body in self.said:
            self.assertNotIn("ghu_", body)
            self.assertNotIn("dev", body.split("github.com/login/device")[-1])       # never the device code


class Connect(LinkCase):
    """Anna clicks Connect GitHub (the panel) or says "connect my github" (chat); GitHub is the fake."""

    def test_panel_click_code_privately_then_connected(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending", "authorization_pending"])

        async def flow():
            answer = await console.act(self.lp, "!r", ANNA, "github", "connect")
            self.assertEqual(answer, pm_github.ON_CARD)       # the line under the buttons: never the code (it'd stay)
            self.assertEqual(github_link.status(ANNA)["code"], "ABCD-1000")         # the card shows it too
            self.assertEqual(github_link.status(ANNA)["url"], "https://github.com/login/device")
            self.assertIsNone(github_link.status("@betty:bridge.localhost")["code"])   # only Anna sees hers
            await github_link.FLOWS[ANNA]["task"]
        asyncio.run(flow())
        self.assertIn("ABCD-1000", self.in_room("!dm")[0])
        self.assertEqual(self.in_room("!r"), [])                                     # never in the project room
        self.assertIn("✓ Connected: your GitHub account is **@anna-gh**.", self.in_room("!dm"))
        self.assertEqual(github_link.status(ANNA), {"login": "anna-gh", "code": None, "url": None})
        self.assertEqual(github_link.link_of(ANNA)["id"], ANNA_ID)
        self.assertNotIn("token", github_link.LINKS_FILE.read_text())               # only login and id are kept
        self.assertNotIn("ghu_", github_link.LINKS_FILE.read_text())
        self.assert_no_token_shown()

    def test_saying_it_in_a_project_room_sends_the_code_privately(self):
        self.fake.approve("anna-gh", ANNA_ID)
        self.run_(self.lp.on_welcome_text(self.room, message(ANNA, "@pm connect my github")))
        self.assertEqual(self.in_room("!r"), ["I sent you the GitHub code in our private chat."])
        self.assertIn("ABCD-1000", self.in_room("!dm")[0])
        self.assertEqual(github_link.link_of(ANNA)["login"], "anna-gh")

    def test_saying_it_in_the_private_chat(self):
        self.fake.approve("anna-gh", ANNA_ID)
        self.assertTrue(self.run_(self.lp.on_github_text(self.dm, message(ANNA, "connect my GitHub account"))))
        self.assertIn("ABCD-1000", self.in_room("!dm")[0])
        self.run_(self.lp.on_github_text(self.dm, message(ANNA, "disconnect my github")))
        self.assertEqual(self.in_room("!dm")[-1], "Disconnected: I no longer know a GitHub account for you.")
        self.assertIsNone(github_link.link_of(ANNA))
        self.assertIsNone(github_link.status(ANNA)["login"])                       # the card shows Connect again

    def test_other_words_are_left_alone(self):
        for text in ("@pm connect my github later maybe, what do you think", "github is down", "@pm where are we?"):
            self.assertFalse(asyncio.run(self.lp.on_github_text(self.room, message(ANNA, text))), text)
        self.assertEqual(self.said, [])

    def test_asking_twice_shows_the_same_code_one_sign_in(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 3)

        async def flow():
            first = await self.lp.connect_github(ANNA)
            second = await self.lp.connect_github(ANNA)
            self.assertEqual(first, second)
            await github_link.FLOWS[ANNA]["task"]
        asyncio.run(flow())
        self.assertEqual(len([r for r in self.fake.requests if r[1] == "/login/device/code"]), 1)

    def test_two_clicks_at_once_ask_github_once(self):
        self.fake.approve("anna-gh", ANNA_ID)
        async def both():
            return await asyncio.gather(self.lp.connect_github(ANNA), self.lp.connect_github(ANNA))
        answers = self.run_(both())
        self.assertEqual(sum("ABCD-1000" in a for a in answers), 1)
        self.assertEqual(len([r for r in self.fake.requests if r[1] == "/login/device/code"]), 1)

    def test_declined_or_expired_connects_nothing(self):
        for answer, words in (("access_denied", "You declined"), ("expired_token", "expired")):
            self.fake.approve("anna-gh", ANNA_ID, answers=[answer])
            self.run_(self.lp.connect_github(ANNA))
            self.assertIn(words, self.in_room("!dm")[-1])
            self.assertIsNone(github_link.link_of(ANNA))
            self.assertNotIn(ANNA, github_link.FLOWS)                                # a new click starts afresh

    def test_a_code_never_waited_on_past_its_expiry(self):
        flow = {"device_code": "x", "expires_at": time.time() - 1, "interval": 0}
        with self.assertRaises(github_link.LinkFailed):
            asyncio.run(github_link.wait_for_login(flow))
        self.assertFalse(any(r[1] == "/login/oauth/access_token" for r in self.fake.requests))

    def test_slow_down_waits_longer(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["slow_down"])
        self.run_(self.lp.connect_github(ANNA))
        self.assertEqual(github_link.link_of(ANNA)["login"], "anna-gh")
        self.assertEqual(len([r for r in self.fake.requests if r[1] == "/login/oauth/access_token"]), 2)

    def test_a_few_codes_per_person_per_hour(self):
        for _ in range(pm_github.STARTS_PER_HOUR):
            self.fake.approve("anna-gh", ANNA_ID, answers=["access_denied"])
            self.run_(self.lp.connect_github(ANNA))
        before = len(self.fake.requests)
        self.assertIn("several GitHub codes this hour", asyncio.run(self.lp.connect_github(ANNA)))
        self.assertEqual(len(self.fake.requests), before)                           # GitHub not asked again

    def test_disconnect_cancels_a_waiting_sign_in(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 50)

        async def flow():
            await self.lp.connect_github(ANNA)
            task = github_link.FLOWS[ANNA]["task"]
            self.assertEqual(await console.act(self.lp, "!r", ANNA, "github", "disconnect"), "Cancelled the GitHub sign-in.")
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(flow())
        self.assertIsNone(github_link.link_of(ANNA))
        self.assertIsNone(github_link.status(ANNA)["code"])

    def test_device_flow_not_enabled_on_the_app_says_what_to_do(self):
        self.fake.device_flow = False
        answer = asyncio.run(self.lp.connect_github(ANNA))
        self.assertIn("Enable Device Flow", answer)
        self.assertEqual(github_link.FLOWS, {})

    def test_off_without_a_client_id(self):
        with mock.patch.object(github_link, "CLIENT_ID", ""):
            self.assertIsNone(github_link.status(ANNA))                              # no card in the panel
            self.assertEqual(asyncio.run(self.lp.connect_github(ANNA)), pm_github.OFF)
        self.assertEqual(self.fake.requests, [])

    def test_the_console_state_carries_the_card(self):
        self.lp.pending = {}
        self.lp.level, self.lp.agent_online = (lambda rid: asyncio.sleep(0, "off")), (lambda agent: None)
        self.lp.solo_on = lambda rid: asyncio.sleep(0, False)
        with mock.patch.object(room_state, "is_moderator", lambda c, r, u: asyncio.sleep(0, True)), \
                mock.patch.object(room_state, "repo_of", lambda c, rid: asyncio.sleep(0, None)), \
                mock.patch.object(room_state, "discussion_of", lambda c, rid: asyncio.sleep(0, False)), \
                mock.patch.object(project_rooms, "visibility", lambda c, rid: asyncio.sleep(0, "private")):
            github_link.save_link(ANNA, "anna-gh", ANNA_ID)
            self.assertEqual(asyncio.run(console.view(self.lp, "!r", ANNA))["github"]["login"], "anna-gh")


class WriteAccess(LinkCase):
    """may_write on the public server: the connected account's access, read as the App."""

    def test_only_a_connected_account_with_write_access_passes(self):
        self.fake.permissions[(REPO, "anna-gh")] = "write"
        self.fake.user_ids["anna-gh"] = ANNA_ID
        self.assertFalse(asyncio.run(github_link.may_write(ANNA, REPO)))            # not connected yet
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.assertTrue(asyncio.run(github_link.may_write(ANNA, REPO)))
        self.assertTrue(asyncio.run(pm_settings.may_write(ANNA, REPO)))             # the bind check uses it
        for perm in ("admin", "read", "none"):
            self.fake.permissions[(REPO, "anna-gh")] = perm
            self.assertEqual(asyncio.run(github_link.may_write(ANNA, REPO)), perm == "admin", perm)

    def test_a_renamed_username_taken_by_someone_else_fails(self):
        self.fake.permissions[(REPO, "anna-gh")] = "admin"
        self.fake.user_ids["anna-gh"] = 999                                          # a different account now
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.assertFalse(asyncio.run(github_link.may_write(ANNA, REPO)))
        github_link.links()[ANNA] = {"login": "../x", "id": ANNA_ID}                 # a damaged entry: none
        self.assertIsNone(github_link.link_of(ANNA))

    def test_github_unreachable_fails_closed(self):
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        with mock.patch.object(github_app, "API", "http://127.0.0.1:9"), self.assertLogs("loop", "WARNING"):
            self.assertFalse(asyncio.run(github_link.may_write(ANNA, REPO)))


class BindCase(BindingSetup):
    """A room on the public server with the real checks: Anna's link and her access, as GitHub says."""

    def setUp(self):
        super().setUp()
        links = Path(tempfile.mkdtemp()) / "github-links.json"
        for target, value in (("server.pm_settings.may_write", REAL_MAY_WRITE), ("server.github_link.link_of", REAL_LINK_OF)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("LINKS_FILE", links), ("_links", None), ("FLOWS", {})):
            p = mock.patch.object(github_link, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.fake.permissions[(OTHER, "anna-gh")] = "write"        # what GitHub reports for maintain too
        self.fake.user_ids["anna-gh"] = ANNA_ID



class ConnectThenBind(BindCase):
    """The whole flow on the public server: not connected → Connect GitHub → the room is bound."""

    def test_not_connected_then_connected_binds(self):
        self.assertEqual(asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)), pm_settings.NOT_CONNECTED)
        self.assertEqual(self.lp.install_watches, {})
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)                              # after Connect GitHub
        self.fake.installations[OTHER.lower()] = 8
        self.assertIn("now works on **octo/other**", asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)))

    def test_no_write_access_names_their_account_for_a_public_repo(self):
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.fake.installations[OTHER.lower()] = 8
        self.fake.permissions[(OTHER, "anna-gh")] = "read"
        self.assertEqual(asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER)),
                         "Your GitHub account @anna-gh can't write to octo/other, so this room can't use it.")
        self.assertEqual(self.written, [])

    def test_no_write_access_to_a_private_repo_confirms_nothing(self):
        """"The App is on it but you can't write" would tell anyone that a private repo of that name exists."""
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.fake.installations[OTHER.lower()] = 8
        self.fake.private.add(OTHER.lower())
        self.fake.permissions[(OTHER, "anna-gh")] = "read"
        answer = asyncio.run(self.lp.set_repo(self.room, ANNA, OTHER))
        self.assertEqual(answer, repo_refusals.install(OTHER))                  # as for a repo without the App
        self.assertNotIn("can't write", answer)
        self.assertEqual(self.written, [])

    def test_app_not_installed_yet_waits_then_checks_access_and_binds(self):
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)

        async def flow():
            self.assertEqual(await self.lp.set_repo(self.room, ANNA, OTHER), repo_refusals.install(OTHER))
            self.assertIn("/installations/new", repo_refusals.install(OTHER))           # the one-click link
            _, task = self.lp.install_watches["!r"]
            self.fake.installations[OTHER.lower()] = 8                               # the owner clicks Install
            await asyncio.wait_for(task, 5)
        asyncio.run(flow())
        self.assertEqual(self.written, [room_state.content("off", OTHER)])


if __name__ == "__main__":
    unittest.main()
