"""Renaming one's own agent (its display name; the Matrix ID stays), as a person does it: "Rename" in
the panel's My agents card (console.act) or "@pm call my agent NAME" / "rename my agent LABEL to NAME"
in the chat (pm_rename.py). Synapse's admin API is the fake here. Run: .venv/bin/python -m unittest tests.test_rename_agent"""
import asyncio
import contextlib
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import console
from server import pm_rename
from common import paths, registry
from common import setup_accounts
from server.loop import Loop
from tests.test_remove_agent import BETTY, CX, PEOPLE, PM, use_registry

ROOT = Path(__file__).resolve().parent.parent


class Rename(unittest.TestCase):
    def setUp(self):
        use_registry(self, PEOPLE)
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp._welcomes, self.lp.busy, self.said = PM, {}, set(), []

        async def say(room_id, body, *a, **k):
            self.said.append((room_id, body))
            return f"$m{len(self.said)}"
        self.lp.say = say
        self.lp.private_room = mock.AsyncMock(return_value="!dm")
        self.lp.is_one_to_one = lambda room, who: room.room_id == "!dm"
        room = SimpleNamespace(room_id="!r", users={CX: None, BETTY: None, PM: None}, invited_users={}, canonical_alias=None)
        self.lp.client = SimpleNamespace(rooms={"!r": room}, user_id=PM)
        self.calls = []
        self.answer = {}

        self.sessions = 0

        def admin_call(method, path, body):
            self.calls.append((method, path, body))
            return self.answer

        @contextlib.contextmanager
        def admin_session():
            self.sessions += 1                    # one admin sign-in per rename (GPT re-check)
            yield admin_call
        p = mock.patch("common.local_signup.admin_session", admin_session)
        p.start()
        self.addCleanup(p.stop)

    def chat(self, sender, words, room="!dm"):
        event = SimpleNamespace(sender=sender, body=words, source={"content": {"body": words}})
        async def go():
            done = await self.lp.on_welcome_text(SimpleNamespace(room_id=room, users={sender: None, PM: None}), event)
            await asyncio.gather(*self.lp.busy)          # answered in the background (GPT review)
            return done
        handled = asyncio.run(go())
        return handled, self.said[-1][1] if self.said else ""

    def test_the_owner_renames_from_the_panel(self):
        answer = asyncio.run(console.act(self.lp, "!r", CX, "rename", "  Codex the Builder ", "cx-codex-agent"))
        self.assertEqual(answer, "Your agent is now called Codex the Builder (its ID @cx-codex-agent:bridge.localhost stays the same).")
        self.assertEqual([c for c in self.calls if c[0] == "PUT"], [("PUT", "/_synapse/admin/v2/users/%40cx-codex-agent%3Abridge.localhost",
                                       {"displayname": "Codex the Builder"})])
        self.assertEqual(self.said[-1], ("!dm", answer))                   # also in our private chat
        self.assertEqual(self.sessions, 1)
        mine = console.my_agents(self.lp_with_presence(), self.lp.client.rooms["!r"], "!r", CX)
        self.assertEqual([a["display"] for a in mine], ["", "Codex the Builder"])   # after a restart too: agents.json

    def lp_with_presence(self):
        self.lp.agent_online = lambda agent: None
        self.lp.ever_connected = lambda agent: True
        return self.lp

    def test_the_owner_renames_in_the_chat_with_a_label(self):
        handled, answer = self.chat(CX, "rename my agent codex to Night Shift")
        self.assertTrue(handled)
        self.assertIn("now called Night Shift (its ID @cx-codex-agent:", answer)
        handled, answer = self.chat(CX, "rename my codex agent to Day Shift")
        self.assertIn("now called Day Shift", answer)

    def test_call_my_agent_with_one_agent(self):
        handled, answer = self.chat(BETTY, "call my agent \"Betty's helper\"")
        self.assertTrue(handled)
        self.assertEqual(answer, "Your agent is now called Betty's helper (its ID @betty-agent:bridge.localhost stays the same).")

    def test_several_agents_and_no_label_asks_which(self):
        handled, answer = self.chat(CX, "call my agent Bob")
        self.assertEqual(answer, 'Which one? Say "rename my agent LABEL to NAME" with one of: main, codex.')
        self.assertEqual(self.calls, [])

    def test_someone_elses_agent_is_refused_without_saying_whose(self):
        answer = asyncio.run(console.act(self.lp, "!r", BETTY, "rename", "Mine now", "cx-codex-agent"))
        self.assertEqual(answer, "You have no agent called cx\\-codex\\-agent.")
        handled, answer = self.chat(BETTY, "rename my agent codex to Mine")
        self.assertEqual(answer, "You have no agent called codex.")
        self.assertEqual(self.calls, [])

    def test_invalid_names_are_refused(self):
        for bad in ("", "   ", "x" * 41, "two\nlines", "bidi‮evil", "tab\there"):
            self.assertIsNone(pm_rename.clean_name(bad), repr(bad))
            self.assertEqual(asyncio.run(self.lp.rename_agent("!dm", BETTY, None, bad)), pm_rename.BAD_NAME)
        self.assertEqual(self.calls, [])
        self.assertEqual(pm_rename.clean_name(" “Ünïcode ok” "), "Ünïcode ok")

    def test_markdown_in_a_name_is_shown_as_text(self):
        answer = asyncio.run(self.lp.rename_agent("!dm", BETTY, None, "[click](https://x) **me**"))
        self.assertIn("\\[click\\]\\(https://x\\) \\*\\*me\\*\\*", answer)
        self.assertEqual(self.calls[-1][2], {"displayname": "[click](https://x) **me**"})   # the name itself as typed

    def test_a_refusal_from_synapse_says_so_and_changes_nothing(self):
        self.answer = {"_status": 500, "errcode": "M_UNKNOWN"}
        self.assertIn("couldn't rename it", asyncio.run(self.lp.rename_agent("!dm", BETTY, None, "Helper")))
        self.assertIsNone(registry.find(BETTY, None)["display"])

    def test_a_few_renames_per_hour(self):
        for i in range(pm_rename.RENAMES_PER_HOUR):
            self.assertIn("now called", asyncio.run(self.lp.rename_agent("!dm", BETTY, None, f"Name {i}")))
        self.assertIn("several renames this hour", asyncio.run(self.lp.rename_agent("!dm", BETTY, None, "Again")))

    def test_a_name_only_reserved_here_is_never_renamed(self):
        """GPT review: a set-up that never finished (not "ready") may name an account someone else made."""
        use_registry(self, {**PEOPLE, BETTY: {"agent": "remote", "ready": False}})
        self.assertEqual(asyncio.run(self.lp.rename_agent("!dm", BETTY, None, "Mine")), "You have no agent here yet.")
        self.assertEqual(self.calls, [])

    def test_a_missing_account_is_never_created_by_a_rename(self):
        self.answer = {"_status": 404, "errcode": "M_NOT_FOUND"}
        self.assertIn("couldn't rename it", asyncio.run(self.lp.rename_agent("!dm", BETTY, None, "Helper")))
        self.assertEqual([c[0] for c in self.calls], ["GET"])                  # update only: no PUT

    def test_a_name_another_of_their_agents_has_is_refused(self):
        """GPT review: one name, one agent, so "remove my agent codex" never picks the wrong one."""
        answer = asyncio.run(self.lp.rename_agent("!dm", CX, "main", "codex"))
        self.assertIn("already have an agent called codex", answer)
        self.assertEqual(self.calls, [])

    def test_nothing_sets_an_agents_name_back_on_a_restart(self):
        """Only @pm's own name is ever set at start (setup_accounts), and only this module and that one
        write display names at all: no bridge, connect script or set-up names an agent."""
        self.assertEqual(set(setup_accounts.DISPLAY_NAMES), {"pm"})
        writers = sorted(str(p.relative_to(ROOT)) for p in [*ROOT.glob("*.py"), *(p for d in paths.CODE_DIRS for p in (ROOT / d).rglob("*.py")), *ROOT.glob("*.sh"), *ROOT.glob("*.ps1"),
                                                              *ROOT.glob("static/*.js")]
                         if re.search(r"/displayname|\"displayname\":", p.read_text(encoding="utf-8")))
        self.assertEqual(writers, ["common/setup_accounts.py", "server/pm_rename.py"])


if __name__ == "__main__":
    unittest.main()
