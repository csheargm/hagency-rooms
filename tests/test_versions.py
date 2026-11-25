"""Versions: the panel's footer shows the server's code, each agent reports its own, My agents says Up
to date / Update available, and Update is its owner's click only (common/version.py, code_report.py,
server/console_update.py, pm_kind_reports.record_code; docs/adr/0020-versions-and-agent-self-update.md).
Run: .venv/bin/python -m unittest tests.test_versions"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nio import ToDeviceEvent, ToDeviceResponse, UnknownToDeviceEvent

from common import code_report
from common import registry
from common import version
from deploy import release
from server import console
from server import console_update
from server import panel
from tests import test_more_agents_flows as flows
from tests.test_kind_report import FakeClient
from tests.test_more_agents_flows import CX, CXA, BA

ROOT = Path(__file__).resolve().parent.parent
OLD, NEW = {"commit": "a" * 40, "time": 1_763_800_000}, {"commit": "b" * 40, "time": 1_764_100_000}
NODE = shutil.which("node")


class Version(unittest.TestCase):
    def test_revision_file_then_git(self):
        d = Path(tempfile.mkdtemp())
        (d / "REVISION").write_text(f"{NEW['commit']}\n{NEW['time']}\n")
        self.assertEqual(version.of(d), NEW)
        self.assertEqual(version.label(NEW), "bbbbbbb · 2025-11-25")
        (d / "REVISION").write_text("nonsense\n")
        self.assertIsNone(version.of(d))
        self.assertIsNone(version.of(Path(tempfile.mkdtemp())))          # neither file nor checkout
        here = version.of(ROOT)                                          # this checkout: its HEAD
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(here["commit"], head)

    def test_only_whole_seconds_and_full_commits(self):
        for when in ("+5", "1_000", " 5", "0", "-1", "", True, 1.5, "999999999999"):   # the last: year 33658 (GPT review)
            self.assertIsNone(version.parse("a" * 40, when), when)
        self.assertIsNone(version.parse("abc", "5"))
        self.assertIsNone(version.parse("A" * 40, "5"))

    def test_up_to_date_is_the_servers_commit_or_a_later_one(self):
        self.assertTrue(version.current(NEW, OLD) and version.current(OLD, OLD))
        self.assertFalse(version.current(OLD, NEW))
        self.assertFalse(version.current(None, NEW) or version.current(NEW, None))

    def test_the_footer_shows_the_servers_version(self):
        self.assertIn(f"Hagency Rooms {version.label(console_update.SERVER)}", panel.PAGE)
        self.assertIn("Change anything here or by telling @pm in the chat.", panel.PAGE)
        self.assertNotIn("__VERSION__", panel.PAGE)

    def test_deploy_writes_revision_and_each_release_clears_it(self):
        script = (ROOT / "deploy" / "deploy.sh").read_text()
        self.assertIn("""git -C "$app" log -1 --format='%H%n%ct' "$sha" > "$pm/REVISION\"""", script)
        self.assertLess(script.index("archive"), script.index("REVISION"))   # after the files: release.py cleared the old one
        d = Path(tempfile.mkdtemp())
        (d / "REVISION").write_text("old")
        self.assertIn(d / "REVISION", release.stale(d, {"bin/loop.py"}))
        self.assertEqual(version.REVISION_FILE, "REVISION")


