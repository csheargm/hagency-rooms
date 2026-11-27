"""The panel's "Check readiness" for agents on people's computers (server/readiness_identity.py, github_setup.py,
console_repo.py; ADR 0021, 0023). Live test 2026-11-27: cx-agent and cx-codex2-agent, both signed in on the owner's
Mac (cx-codex2-agent had just opened a PR), were called "not signed in to GitHub", because the check ran `gh` on
the server under their names. Now each agent's own state report counts, and Write access is read as the App.
Run: .venv/bin/python -m unittest tests.test_readiness_check"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nio import MatrixRoom, RoomGetStateResponse, ToDeviceEvent

from common import github, github_app, github_login, registry, room_state, solo_review
from server import agent_removal, console, github_setup, readiness_identity
from server.loop import Loop

S = "bridge.localhost"
CX, PM = f"@cx:{S}", f"@pm:{S}"
CXA, CX2 = f"@cx-agent:{S}", f"@cx-codex2-agent:{S}"
REPO, LOGIN = "csheargm/hagency-demo", "csheargm"
GATED = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
PEOPLE = {CX: {"agent": "remote", "dm": "!dm", "more": [{"label": "codex2", "agent": "remote"}]}}


class FakeClient:
    """The room's state as the server keeps it (its repo, Solo review), and to-device reports in."""
    user_id = PM

    def __init__(self, room: MatrixRoom):
        self.rooms = {room.room_id: room}
        self.events = {github.LEVEL_EVENT: self.event(github.LEVEL_EVENT, room_state.content("autonomous", REPO))}

    @staticmethod
    def event(kind: str, content: dict) -> dict:
        return {"type": kind, "state_key": "", "sender": PM, "content": content}

    def solo(self, on: bool, ok: bool = True, repo: str = REPO) -> None:
        """ok: the owner's agent let repo admins past the review rule (pm_solo_rules)."""
        self.events[solo_review.SOLO_EVENT] = self.event(solo_review.SOLO_EVENT, solo_review.content(on, repo=repo, ok=ok))

    async def room_get_state(self, rid):
        return RoomGetStateResponse(list(self.events.values()), rid)


class Flow(unittest.TestCase):
    """The real path: the agents' state reports reach @pm, then the room owner clicks Check readiness."""
    def setUp(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps(PEOPLE))
        self.github = {"labels": [{"name": github.LABEL}], "rules": GATED, "permission": (200, {"permission": "admin"})}
        self._patch(registry, "PATH", path)
        self._patch(agent_removal, "deactivated", mock.Mock(return_value=set()))   # Synapse's list: nobody
        self._patch(github_app, "configured", lambda: True)
        self._patch(github_app, "as_app", self.as_app)
        room = MatrixRoom("!r", PM)
        for user in (PM, CX, CXA, CX2):
            room.add_member(user, None, None)
        room.power_levels.users[CX] = 100
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.client, self.lp.dm_rooms, self.lp.busy = PM, FakeClient(room), {CX: "!dm"}, set()
        self.lp.client.solo(True)

    def _patch(self, target, name, value):
        p = mock.patch.object(target, name, value)
        p.start()
        self.addCleanup(p.stop)

    async def as_app(self, repo, method, path, body=None):
        """GitHub as the server's App sees it (read-only)."""
        self.assertEqual((repo, method), (REPO, "GET"))
        if path == f"/repos/{REPO}":
            return 200, {"default_branch": "main"}
        if path == f"/repos/{REPO}/rules/branches/main":
            return 200, self.github["rules"]
        if path.startswith(f"/repos/{REPO}/labels?"):
            return 200, self.github["labels"]
        if path == f"/repos/{REPO}/collaborators/{LOGIN}/permission":
            return self.github["permission"]
        raise AssertionError(f"unexpected read {path}")

    def report(self, sender: str, state: str = "signed_in", login: str | None = LOGIN) -> None:
        """An agent's start report (agent_github.py), as @pm receives it."""
        content = {"state": state, "startup": True, **({"login": login} if login else {})}
        event = ToDeviceEvent.parse_event({"sender": sender, "type": github_login.STATE_TYPE, "content": content})
        asyncio.run(self.lp.on_kind_report(event))

    def check(self) -> str:
        with mock.patch.object(github, "run", mock.AsyncMock(side_effect=AssertionError("no gh on the server"))):
            return asyncio.run(console.act(self.lp, "!r", CX, "check", ""))

    def test_two_agents_signed_in_as_one_account_with_solo_on_are_ready(self):
        self.report(CXA)
        self.report(CX2)
        said = self.check()
        self.assertTrue(said.startswith(f"✓ {REPO} is ready. Checked:"), said)
        self.assertIn(f"cx-agent and cx-codex2-agent: signed in to GitHub as @{LOGIN} (as each reports), "
                      f"and GitHub says @{LOGIN} can write to {REPO}", said)
        self.assertIn("'agent-ok' label is on", said)
        self.assertIn("Solo review is on", said)
        self.assertNotIn("not signed in", said)

    def test_no_report_yet_is_never_called_signed_out(self):
        self.report(CXA)
        said = self.check()
        self.assertIn(readiness_identity.NO_REPORT.format(agent="cx-codex2-agent"), said)
        self.assertNotIn("not signed in", said)

    def test_signed_out_says_the_button(self):
        self.report(CXA)
        self.report(CX2, state="none", login=None)
        self.assertIn("✗ " + readiness_identity.SIGNED_OUT.format(agent="cx-codex2-agent"), self.check())

    def test_shared_account_without_solo_points_at_the_switch(self):
        self.report(CXA)
        self.report(CX2)
        self.lp.client.solo(False)
        said = self.check()
        self.assertIn(f"share the GitHub account @{LOGIN}", said)
        self.assertIn(github_setup.SOLO_FIX, said)
        self.assertNotIn("its own account", said)

    def test_solo_on_but_the_rule_not_yet_letting_admins_past(self):
        self.report(CXA)
        self.report(CX2)
        self.lp.client.solo(True, ok=False)
        said = self.check()
        self.assertIn("doesn't let repo admins merge past it yet", said)
        self.assertNotIn("is ready", said)

    def test_solo_on_with_a_write_only_account(self):
        self.report(CXA)
        self.report(CX2)
        self.github["permission"] = (200, {"permission": "write"})
        self.assertIn(f"@{LOGIN} isn't an admin of {REPO}", self.check())

    def test_solo_for_another_repo_never_counts(self):
        self.report(CXA)
        self.report(CX2)
        self.lp.client.solo(True, repo="csheargm/elsewhere")
        self.assertIn(github_setup.SOLO_FIX, self.check())

    def test_missing_label_gives_github_steps_only_then(self):
        self.report(CXA)
        self.report(CX2)
        self.github["labels"] = []
        said = self.check()
        self.assertIn("Issues → Labels → New label", said)
        self.assertNotIn("github_setup.py", said)          # GUI only: no command for a newcomer
        self.assertNotIn("python", said)

    def test_write_access_unknown_is_said_as_unknown(self):
        self.report(CXA)
        self.report(CX2)
        self.github["permission"] = (502, {"message": "Bad gateway"})
        said = self.check()
        self.assertIn("couldn't check its Write access", said)
        self.assertNotIn("not signed in", said)
        self.assertNotIn("is ready", said)

    def test_no_write_access_says_how_to_add_it(self):
        self.report(CXA)
        self.report(CX2)
        self.github["permission"] = (200, {"permission": "read"})
        self.assertIn("Settings → Collaborators → Add people", self.check())


if __name__ == "__main__":
    unittest.main()
