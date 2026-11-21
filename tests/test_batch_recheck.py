"""The GPT re-check of the batch 5+6 review fixes (REVIEWS.md): each point adopted, as a regression.
Run: .venv/bin/python -m unittest tests.test_batch_recheck"""
import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import agent_removal
from server import github_link
from server import ownership
from server import pairing
from server import panel
from common import registry
from server import registry_removal
from server import remote_agent
from server import remote_people
from tests.test_github_link import ANNA, ANNA_ID
from tests.test_github_link_more import BindRaceCase
from tests.test_github_app import OTHER
from tests.test_remove_then_add import S, PruneCase, RemovalCase
from tests.test_more_agents_flows import BETTY, text

FAR = "@eve:other.example"


class PrivacyWritesWaitForTheRequester(BindRaceCase):
    """#1: a requester who left during an install wait still had @pm close and list the room."""
    def test_no_privacy_change_for_someone_who_left(self):
        self.lp.close_for_private = mock.AsyncMock(return_value=(None, ""))
        self.now["membership"] = "leave"
        asyncio.run(self.lp.bind_checked("!r", ANNA, OTHER, None, github_link.link_of(ANNA)))
        self.lp.close_for_private.assert_not_called()
        self.assertEqual(self.written, [])


class CodesAndOlderRecords(RemovalCase):
    def test_revoke_drops_every_code_in_memory_before_the_save_can_fail(self):
        """#2: a full disk stopped revocation after the first matching code."""
        codes = pairing.Codes(Path(tempfile.mkdtemp()) / "pairing.json")
        a, b = codes.issue(BETTY), codes.issue(BETTY, agent="betty-codex-agent")
        dict.__setitem__(codes.codes, "extra", {"person": BETTY, "expires": 1e12})       # a second first-agent code
        with mock.patch.object(codes.codes, "_save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            codes.revoke(BETTY)
        self.assertNotIn("extra", codes.codes)
        self.assertIsNone(codes.redeem(a))
        self.assertEqual(codes.redeem_agent(b)[0], BETTY)                                # another agent's: kept

    def test_an_older_record_closed_but_unsaved_can_be_removed_on_retry(self):
        """#3: no "ready", its password signed in, closed, then agents.json unwritable; the retry can't
        sign in any more and Synapse's "it exists" made it refuse for ever."""
        registry.update(BETTY, None, ready=False)
        with mock.patch.object(agent_removal, "_signs_in", return_value=True), \
                mock.patch.object(registry, "save", side_effect=OSError("disk full")), \
                self.assertRaises(agent_removal.NotForgotten):
            agent_removal.remove(BETTY, "betty-agent")
        with mock.patch.object(agent_removal, "_signs_in", return_value=False), \
                mock.patch.object(agent_removal.local_signup, "admin_call", return_value={"name": "x", "deactivated": True}):
            self.assertTrue(agent_removal.remove(BETTY, "betty-agent"))
        self.assertEqual(registry.agents_of(BETTY), [])
        with mock.patch.object(agent_removal.local_signup, "admin_call", return_value={"deactivated": False}):
            self.assertEqual(agent_removal._account("@x:y"), agent_removal.LIVE)

    def test_after_the_first_is_removed_the_default_agent_is_a_live_one(self):
        """#6: planning and the local write check still named the closed betty-agent."""
        self.run_(self.lp.remove_for_good("!dm", BETTY, "betty-agent"))
        self.assertEqual(registry.agent_of(BETTY), "betty-agent")                        # none yet: as before
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(BETTY, "bring my own")))
        self.run_(self.lp.on_add_text(SimpleNamespace(room_id="!dm"), text(BETTY, "codex")))
        self.assertEqual(ownership.own_agent_id(BETTY), f"@betty-codex-agent:{S}")
        self.env["PW_BETTY_CODEX_AGENT"] = "pw2"
        self.assertIsNone(remote_agent.credentials_for(BETTY, None))                    # an old first-agent code
        self.assertEqual(remote_agent.credentials_for(BETTY, "betty-codex-agent")["user_id"], f"@betty-codex-agent:{S}")


class HourlyCapRemembersRemovedAgents(unittest.TestCase):
    """#4: removing a named agent dropped its "created", so add-remove-add made more accounts an hour."""
    def test_a_removed_agents_creation_still_counts(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({FAR: {"agent": "remote", "created": 100.0,
                                          "more": [{"label": "codex", "agent": "remote", "created": 1e12 - 5}]}}))
        with mock.patch.object(registry, "PATH", path), mock.patch("time.time", return_value=1e12):
            registry_removal.forget_agent(FAR, registry.find(FAR, "codex")["localpart"])
            self.assertIn(1e12 - 5, remote_people.created_times(registry.from_elsewhere(registry.load())))
            registry_removal.forget_agent(FAR, registry.find(FAR, None)["localpart"])     # the tombstone keeps it too
            self.assertIn(1e12 - 5, remote_people.created_times(registry.from_elsewhere(registry.load())))
            with mock.patch.object(remote_people, "per_hour", return_value=1), \
                    self.assertRaises(remote_people.AccountError):
                remote_people.check_new(registry.from_elsewhere(registry.load()), FAR, at=1e12, further=True)


class UnsavedUnlinkIsRetried(PruneCase):
    """#5: a failed unlink put the proof back, and a reactivation before the next scan kept it."""
    def test_out_of_memory_at_once_and_retried_after_a_reactivation(self):
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        with mock.patch.object(github_link.links(), "_save", side_effect=OSError("disk full")), \
                self.assertLogs("loop", "WARNING"):
            asyncio.run(self.lp.prune_registry(now=10**9))
        self.assertIsNone(github_link.link_of(ANNA))                                     # fails closed now
        self.assertIn(ANNA, json.loads(github_link.LINKS_FILE.read_text()))
        self.dead = set()                                                                # reactivated meanwhile
        with self.assertLogs("loop", "WARNING"):
            asyncio.run(self.lp.prune_registry(now=10**9 + 11 * 60))                     # soon, not in 6 h
        self.assertNotIn(ANNA, json.loads(github_link.LINKS_FILE.read_text()))


@unittest.skipUnless(shutil.which("node"), "node isn't installed")
class PanelWithoutAgents(unittest.TestCase):
    """#7: after removing the last agent, "Add an agent" was hidden; without panel-agents.js every click threw."""
    def node(self, script):
        done = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return done.stdout

    def test_the_add_form_shows_with_no_agents(self):
        esc = "const esc = t => String(t); const cap = t => t;"
        out = self.node(f"{esc}\n{panel.SCRIPTS['panel-agents.js'].read_text()}\n"
                        "process.stdout.write(agentCards({agents: [], can_add: true}) + '|' + agentCards({agents: [], can_add: false}));")
        shown, hidden = out.split("|")
        self.assertIn('data-a="add"', shown)
        self.assertEqual(hidden, "")

    def test_clicks_work_without_the_agents_script_and_remove_is_refused(self):
        ok = re.search(r"^const ok = .*?; };$", panel.PAGE, re.S | re.M).group(0)
        out = self.node("const window = {};\n" + ok + "\nprocess.stdout.write(String(ok({dataset: {a: 'mode'}})) + "
                        "String(ok({dataset: {a: 'remove'}})));")
        self.assertEqual(out, "truefalse")


if __name__ == "__main__":
    unittest.main()
