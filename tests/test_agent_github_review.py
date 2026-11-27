"""GPT review of the panel's "Sign in to GitHub", the agent's side (adapters/agents/agent_github.py,
adapters/agents/agent_update.py; docs/adr/0021-agent-github-signin.md): a stop while gh's pipe was full
hung, a revoked token still counted as signed in, and a click sent while the agent started was missed.
Run: .venv/bin/python -m unittest tests.test_agent_github_review"""
import asyncio
import json
import os
import unittest
from unittest import mock

from nio import LoginResponse

from adapters.agents import agent_github
from adapters.agents import agent_update
from adapters.agents import bridge
from common import github_login
from tests import test_console_github as pm_side
from tests.test_agent_github import AGENT, Base, FakeBridge, alive, now, request


class Cleanup(Base):
    def test_a_stop_while_gh_fills_its_pipe_still_ends_at_once(self):
        """gh printed more than a pipe holds while the agent was busy sending its code: killing it then
        waited forever for a pipe nobody read, and the next click (or an Update) waited on that."""
        b = FakeBridge()

        async def slow(message, tx):                     # @pm is slow to take the code: nobody reads gh meanwhile
            await asyncio.sleep(60)
        b.client.to_device = slow
        os.environ["FAKE_GH_MODE"] = "flood"

        async def go():
            with mock.patch.object(agent_github, "SIGNIN_S", 30):
                await b.on_github_request(request(agent_github.PM_ID))
                for _ in range(200):
                    if self.log.exists():
                        break
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.5)                 # its pipe is full now
                pid = json.loads(self.log.read_text())["pid"]
                await asyncio.wait_for(b.stop_github(), 10)
                self.assertFalse(alive(pid))
        asyncio.run(go())



class TokenCheck(Base):
    """A hosts.yml with a username but a revoked or missing token counted as signed in: the panel said so and
    refused every click, for good."""
    def signed_in_folder(self):
        (self.root / AGENT).mkdir(parents=True)
        (self.root / AGENT / "hosts.yml").write_text("github.com:\n    user: octo-cat\n")

    def test_a_token_github_refuses_is_not_signed_in_and_a_click_signs_in_again(self):
        self.signed_in_folder()
        for refused in ("revoked", "none"):
            with mock.patch.dict(os.environ, {"FAKE_GH_API": refused}):
                self.assertEqual(now(AGENT), {"state": "none"}, refused)
        with mock.patch.dict(os.environ, {"FAKE_GH_API": "revoked"}):
            b = self.click()
        self.assertEqual(self.answers(b), [{"id": "k1", "code": "WDJB-MJHT"}, {"id": "k1", "result": "signed_in", "login": "octo-cat"}])
        self.assertTrue(self.log.exists())                  # gh's sign-in ran: not "already"
        self.assertNotIn("Bad credentials", json.dumps(b.sent) + self.logs)   # gh's words: never sent, never logged

    def test_a_good_token_is_already_signed_in_and_checked_in_its_own_folder_only(self):
        self.signed_in_folder()
        seen = self.root.parent / "api.json"
        with mock.patch.dict(os.environ, {"FAKE_GH_API_LOG": str(seen)}):
            self.assertEqual(now(AGENT), {"state": "signed_in", "login": "octo-cat"})
            b = self.click()
        self.assertEqual(self.answers(b), [{"id": "k1", "result": "already", "login": "octo-cat"}])
        given = json.loads(seen.read_text())
        self.assertEqual(given["args"], agent_github.PROBE)
        self.assertEqual(given["env"], {"GH_CONFIG_DIR": str(self.root / AGENT), "GH_TOKEN": None, "GITHUB_TOKEN": None})

    def test_no_answer_from_github_keeps_what_its_folder_says(self):
        self.signed_in_folder()
        with mock.patch.dict(os.environ, {"FAKE_GH_API": "down"}):
            self.assertEqual(now(AGENT), {"state": "signed_in", "login": "octo-cat"})
        with mock.patch.dict(os.environ, {"FAKE_GH_API": "hang"}), mock.patch.object(agent_github, "PROBE_S", 0.5):
            self.assertEqual(now(AGENT), {"state": "signed_in", "login": "octo-cat"})   # bounded, not 60 seconds
            b = self.click()                                # a click still signs in: an unchecked token isn't "already"
        self.assertEqual(self.answers(b)[-1], {"id": "k1", "result": "signed_in", "login": "octo-cat"})


class FirstSync(Base):
    """A click that reached the agent in its first sync was skipped: its callbacks came after it."""
    def test_a_click_in_the_first_sync_is_taken_and_its_start_report_names_it(self):
        b = bridge.Bridge("cx", "codex")
        mine = FakeBridge()
        b.client.to_device = mine.client.to_device
        os.environ["FAKE_GH_MODE"] = "hang"

        async def first_sync(client, *args):                # the click comes in it, to the callbacks there are
            for cb in client.to_device_callbacks:
                await cb.func(request(agent_github.PM_ID, "k1"))

        async def go():
            with mock.patch.object(bridge, "agent_password", return_value="pw"), \
                    mock.patch.object(bridge.lifecycle, "login", mock.AsyncMock(return_value=LoginResponse(b.user_id, "D", "t"))), \
                    mock.patch.object(bridge.lifecycle, "first_sync", first_sync), \
                    mock.patch.object(agent_update.kind_report, "send", mock.AsyncMock()), \
                    mock.patch.object(agent_update.code_report, "send", mock.AsyncMock()), \
                    mock.patch.object(bridge.Bridge, "serve_until_updated", mock.AsyncMock()), \
                    mock.patch.object(agent_github, "SIGNIN_S", 1):
                await b.run()
                self.assertIsNotNone(b.__dict__.get("_gh_job"))
                await b._gh_job
        asyncio.run(go())
        states = [c for t, _, c in mine.sent if t == github_login.STATE_TYPE]
        self.assertEqual(states[0], {"state": "none", "startup": True, "id": "k1"})   # its start, already on k1
        self.assertEqual(states[-1], {"state": "none", "id": "k1"})                   # after k1


class PmReadsTheReport(pm_side.Flow):
    """@pm took every state report for a restart: a late one from an old click ended a newer click."""
    def report(self, content):
        self.deliver([(github_login.STATE_TYPE, None, content)])
        return self.row()["gh_busy"]

    def test_only_a_start_ends_a_waiting_click_and_not_the_one_it_took_up(self):
        self.start()
        self.click()
        ask = self.lp._gh_signins["cx-agent"]["id"]
        self.assertTrue(self.report({"state": "none", "id": "an-older-click"}))       # after a sign-in: ends nothing
        self.assertTrue(self.report({"state": "none", "startup": True, "id": ask}))   # its first sync took this one
        self.assertFalse(self.report({"state": "none", "startup": True}))             # a start: the dead click goes
        self.lp._gh_signins.clear()
        self.click()
        self.assertFalse(self.report({"state": "none"}))    # an agent from before "startup": still a start

    def test_pm_keeps_only_known_fields(self):
        self.assertEqual(github_login.state({"state": "none", "startup": True, "id": "k1"}), {"state": "none", "login": None})
        for bad in ({"state": "none", "startup": False}, {"state": "none", "startup": "yes"}, {"state": "none", "id": 5},
                    {"state": "none", "id": "x" * 65}):
            self.assertIsNone(github_login.state(bad), bad)


if __name__ == "__main__":
    unittest.main()
