"""Unit tests: removing an agent for good, and forgetting deactivated accounts (agent_removal.py,
pm_remove.py, registry_removal.forget_agent/prune, console "remove"). Run: .venv/bin/python -m unittest tests.test_remove_agent"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import agent_removal
from server import console
from common import matrix_calls
from common import registry
from server import registry_removal
from server import remote_agent
from common import setup_accounts
from server.loop import Loop

S = "bridge.localhost"
CX, BETTY, PM = f"@cx:{S}", f"@betty:{S}", f"@pm:{S}"
CXA, CXC = f"@cx-agent:{S}", f"@cx-codex-agent:{S}"
PEOPLE = {CX: {"agent": "remote", "ready": True, "more": [{"label": "codex", "agent": "remote", "ready": True}]},
          BETTY: {"agent": "remote", "ready": True}, "anna": {"agent": "hermes"}}


def use_registry(case, data):
    path = Path(tempfile.mkdtemp()) / "agents.json"
    path.write_text(json.dumps(data))
    p = mock.patch.object(registry, "PATH", path)
    p.start()
    case.addCleanup(p.stop)


def click(sender, key, to):
    return SimpleNamespace(sender=sender, key=key, reacts_to=to, server_timestamp=10**13)


class Registry(unittest.TestCase):
    def setUp(self):
        use_registry(self, PEOPLE)

    def test_a_further_agent_frees_its_slot_and_its_name_can_be_used_again(self):
        """Owner request: remove "codex", add "codex" again. Its closed account never comes back: the
        new one gets a new account name, and keeps the name its person gave it."""
        registry_removal.forget_agent(CX, "cx-codex-agent")
        self.assertEqual([a["localpart"] for a in registry.agents_of(CX)], ["cx-agent"])
        again = registry.add_agent(CX, "codex")
        self.assertEqual((again["localpart"], again["label"], registry.name_of(again)), ("cx-codex2-agent", "codex2", "codex"))
        self.assertEqual(registry.find(CX, "codex")["localpart"], "cx-codex2-agent")   # "remove my agent codex" finds it
        with self.assertRaisesRegex(ValueError, "already have an agent called codex"):
            registry.add_agent(CX, "codex")                    # unique among their agents now
        self.assertEqual(registry.add_agent(CX, "laptop")["localpart"], "cx-laptop-agent")

    def test_the_first_agent_goes_last_with_its_line(self):
        with self.assertRaisesRegex(ValueError, "other agents first"):
            registry_removal.forget_agent(CX, "cx-agent")
        registry_removal.forget_agent(BETTY, "betty-agent")
        self.assertEqual(registry.agents_of(BETTY), [])          # her line stays: betty-agent can't come back
        self.assertTrue(registry.first_removed(BETTY))
        self.assertIn(CX, registry.load())

    def test_prune_forgets_only_people_whose_every_account_is_deactivated(self):
        """The 9 pubtest accounts were deactivated on the live server but stayed in agents.json."""
        use_registry(self, {**PEOPLE, "pubtest1": {"agent": "remote"}, "@eve:other.example": {"agent": "remote"}})
        eve = registry.agents_of("@eve:other.example")[0]["localpart"]
        dead = {f"@pubtest1:{S}", f"@pubtest1-agent:{S}", f"@{eve}:{S}",
                CXC, f"@betty-agent:{S}"}                      # betty herself and cx-agent are live
        self.assertEqual(sorted(registry_removal.prune(dead)), ["@eve:other.example", f"@pubtest1:{S}"])
        left = registry.load()
        self.assertEqual(sorted(left), sorted([CX, BETTY, f"@anna:{S}", "@eve:other.example"]))   # never a live person
        self.assertEqual(registry.agents_of("@eve:other.example"), [])   # eve's own account may be live: she may add one
        self.assertEqual(len(registry.agents_of(CX)), 2)
        self.assertEqual(registry_removal.prune(set()), [])


class Remove(unittest.TestCase):
    def setUp(self):
        use_registry(self, PEOPLE)
        self.env = {"PW_CX_CODEX_AGENT": "pw1", "PW_OTHER": "keep"}
        self.calls = []

        def admin(method, path, body):
            self.calls.append((method, path, body))
            return self.answer
        self.answer = {}
        for target, fn in ((agent_removal.local_signup, ("admin_call", admin)),
                           (agent_removal, ("load_env", lambda: dict(self.env))),
                           (agent_removal.setup_accounts, ("save_env", lambda env: setattr(self, "env", env))),
                           (agent_removal.setup_accounts, ("password_key", lambda a: "PW_" + a.upper().replace("-", "_")))):
            p = mock.patch.object(target, *fn)
            p.start()
            self.addCleanup(p.stop)

    def test_it_is_deactivated_its_password_dropped_and_its_slot_freed(self):
        self.assertTrue(agent_removal.remove(CX, "codex"))
        self.assertEqual(self.calls, [("POST", "/_synapse/admin/v1/deactivate/%40cx-codex-agent%3Abridge.localhost",
                                       {"erase": False})])
        self.assertEqual(self.env, {"PW_OTHER": "keep"})
        self.assertEqual(len(registry.agents_of(CX)), 1)

    def test_a_refusal_changes_nothing(self):
        self.answer = {"_status": 403, "errcode": "M_FORBIDDEN"}
        with self.assertRaisesRegex(setup_accounts.AccountError, "M_FORBIDDEN"):
            agent_removal.remove(CX, "codex")
        self.assertEqual(len(registry.agents_of(CX)), 2)
        self.assertIn("PW_CX_CODEX_AGENT", self.env)

    def test_never_someone_elses_or_a_server_run_agent(self):
        for who, which in ((BETTY, "cx-codex-agent"), ("anna", "anna-agent"), (CX, "cx-agent")):
            with self.assertRaises(setup_accounts.AccountError):
                agent_removal.remove(who, which)               # cx-agent: the first goes last
        self.assertEqual(self.calls, [])

    def test_an_account_we_never_held_is_only_forgotten(self):
        use_registry(self, {CX: {"agent": "remote", "more": [{"label": "codex", "agent": "remote"}]}})
        self.env = {}
        self.assertFalse(agent_removal.remove(CX, "codex"))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(registry.agents_of(CX)), 1)

    def test_gpt_review_never_on_a_stale_name_or_an_unconfirmed_password(self):
        """GPT review: a stale agent_user, or a login that failed for any reason, must change nothing."""
        use_registry(self, {BETTY: {"agent": "remote", "ready": True, "agent_user": "anna"}})
        with self.assertRaisesRegex(setup_accounts.AccountError, "odd record"):
            agent_removal.remove(BETTY, "anna")
        use_registry(self, {CX: {"agent": "remote", "more": [{"label": "codex", "agent": "remote"}]}})
        with mock.patch.object(agent_removal, "_signs_in", return_value=False), \
                self.assertRaisesRegex(setup_accounts.AccountError, "couldn't confirm"):
            agent_removal.remove(CX, "codex")                  # e.g. Synapse answered 500 to the login
        self.assertEqual(len(registry.agents_of(CX)), 2)
        self.assertEqual([c[0] for c in self.calls], ["GET"])    # only asked whether it exists: never closed
        self.assertIn("PW_CX_CODEX_AGENT", self.env)

    def test_gpt_recheck_a_set_up_whose_registration_failed_can_still_be_removed(self):
        use_registry(self, {CX: {"agent": "remote", "more": [{"label": "codex", "agent": "remote"}]}})
        self.answer = {"_status": 404, "errcode": "M_NOT_FOUND"}  # Synapse: no such account
        with mock.patch.object(agent_removal, "_signs_in", return_value=False):
            self.assertFalse(agent_removal.remove(CX, "codex"))
        self.assertEqual([c[0] for c in self.calls], ["GET"])    # no deactivation for an account that isn't there
        self.assertEqual((len(registry.agents_of(CX)), self.env), (1, {"PW_OTHER": "keep"}))

    def test_gpt_review_a_leftover_password_is_not_a_failure_and_the_last_removal_stays_empty(self):
        use_registry(self, {BETTY: {"agent": "remote", "ready": True}})
        self.env = {"PW_BETTY_AGENT": "pw"}
        with mock.patch.object(agent_removal.setup_accounts, "save_env", side_effect=OSError("disk full")), \
                self.assertLogs("loop", "ERROR"):
            self.assertTrue(agent_removal.remove(BETTY, "betty-agent"))
        self.assertEqual(list(registry.load()), [BETTY])      # never the demo anna and betty again
        self.assertEqual(registry.agents_of(BETTY), [])        # only the names that can't come back

    def test_gpt_review_prune_waits_for_removals_and_set_ups(self):
        held = []
        with mock.patch.object(registry_removal, "prune", lambda dead: held.append(agent_removal.WRITING.locked()) or []):
            agent_removal.prune(set())
        self.assertEqual(held, [True])

    def test_the_deactivated_list_is_read_in_full_or_not_at_all(self):
        pages = iter([{"users": [{"name": "@a:x", "deactivated": True}, {"name": "@b:x", "deactivated": False}],
                       "next_token": "500"}, {"users": [{"name": "@c:x", "deactivated": True}]}])
        with mock.patch.object(agent_removal.local_signup, "admin_call", lambda *a: next(pages)):
            self.assertEqual(agent_removal.deactivated(), {"@a:x", "@c:x"})
        for broken in ({"_status": 403, "errcode": "M_FORBIDDEN"}, {"users": "nope"}):
            with mock.patch.object(agent_removal.local_signup, "admin_call", lambda *a, b=broken: b), \
                    self.assertLogs("loop", "WARNING"):
                self.assertIsNone(agent_removal.deactivated())
        with mock.patch.object(agent_removal.local_signup, "admin_call", side_effect=OSError("down")), \
                self.assertLogs("loop", "WARNING"):
            self.assertIsNone(agent_removal.deactivated())


class InChat(unittest.TestCase):
    def setUp(self):
        use_registry(self, PEOPLE)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp._seen, self.said, self.buttons = PM, {"cx-codex-agent": {"online": False}}, [], []

        async def say(room_id, body, *a, **k):
            self.said.append(body)
            return f"$m{len(self.said)}"

        async def thumb(room_id, event_id, key="👍"):
            self.buttons.append((event_id, key))
        self.lp.say, self.lp.offer_thumb = say, thumb
        self.lp.private_room = mock.AsyncMock(return_value="!dm")
        for target, name in ((agent_removal, "remove"), (remote_agent.CODES, "revoke")):
            p = mock.patch.object(target, name)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)

    def test_remove_asks_then_only_the_owners_check_removes(self):
        self.lp._welcomes = {}
        self.lp.is_one_to_one = lambda room, who: True
        asyncio.run(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), SimpleNamespace(
            sender=CX, body="remove my agent codex", source={"content": {"body": "remove my agent codex"}})))
        self.assertIn("Remove **cx-codex-agent** for good?", self.said[-1])
        question = self.buttons[-1][0]
        self.assertEqual(self.buttons[-1][1], "✅")
        self.assertFalse(asyncio.run(self.lp.on_removal_reaction(SimpleNamespace(room_id="!dm"), click(BETTY, "✅", question))))
        self.assertTrue(asyncio.run(self.lp.on_removal_reaction(SimpleNamespace(room_id="!dm"), click(CX, "👀", question))))
        self.remove.assert_not_called()
        self.assertTrue(asyncio.run(self.lp.on_removal_reaction(SimpleNamespace(room_id="!dm"), click(CX, "✅", question))))
        self.remove.assert_called_once_with(CX, "cx-codex-agent")
        self.revoke.assert_called_once_with(CX, "cx-codex-agent")
        self.assertIn("gone for good", self.said[-1])
        self.assertNotIn("cx-codex-agent", self.lp._seen)       # never an "offline" note for it again

    def test_gpt_review_unanswered_questions_dont_pile_up(self):
        asyncio.run(self.lp.ask_removal("!dm", CX, "codex"))
        asyncio.run(self.lp.ask_removal("!dm", CX, "codex"))
        self.assertEqual(len(self.lp.removals()), 1)           # the newer question replaces the older
        next(iter(self.lp.removals().values()))["until"] = 0
        asyncio.run(self.lp.ask_removal("!dm", BETTY, None))
        self.assertEqual([a["person"] for a in self.lp.removals().values()], [BETTY])   # expired: evicted

    def test_gpt_recheck_a_refused_removal_keeps_its_codes(self):
        self.remove.side_effect = setup_accounts.AccountError("I couldn't confirm it")
        self.assertFalse(asyncio.run(self.lp.remove_for_good("!dm", CX, "codex")))
        self.revoke.assert_not_called()
        self.assertIn("couldn't remove it", self.said[-1])

    def test_which_one_and_first_goes_last(self):
        asyncio.run(self.lp.ask_removal("!dm", CX, None))
        self.assertIn("Which one?", self.said[-1])
        asyncio.run(self.lp.ask_removal("!dm", CX, "main"))
        self.assertIn("Remove your other agents first (codex)", self.said[-1])
        self.assertEqual(self.buttons, [])

    def test_the_panel_needs_its_second_click(self):
        room = SimpleNamespace(users={CX: 0}, invited_users={})
        self.lp.client = SimpleNamespace(rooms={"!r": room})
        self.lp.is_person = lambda r, u: True
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "remove", "", "codex")), "unknown action")
        self.remove.assert_not_called()
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "remove", "sure", "codex")), "ok")
        self.remove.assert_called_once_with(CX, "cx-codex-agent")

    def test_presence_skips_deactivated_agents_and_pruning_is_logged(self):
        """Synapse answers a deactivated account's presence without "presence": nio logged an error."""
        use_registry(self, {**PEOPLE, "pubtest1": {"agent": "remote"}})
        dead = {f"@pubtest1:{S}", f"@pubtest1-agent:{S}", CXC}
        presence, self.lp.client = mock.AsyncMock(return_value=True), mock.Mock()
        with mock.patch.object(agent_removal, "deactivated", return_value=dead), self.assertLogs("loop", "WARNING") as logs, \
                mock.patch.object(matrix_calls, "presence", presence):
            asyncio.run(self.lp.check_agents(now=10**6))
        self.assertIn(f"forgot @pubtest1:{S}", "\n".join(logs.output))
        asked = [c.args[1] for c in presence.call_args_list]
        self.assertEqual(sorted(asked), sorted([CXA, f"@betty-agent:{S}"]))   # not cx-codex-agent, not pubtest1's


if __name__ == "__main__":
    unittest.main()
