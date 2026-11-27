"""My agents' "Sign in to GitHub" when either side restarts, and the rows that can't have the button
(server/console_github.py, adapters/agents/agent_github.py; ADR 0021). Review findings, 2026-11-26:
a restart of @pm or of the agent locked the owner out for 15 minutes; Hermes and older agents were nagged.
Run: .venv/bin/python -m unittest tests.test_console_github_restart"""
import asyncio
import os
import unittest
from unittest import mock

from nio import ToDeviceEvent

from adapters.agents import agent_github
from common import github_login
from common import kind_report
from common import registry
from server import console
from server import console_github
from tests import test_console_github as base
from tests.test_more_agents_flows import CX, CXA


class Restarts(base.Flow):
    async def deliver_now(self, sent):
        for kind, _, content in sent:
            await self.lp.on_kind_report(ToDeviceEvent.parse_event({"sender": CXA, "type": kind, "content": content}))
        sent.clear()

    async def code_reaches_pm(self):
        """@pm's latest request reaches the agent, and its code comes back to @pm."""
        ask = self.lp.client.to_device.await_args.args[0].content["id"]
        await self.agent.on_github_request(agent_side_request(ask))
        for _ in range(200):
            if any("code" in c for t, _, c in self.agent.sent if t == github_login.ANSWER_TYPE):
                break
            await asyncio.sleep(0.05)
        await self.deliver_now(self.agent.sent)
        return ask

    def test_after_pm_restarts_a_new_click_gets_a_new_code(self):
        self.start()
        os.environ["FAKE_GH_MODE"] = "hang"

        async def go():
            with mock.patch.object(agent_github, "SIGNIN_S", 1):
                await console.act(self.lp, "!r", CX, "github_signin", "", "cx-agent")
                first = await self.code_reaches_pm()
                self.lp._gh_signins.clear()               # @pm restarted: its clicks were in memory only
                self.said.clear()
                said = await console.act(self.lp, "!r", CX, "github_signin", "", "cx-agent")
                self.assertEqual(said["result"], "ok")
                second = await self.code_reaches_pm()
                self.assertNotEqual(first, second)
                self.assertEqual(self.row()["gh_code"], "WDJB-MJHT")   # the owner has a code again, at once
                self.assertEqual(len(self.said), 1)
                await self.agent._gh_job
        asyncio.run(go())

    def test_an_agent_restart_mid_sign_in_brings_the_button_back(self):
        self.start()
        self.click()
        ask = self.lp._gh_signins["cx-agent"]["id"]
        self.deliver([(github_login.ANSWER_TYPE, None, {"id": ask, "code": "WDJB-MJHT"})])
        self.assertTrue(self.row()["gh_busy"])
        self.start()                                        # it restarted (an Update, a crash): its start report
        row = self.row()
        self.assertEqual((row["gh_busy"], row["gh_code"], row["gh_says"]), (False, "", console_github.NOT_SIGNED))
        self.assertEqual(self.click()["result"], "ok")      # and a click works at once

    def test_a_finished_click_is_kept_after_its_state_report(self):
        self.start()
        self.click()
        self.deliver(self.pm_asks_agent(mode="fail"))       # its result, then its state
        self.assertEqual(self.row()["gh_says"], console_github.SAYS["failed"])

    def test_pm_waits_longer_than_the_agents_worst_case(self):
        worst = agent_github.HELP_S + agent_github.CODE_WAIT_S + kind_report.SEND_TIMEOUT_S
        self.assertGreater(console_github.CODE_WAIT_S, worst)


class Rows(base.Flow):
    def test_a_hermes_agent_gets_no_github_line(self):
        self.start()
        registry.update(CX, None, agent_kind="hermes")
        row = self.row()
        self.assertEqual((row["gh"], row["gh_says"], row["gh_cli"]), ("", "", ""))
        self.assertEqual(self.click(), console_github.HERMES)
        self.lp.client.to_device.assert_not_awaited()

    def test_an_older_agent_gets_one_soft_line(self):
        row = self.row()                                    # never sent a state: it may well be signed in already
        self.assertEqual((row["gh"], row["gh_says"]), ("old", console_github.OLD_CODE))
        self.assertNotIn("first", console_github.OLD_CODE)

    def test_an_offline_one_says_to_start_it(self):
        self.start()
        self.online.clear()
        self.assertEqual(self.row()["gh_says"], console_github.OFFLINE)
        if base.NODE is None:
            self.skipTest("node not installed")
        shown = self.panel()["html"]
        self.assertIn("Start it on its computer", shown)
        self.assertNotIn('data-a="github_signin"', shown)

    def test_the_code_has_copy_like_the_github_card(self):
        if base.NODE is None:
            self.skipTest("node not installed")
        self.start()
        self.click()
        ask = self.lp._gh_signins["cx-agent"]["id"]
        self.deliver([(github_login.ANSWER_TYPE, None, {"id": ask, "code": "WDJB-MJHT"})])
        shown = self.panel()["html"]
        self.assertIn('id="ghcode-cx-agent" class="code" readonly size="10" value="WDJB-MJHT"', shown)
        self.assertIn('<button data-copy="WDJB-MJHT" data-field="ghcode-cx-agent">Copy</button>', shown)


def agent_side_request(ask):
    return base.agent_side.request(agent_github.PM_ID, ask)


if __name__ == "__main__":
    unittest.main()