class Reports(flows.Base):
    def setUp(self):
        super().setUp()
        self.online = {"cx-agent"}
        self.lp.agent_online = lambda a: True if a in self.online else None
        self.lp.ever_connected = lambda a: True
        self.lp.client = SimpleNamespace(to_device=mock.AsyncMock(side_effect=lambda m, tx: ToDeviceResponse(m)),
                                         rooms={"!r": SimpleNamespace(users={CX: 0}, invited_users={}, name="P", room_id="!r")})
        self.lp.is_person = lambda r, u: True
        p = mock.patch.object(console_update, "SERVER", NEW)
        p.start()
        self.addCleanup(p.stop)

    def start(self, runs, own=False, sender=CXA):
        """The agent's start on its computer (code_report.send), then @pm receiving it."""
        client = FakeClient(sender)
        self.assertTrue(asyncio.run(code_report.send(client, flows.PM, code_report.build(runs, own))))
        for event in client.delivered():
            self.assertIsInstance(event, UnknownToDeviceEvent)
            asyncio.run(self.lp.on_kind_report(event))

    def row(self):
        return console.my_agents(self.lp, SimpleNamespace(users={}, invited_users={}), "!r", CX)[0]

    def click(self, user=CX, agent="cx-agent"):
        return asyncio.run(console.act(self.lp, "!r", user, "selfupdate", "", agent))

    def test_an_agent_from_before_versions_offers_the_connect_page_once(self):
        row = self.row()
        self.assertEqual((row["code"], row["code_fallback"]), ("unknown", True))
        self.assertIn("Update my agent", asyncio.run(console_update.request(self.lp, CX, "cx-agent")))
        self.lp.client.to_device.assert_not_awaited()
        with mock.patch.object(console, "fresh_code", mock.AsyncMock(return_value=console.UPDATE)) as fresh:
            said = asyncio.run(console.act(self.lp, "!r", CX, "update", "agents", "cx-agent"))
        fresh.assert_awaited_once_with(self.lp, CX, "cx-agent", update=True)   # PR #58's Connect page
        self.assertEqual(said, {"result": "ok", "says": console_update.BY_HAND})    # My agents' words, not Your cards'

    def test_report_then_up_to_date_or_update_available(self):
        self.start(NEW)
        self.assertEqual((self.row()["code"], self.row()["code_version"]), ("current", version.label(NEW)))
        self.start(OLD)
        self.assertEqual(self.row()["code"], "behind")
        self.start(OLD, own=True)
        self.assertEqual(self.row()["code"], "own")
        self.assertEqual(asyncio.run(console_update.request(self.lp, CX, "cx-agent")), console_update.SAYS["own"])
        self.lp.client.to_device.assert_not_awaited()

    def test_another_account_cant_set_someone_elses_version(self):
        self.start(OLD)
        self.start(NEW, sender=BA)                    # betty's agent: its own entry only
        self.assertEqual(self.row()["code"], "behind")
        self.start(NEW, sender=CX)                    # a person's own account isn't an agent
        self.assertEqual(self.row()["code"], "behind")
        for bad in ({"commit": "a" * 40, "time": "5", "api_key": "x"}, {"commit": "a" * 7, "time": "5"},
                    {"commit": "a" * 40, "time": 5}, {"commit": "a" * 40, "time": "5", "own": "no"}, "text"):
            self.assertIsNone(code_report.valid(bad), bad)

    def test_update_is_the_owners_click_then_the_agents_answer_then_its_new_report(self):
        self.start(OLD)
        self.assertEqual(asyncio.run(console_update.request(self.lp, "@betty:bridge.localhost", "cx-agent")),
                         "that isn't one of your agents on your own computer")
        self.lp.client.to_device.assert_not_awaited()
        self.assertEqual(self.click(), "ok")
        message = self.lp.client.to_device.await_args.args[0]
        self.assertEqual((message.type, message.recipient, message.content),
                         (code_report.UPDATE_TYPE, CXA, {"commit": NEW["commit"], "id": self.lp._updates["cx-agent"]["id"]}))   # the server's commit
        self.assertTrue(self.row()["code_updating"])
        self.assertEqual(self.click(), console_update.UPDATING)                        # a second click sends nothing
        self.assertEqual(self.lp.client.to_device.await_count, 1)
        self.answer(CXA, "updated")
        self.start(NEW)                                                                # restarted on the new code
        row = self.row()
        self.assertEqual((row["code"], row["code_says"], row["code_updating"]), ("current", "", False))

    def answer(self, sender, result, ask=True):
        ask = self.lp.__dict__.get("_updates", {}).get(sender[1:].split(":")[0], {}).get("id") if ask is True else ask
        event = ToDeviceEvent.parse_event({"sender": sender, "type": code_report.RESULT_TYPE, "content": {"result": result, "id": ask}})
        asyncio.run(self.lp.on_kind_report(event))

    def test_a_refusal_is_said_in_plain_words_and_only_for_a_click_waiting(self):
        self.start(OLD)
        self.answer(CXA, "edited")                    # nobody clicked: ignored
        self.assertEqual(self.row()["code_says"], "")
        self.click()
        self.answer(BA, "edited")                     # another agent can't answer for it
        self.answer(CXA, "rm -rf")                    # not a word we show
        self.answer(CXA, "edited", ask="an older click")   # another click's answer
        self.assertTrue(self.row()["code_updating"])
        self.answer(CXA, "edited")
        row = self.row()
        self.assertEqual((row["code_says"], row["code_fallback"]), (console_update.SAYS["edited"], True))

    def test_no_answer_is_said_after_a_while_and_update_comes_back(self):
        self.start(OLD)
        self.click()
        self.lp._updates["cx-agent"]["at"] -= console_update.WAIT_S + 1
        row = self.row()
        self.assertEqual((row["code_says"], row["code_updating"]), (console_update.NO_ANSWER, False))
        self.assertEqual(self.click(), "ok")          # past the gap: asked again
        self.assertEqual(self.lp.client.to_device.await_count, 2)

    def test_a_failed_send_frees_the_click(self):
        self.start(OLD)
        self.lp.client.to_device.side_effect = OSError("down")
        self.assertIn("couldn't reach", self.click())
        self.assertNotIn("cx-agent", self.lp._updates)

    def test_hermes_gets_the_connect_page_words(self):
        registry.update(CX, None, agent_kind="hermes")
        self.start(OLD)
        row = self.row()
        self.assertEqual((row["code"], row["code_fallback"], row["code_says"]), ("unknown", True, console_update.HERMES))

    def test_offline_agents_are_not_asked(self):
        self.start(OLD)
        self.online.clear()
        self.assertIn("offline", self.click())
        self.lp.client.to_device.assert_not_awaited()


