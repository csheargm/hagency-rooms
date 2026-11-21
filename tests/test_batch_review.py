"""GPT review of the combined batch 12–14 (R15): each adopted point, as a test. Logged in REVIEWS.md
under "GPT review: batch 12–14 combined". Run: .venv/bin/python -m unittest tests.test_batch_review"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common import agent_kinds
from server import connect_page
from server import connect_status
from server import pairing
from server import project_rooms
from server import pm_unbind
from common import registry
from server import remote_agent
from tests import test_remove_agent as removal
from tests import test_remove_repo as repos
from tests import test_more_agents_flows as flows
from tests.test_more_agents_flows import CX, text


class StaleRemoveConfirmation(removal.InChat):
    def test_a_confirmed_account_never_matches_another_agents_display_name(self):
        """The ✅ names an account; once it is gone, another agent *called* that account is never removed."""
        self.assertFalse(asyncio.run(self.lp.remove_for_good("!dm", CX, "cx-gone-agent")))
        with mock.patch.object(registry, "agents_of", lambda p, *a: [
                {"localpart": "cx-other-agent", "label": "other", "first": False, "agent": registry.REMOTE,
                 "display": "cx-codex-agent"}] if p == CX else []), mock.patch.object(registry, "owns", lambda p, a: True):
            self.assertFalse(asyncio.run(self.lp.remove_for_good("!dm", CX, "cx-codex-agent")))
        self.remove.assert_not_called()


class StatusDuringRedeem(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.codes = pairing.Codes(Path(tempfile.mkdtemp()) / "p.json")
        for target, name, value in ((remote_agent, "CODES", self.codes), (remote_agent, "LIMIT", pairing.Limiter())):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        app = web.Application()
        app.add_routes([*connect_page.routes(), *remote_agent.routes()])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_the_page_says_waiting_while_the_sign_in_is_looked_up(self):
        code, seen = self.codes.issue("newbie1"), []
        loop = asyncio.get_running_loop()

        def lookup(*a):                       # the page asks while POST /connect is still looking it up
            seen.append(asyncio.run_coroutine_threadsafe(self.ask(code), loop).result(5))
            return {"user_id": "@newbie1-agent:x", "password": "p"}
        with mock.patch.object(remote_agent, "locked_credentials", lookup):
            r = await self.client.post("/connect", json={"code": code})
        self.assertEqual(r.status, 200)
        self.assertEqual(seen, ["waiting"])                  # never "expired" in between
        self.assertEqual(await self.ask(code), "connected")

    async def ask(self, code):
        r = await self.client.post(connect_page.STATUS_PATH, json={"code": code})
        return (await r.json())["state"]

    async def test_concurrent_wrong_codes_never_pass_the_budget(self):
        answers = await asyncio.gather(*(self.client.post(connect_page.STATUS_PATH, json={"code": f"WRONG-{i:05d}"})
                                         for i in range(12)))
        self.assertEqual(sum(r.status == 429 for r in answers), 12 - pairing.FAILS_PER_CLIENT)

    def tearDown(self):
        connect_status.LIMIT.fails.clear()
        connect_status._asks.clear()


class RemoveRepoEdges(repos.Setup):
    def ask(self, room, waiting=None):
        """"@pm remove the repo" in `room`, with an install wait running there (a coroutine) or none."""
        async def go():
            if waiting is not None:
                self.lp.install_watches[room.room_id] = ("acme/next", asyncio.ensure_future(waiting()))
                await asyncio.sleep(0)
            body = "@pm remove the repo"
            await self.lp.on_project_text(room, SimpleNamespace(sender=repos.ANNA, body=body, source={"content": {"body": body}}))
        asyncio.run(go())
        return self.said[-1][1]

    def test_remove_the_repo_ends_a_pending_install_wait_in_the_same_words(self):
        self.state["!b"] = {}
        ended = []

        async def wait():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                ended.append(True)
                raise
        self.assertEqual(self.ask(self.b, wait), pm_unbind.NO_REPO_HERE)
        self.assertEqual((ended, self.lp.install_watches), ([True], {}))
        self.assertEqual(self.ask(self.b), pm_unbind.NO_REPO_HERE)          # no wait: the very same words
        self.assertNotIn("acme/next", " ".join(b for _, b in self.said))    # a quiet wait's repo is never named

    def test_a_bind_finishing_as_the_wait_ends_is_asked_about(self):
        self.state["!b"] = {}

        async def binding():                  # cancelled in its shielded bind: the bind still finishes
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.bind("!b")
                raise
        self.assertIn(f"**{repos.REPO}**", self.ask(self.b, binding))      # the ✅ question, not "no repo"

    def test_a_private_repos_room_never_becomes_open_after(self):
        with mock.patch("server.project_rooms.repo_is_private", mock.AsyncMock(return_value=True)):
            self.assertEqual(asyncio.run(self.lp.remove_repo(self.a, repos.ANNA, repos.REPO)), pm_unbind.DONE)
        self.assertEqual(self.state["!a"][project_rooms.PRIVATE_PAST_EVENT][0], {"private": True})
        with mock.patch.object(project_rooms, "apply", mock.AsyncMock(return_value=None)) as applied, \
                mock.patch("server.room_address.follow", mock.AsyncMock(return_value="")):
            self.assertEqual(asyncio.run(self.lp.set_visibility(self.a, repos.ANNA, "open")), project_rooms.NOT_OPEN_PAST)
            applied.assert_not_called()
            self.assertIn("listed", asyncio.run(self.lp.set_visibility(self.a, repos.ANNA, "listed")))   # still allowed

    def test_a_public_repos_room_may_open_after(self):
        self.assertEqual(asyncio.run(self.lp.remove_repo(self.a, repos.ANNA, repos.REPO)), pm_unbind.DONE)
        self.assertFalse(asyncio.run(project_rooms.had_private_repo(self.lp.client, "!a")))


class AddressedInPrivate(flows.Provisioning):
    def test_at_pm_typed_in_our_private_chat_reads_the_same(self):
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"),
                                          text(CX, "@pm add another agent called pi using claude")))
        self.assertEqual(registry.find(CX, "pi")["agent_kind"], "claude")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Pages(unittest.TestCase):
    def node(self, program: str) -> str:
        return subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True, timeout=30,
                              check=True).stdout

    def test_hermes_on_windows_shows_no_line_to_copy(self):
        js = json.dumps(str(connect_page.STATIC / "connect.js"))
        kinds = json.dumps(agent_kinds.kinds())
        out = self.node(f"const m = require({js}); const k = {kinds}; process.stdout.write(JSON.stringify(["
                        "m.shownCommand('windows', 'https://c', 'ABCDE-FGHJK', 'hermes', k),"
                        "m.shownCommand('mac', 'https://c', 'ABCDE-FGHJK', 'hermes', k)]))")
        windows, mac = json.loads(out)
        self.assertEqual(windows, "")
        self.assertTrue(mac.endswith("ABCDE-FGHJK --agent hermes"))

    def test_the_panel_hint_names_what_pm_resends_by(self):
        from server import panel
        import re
        esc = re.search(r"^const esc = .*$", panel.PAGE, re.M).group(0)
        source = "\n".join([esc, "const cap = t => t;", panel.SCRIPTS["panel-agents.js"].read_text()])
        rows = [{"remote": True, "online": False, "connected": False, "first": False, "label": "codex2", "display": "codex"},
                {"remote": True, "online": False, "connected": False, "first": False, "label": "lap", "display": "My laptop"}]
        out = self.node(source + f"\nprocess.stdout.write(JSON.stringify({json.dumps(rows)}.map(agentHint)))")
        reused, spaced = json.loads(out)
        self.assertIn("add another agent called codex&quot;", reused.replace('"', "&quot;"))
        self.assertNotIn("codex2", reused)
        self.assertIn("Rename it here to one short word", spaced)


class SwitchFromPrivate(repos.Setup):
    def test_switching_from_a_private_repo_keeps_the_room_from_opening(self):
        """GPT review (any repo): the marker was set on removal only, not when another repo replaced it."""
        private = mock.AsyncMock(side_effect=lambda r: r == repos.REPO)
        with mock.patch("server.project_rooms.repo_is_private", private):
            self.assertIn("now works on **a/public-one**", asyncio.run(self.lp.bind("!a", "a/public-one")))
            self.assertEqual(self.state["!a"][project_rooms.PRIVATE_PAST_EVENT][0], {"private": True})
            self.assertTrue(asyncio.run(project_rooms.had_private_repo(self.lp.client, "!a")))

    def test_switching_from_a_public_repo_marks_nothing(self):
        asyncio.run(self.lp.bind("!a", "a/public-one"))
        self.assertNotIn(project_rooms.PRIVATE_PAST_EVENT, self.state["!a"])
