"""Removing an agent, then what happens next (GPT review, batch 5+6): a person whose last agent was
removed gets a new one with a new name; pruning and registry write failures (agent_removal.py,
pm_remove.py, registry.py). Run: .venv/bin/python -m unittest tests.test_remove_then_add"""
import asyncio
from types import SimpleNamespace
from unittest import mock

from server import agent_removal
from server import console
from server import github_link
from common import registry
from server import remote_agent
from common import setup_accounts
from client import supervise
from tests.test_github_link import ANNA, ANNA_ID, LinkCase
from tests.test_more_agents_flows import BETTY, Base, text
from tests.test_remove_agent import use_registry

S = "bridge.localhost"


class RemovalCase(Base):
    """Betty's agents on her own computer; Synapse, .env and the pairing codes faked."""
    people = {BETTY: {"agent": "remote", "ready": True, "dm": "!b"}}

    def setUp(self):
        super().setUp()
        self.lp.dm_rooms = {BETTY: "!dm"}
        self.lp.is_one_to_one = lambda room, who: True
        self.env, self.registered, self.codes = {"PW_BETTY_AGENT": "pw"}, [], []

        def ensure_user(env, agent, secret, verify=False):
            self.registered.append(agent)
            return "pw2", True
        for target, name, value in (
                (agent_removal.local_signup, "admin_call", lambda *a: {}),       # Synapse: deactivated
                (agent_removal, "load_env", lambda: dict(self.env)),
                (remote_agent, "load_env", lambda: dict(self.env)),
                (setup_accounts, "save_env", lambda env: setattr(self, "env", env)),
                (setup_accounts, "password_key", lambda a: "PW_" + a.upper().replace("-", "_")),
                (setup_accounts, "ensure_user", ensure_user), (setup_accounts, "shared_secret", lambda: "s"),
                (remote_agent, "open_dm", lambda agent, pw, person: "!agent-dm"),
                (remote_agent.CODES, "revoke", lambda *a: None),
                (remote_agent.CODES, "issue", lambda person, agent=None: self.codes.append(agent) or "AAAAA-BBBBB")):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)



class RemoveLastThenAdd(RemovalCase):
    """#3: removing the first (last) agent deleted the line, and "bring my own" then tried to
    register betty-agent again, which Synapse never allows for a deactivated name."""

    def say_(self, body):
        return self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(BETTY, body)))

    def test_after_removing_her_only_agent_she_gets_a_new_one_with_a_new_name(self):
        self.assertTrue(self.run_(self.lp.remove_for_good("!dm", BETTY, "betty-agent")))
        self.assertIn("gone for good", self.said[-1][1])
        self.say_("bring my own")                                   # what the success message leads to
        self.assertIn("What should I call your new agent?", self.said[-1][1])
        self.run_(self.lp.on_add_text(SimpleNamespace(room_id="!dm"), text(BETTY, "codex")))
        self.assertEqual(self.registered, ["betty-codex-agent"])    # never the closed betty-agent
        self.assertEqual(self.codes, ["betty-codex-agent"])
        self.assertIn("Your agent is **betty-codex-agent**", self.said[-1][1])
        self.assertEqual([a["localpart"] for a in registry.agents_of(BETTY)], ["betty-codex-agent"])

    def test_the_closed_name_is_never_registered_again_and_nothing_runs_it_here(self):
        self.run_(self.lp.remove_for_good("!dm", BETTY, "betty-agent"))
        with self.assertRaisesRegex(setup_accounts.AccountError, "add another agent"):
            remote_agent.provision(BETTY)
        self.assertEqual(self.registered, [])
        self.assertEqual([k.name for k in supervise.children("codex", False)], ["pm"])   # no server-run betty-agent
        room = SimpleNamespace(users={BETTY: 0}, invited_users={})
        self.lp.client = SimpleNamespace(rooms={"!r": room})
        self.lp.agent_online = lambda agent: False
        self.assertTrue(registry.may_add_more(BETTY))
        mine = console.my_agents(self.lp, room, "!r", BETTY)
        self.assertEqual(mine, [])
        self.assertTrue(console.can_add(BETTY, mine))               # the panel's "Add an agent" stays


class PruneCase(LinkCase):
    """Anna (deactivated, as Synapse's list says: self.dead) and Betty, both with agents here."""

    def setUp(self):
        super().setUp()
        use_registry(self, {ANNA: {"agent": "remote", "ready": True}, BETTY: {"agent": "remote", "ready": True}})
        self.dead = {ANNA, f"@anna-agent:{S}"}
        p = mock.patch.object(agent_removal, "deactivated", lambda: self.dead)
        p.start()
        self.addCleanup(p.stop)