@unittest.skipIf(NODE is None, "node not installed")
class Panel(unittest.TestCase):
    """panel_agents.js's row for each state, run in node."""
    def row(self, **a):
        agent = {"name": "cx-agent", "label": "main", "first": True, "follow": "always", "remote": True, "online": True,
                 "connected": True, "here": False, "invited": False, "code_says": "", "code_version": "", **a}
        src = (ROOT / "static" / "panel_agents.js").read_text()
        prog = (src + "\nconst esc = t => String(t).replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';');"
                "const cap = t => t; process.stdout.write(agentRow(JSON.parse(process.argv[1]), null, false, false));")
        return subprocess.run([NODE, "-e", prog, json.dumps(agent)], capture_output=True, text=True, timeout=30, check=True).stdout

    def test_each_state_shows_its_words_and_button(self):
        self.assertIn("Up to date", self.row(code="current", code_version="bbbbbbb · 2025-11-25"))
        behind = self.row(code="behind", code_version="aaaaaaa · 2025-11-22")
        self.assertIn('data-a="selfupdate"', behind)
        self.assertIn("Update available", behind)
        self.assertIn("Version aaaaaaa · 2025-11-22", behind)
        old = self.row(code="unknown", code_fallback=True, code_says=console_update.OLD_CODE)
        self.assertIn('data-a="update" data-v="agents"', old)
        self.assertIn("once", old)
        self.assertNotIn("selfupdate", self.row(code="behind", code_updating=True, code_says=console_update.UPDATING))
        self.assertIn("Your own version", self.row(code="own"))
        self.assertNotIn("Update", self.row(code="behind", online=False, connected=False))


if __name__ == "__main__":
    unittest.main()
