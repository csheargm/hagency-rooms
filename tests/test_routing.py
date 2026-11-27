"""Unit tests, step 4: @pm's task routing with several agents per person (routing.py, loop.py): any
agent may build, but two agents of one person never check each other's work, decided by the
owners' full Matrix IDs. Run: .venv/bin/python -m unittest tests.test_routing"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import ownership
from common import registry
from server import routing
from server.loop import Loop

S = "bridge.localhost"
CX, BETTY = f"@cx:{S}", f"@betty:{S}"
CXA, CXC, BA = f"@cx-agent:{S}", f"@cx-codex-agent:{S}", f"@betty-agent:{S}"


class Routing(unittest.TestCase):
    OWNERS = {CXA: CX, CXC: CX, BA: BETTY}

    def test_two_agents_of_one_person_never_check_each_other(self):
        owner = self.OWNERS.get
        self.assertEqual(routing.checkers_for([CXA, CXC], CXA, owner), [])
        self.assertEqual(routing.checkers_for([CXA, CXC, BA], CXC, owner), [BA])
        self.assertEqual(routing.checkers_for([CXA, CXC, BA], BA, owner), [CXA, CXC])   # either of cx's
        self.assertEqual(routing.checkers_for([CXA, "@odd-agent:x"], CXA, owner), [])  # no registered owner: never
        self.assertEqual(routing.checkers_for([CXA, BA], "@odd-agent:x", owner), [])

    def test_known_offline_agents_get_no_work(self):
        online = {"cx-agent": False, "cx-codex-agent": True}.get
        self.assertEqual(routing.available([CXA, CXC, BA], online), [CXC, BA])

    def test_a_check_from_the_builders_own_person_is_never_accepted(self):
        """GPT 24: also when results come in, not only when the checker is picked."""
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({CX: {"agent": "remote", "more": [{"label": "codex"}]}, BETTY: {"agent": "remote"}}))
        with mock.patch.object(registry, "PATH", path):
            lp = Loop.__new__(Loop)
            lp.room_repos, lp.save, lp.say = {"!r": "o/r"}, lambda: None, mock.AsyncMock()
            for reviewer, stage in ((CXC, "reviewing"), (BA, "approved")):
                lp.state = {"7": {"room": "!r", "repo": "o/r", "stage": "reviewing", "builder": CXA, "reviewer": reviewer,
                                  "pr": 9, "task_id": "T"}}
                lp.key_for_result = lambda rid, result: "7"
                lp.after_review_step = mock.AsyncMock(side_effect=lambda rid, item, key, result: item.update(stage="approved"))
                asyncio.run(lp.on_result(SimpleNamespace(room_id="!r"), reviewer,
                                         {"kind": "review", "issue": 7, "pr": 9, "task_id": "T", "verdict": "approve"}))
                self.assertEqual(lp.state["7"]["stage"], stage)

    def test_the_loop_asks_another_persons_agent_by_owner_full_id(self):
        path = Path(tempfile.mkdtemp()) / "agents.json"
        path.write_text(json.dumps({CX: {"agent": "remote", "more": [{"label": "codex"}]}, BETTY: {"agent": "remote"}}))
        with mock.patch.object(registry, "PATH", path):
            lp = Loop.__new__(Loop)
            lp._seen, lp.say, lp.stop = {}, mock.AsyncMock(), mock.AsyncMock()
            lp.solo_on = mock.AsyncMock(return_value=False)      # solo review is off by default (ADR 0023)
            lp.agents_in = lambda room: [CXA, CXC]
            item = {"builder": CXA}
            self.assertFalse(asyncio.run(lp.pick_reviewer(SimpleNamespace(room_id="!r"), item, "7", "review PR #1")))
            lp.stop.assert_awaited_once()                                                # cx's own agents: stopped
            lp.agents_in = lambda room: [CXA, CXC, BA]
            self.assertTrue(asyncio.run(lp.pick_reviewer(SimpleNamespace(room_id="!r"), item, "7", "review PR #1")))
            self.assertEqual(item["reviewer"], BA)
            self.assertEqual(ownership.owner_id(CXC), CX)


if __name__ == "__main__":
    unittest.main()