class PruneDropsGitHub(PruneCase):
    """#4: pruning a deactivated person here left their GitHub link, and a sign-in still waiting could
    save one after; a reactivated account would then bind repos on that old proof."""

    def prune(self):
        with self.assertLogs("loop", "WARNING") as logs:
            asyncio.run(self.lp.prune_registry(now=10**9))
        return "\n".join(logs.output)

    def test_a_deactivated_persons_link_goes_and_others_stay(self):
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        github_link.save_link(BETTY, "betty-gh", 7)
        self.assertIn(f"dropped deactivated {ANNA}'s GitHub link", self.prune())
        self.assertIsNone(github_link.link_of(ANNA))
        self.assertEqual(github_link.link_of(BETTY)["login"], "betty-gh")       # live: untouched
        self.assertNotIn(ANNA, registry.load())

    def test_a_sign_in_waiting_when_pruned_never_saves_a_link(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 50)

        async def flow():
            await self.lp.connect_github(ANNA)
            task = github_link.FLOWS[ANNA]["task"]
            with self.assertLogs("loop", "WARNING"):
                await self.lp.prune_registry(now=10**9)
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(flow())
        self.assertIsNone(github_link.link_of(ANNA))
        self.assertIsNone(github_link.status(ANNA)["code"])

    def test_an_approval_landing_as_the_prune_runs_is_not_saved(self):
        async def approved_meanwhile(flow, sleep=None):
            self.lp.forget_github_of(self.dead)                                  # the prune, as GitHub answers
            return "anna-gh", ANNA_ID
        with mock.patch.object(github_link, "wait_for_login", approved_meanwhile), self.assertLogs("loop", "WARNING"):
            async def flow():
                await self.lp.connect_github(ANNA)
                with self.assertRaises(asyncio.CancelledError):
                    await github_link.FLOWS.get(ANNA, {}).get("task") or asyncio.sleep(0)
            asyncio.run(flow())
        self.assertIsNone(github_link.link_of(ANNA))

    def test_an_active_person_removing_an_agent_keeps_their_link(self):
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        self.lp._seen = {}                                                       # pm_presence.py, in memory here
        with mock.patch.object(agent_removal, "remove", return_value=True), \
                mock.patch.object(remote_agent.CODES, "revoke"):
            self.assertTrue(asyncio.run(self.lp.remove_for_good("!dm", ANNA, "anna-agent")))
        self.assertEqual(github_link.link_of(ANNA)["login"], "anna-gh")


class SaveFails(RemovalCase):
    """#5: agents.json unwritable (a full disk) after the account was closed, and during pruning."""
    people = {BETTY: {"agent": "remote", "ready": True, "dm": "!b", "more": [{"label": "codex", "agent": "remote", "ready": True}]}}

    def setUp(self):
        super().setUp()
        self.revoked, self.closed = [], []
        for target, name, value in ((remote_agent.CODES, "revoke", lambda *a: self.revoked.append(a)),
                                    (agent_removal.local_signup, "admin_call", lambda *a: self.closed.append(a[1]) or {})):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_closed_account_is_reported_closed_and_its_codes_revoked(self):
        with mock.patch.object(registry, "save", side_effect=OSError("disk full")), self.assertLogs("loop", "ERROR"):
            self.assertTrue(self.run_(self.lp.remove_for_good("!dm", BETTY, "codex")))
        self.assertEqual(len(self.closed), 1)                                    # deactivated
        self.assertEqual(self.revoked, [(BETTY, "betty-codex-agent")])
        self.assertIn("account is closed for good, but I couldn't update my records", self.said[-1][1])
        self.assertNotIn("try again;", self.said[-1][1])
        self.assertTrue(self.run_(self.lp.remove_for_good("!dm", BETTY, "codex")))   # later: frees its slot
        self.assertEqual([a["label"] for a in registry.agents_of(BETTY)], ["main"])

    def test_nothing_closed_and_unsaved_is_a_plain_retry(self):
        registry.update(BETTY, "codex", ready=False)
        with mock.patch.object(agent_removal, "_account", return_value=agent_removal.MISSING), \
                mock.patch.object(registry, "save", side_effect=OSError("disk full")), self.assertLogs("loop", "ERROR"):
            self.assertFalse(self.run_(self.lp.remove_for_good("!dm", BETTY, "codex")))
        self.assertEqual((self.closed, self.revoked), ([], []))
        self.assertIn("please try again", self.said[-1][1])

    def test_a_prune_that_cant_save_is_logged_and_the_poll_goes_on(self):
        dead = {f"@betty-agent:{S}", f"@betty-codex-agent:{S}", BETTY}
        with mock.patch.object(agent_removal, "deactivated", return_value=dead), \
                mock.patch.object(registry, "save", side_effect=OSError("disk full")), \
                self.assertLogs("loop", "ERROR") as logs:
            self.run_(self.lp.prune_registry(now=10**9))                         # never raises into the poll
        self.assertIn("couldn't save agents.json", "\n".join(logs.output))
        self.assertIn(BETTY, registry.load())
        with mock.patch.object(agent_removal, "deactivated", return_value=dead), self.assertLogs("loop", "WARNING"):
            self.run_(self.lp.prune_registry(now=10**9 + 11 * 60))               # tried again soon, not in 6 h
        self.assertNotIn(BETTY, registry.load())


if __name__ == "__main__":
    import unittest
    unittest.main()
