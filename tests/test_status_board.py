"""Unit tests: the live Project status board. Run: .venv/bin/python -m unittest tests.test_status_board"""
import asyncio
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from nio import RoomGetStateEventResponse, RoomPutStateResponse, RoomSendResponse

from server import project_rooms
from common import room_state
from server import status_board
from server.status_board import Boards, render

NOW = 1_000_000.0
A = "@anna-hermes-agent:bridge.localhost"


class Render(unittest.TestCase):
    def test_shows_step_agent_and_time(self):
        text = render({"210": {"room": "!r", "repo": "your-name/hagency-hello", "stage": "building", "agent": A, "since": NOW - 180,
                               "title": "docs: add HERMES.md"}}, "autonomous", [], NOW)
        self.assertIn("loop: **autonomous**", text)
        self.assertIn("#210 docs: add HERMES.md — building · anna-hermes-agent · 3 min", text)

    def test_queue_done_and_stopped(self):
        items = {"200": {"stage": "done", "ended": NOW - 60, "title": "a"},
                 "201": {"stage": "done", "pr": 301, "ended": NOW - 60, "title": "b"},
                 "96": {"stage": "stopped", "why": "PR #97 has merge conflicts", "ended": NOW - 60},
                 "50": {"stage": "done", "pr": 51, "ended": NOW - 2 * status_board.RECENT_S}}
        text = render(items, "assist", [{"number": 211, "title": "docs: add BETTY.md"}], NOW)
        self.assertIn("- #211 docs: add BETTY.md", text)
        self.assertIn("#200 a (closed: already done)", text)
        self.assertIn("#201 b → PR #301 merged", text)
        self.assertIn("#96 — PR #97 has merge conflicts", text)
        self.assertNotIn("#50", text)                       # older than a day
        self.assertIn("nothing in progress", text)

    def test_an_agents_name_is_never_markup(self):
        """GPT review 16: who() put the agent's id on the board unescaped."""
        self.assertEqual(status_board.who("@<b>x</b>:s"), "&lt;b&gt;x&lt;/b&gt;")

    def test_never_shows_paths_or_tasks(self):
        text = render({"7": {"stage": "building", "agent": A, "since": NOW, "body": "/Users/x/secret",
                             "task": {"kind": "build"}}}, "autonomous", [], NOW)
        self.assertNotIn("/Users", text)


class FakeClient:
    def __init__(self):
        self.rooms = {"!r": None}
        self.sent, self.pins = [], None

    async def room_send(self, room_id, kind, content):
        self.sent.append(content)
        return RoomSendResponse(f"$e{len(self.sent)}", room_id)

    async def room_get_state(self, room_id):
        return SimpleNamespace(events=[])

    async def room_get_state_event(self, room_id, kind, key):
        return RoomGetStateEventResponse({"pinned": ["$other"]}, kind, key, room_id)

    async def room_put_state(self, room_id, kind, content, **kw):
        if kind == "m.room.pinned_events":
            self.pins = content["pinned"]
        elif kind == "im.vector.modular.widgets":
            self.widget = content
        elif kind != "io.element.widgets.layout":
            self.status = content
        return RoomPutStateResponse("$p", room_id)


class Board(unittest.TestCase):
    def setUp(self):
        for name, value in (("bound", True), ("repo_of", "your-name/hagency-hello")):   # a project room
            _p = mock.patch.object(room_state, name, mock.AsyncMock(return_value=value))
            _p.start()
            self.addCleanup(_p.stop)
        self.b = Boards()
        self.b.client, self.b.state, self.b.open_issues = FakeClient(), {}, {}
        self.b.board_file = Path(tempfile.mkdtemp()) / "boards.json"
        self.b.boards = self.b.load_boards()

        async def level(room_id):
            return "autonomous"
        self.b.level = level

    def refresh(self):
        asyncio.run(self.b.refresh_boards())

    def test_first_refresh_posts_and_pins_keeping_other_pins(self):
        self.refresh()
        self.assertEqual(len(self.b.client.sent), 1)
        self.assertEqual(self.b.client.pins, ["$other", "$e1"])

    def test_unchanged_text_is_not_reposted(self):
        self.refresh()
        self.refresh()
        self.assertEqual(len(self.b.client.sent), 1)

    def test_change_edits_the_same_message(self):
        self.refresh()
        self.b.state["7"] = {"room": "!r", "repo": "your-name/hagency-hello", "stage": "building", "agent": A, "since": 0, "title": "x"}
        self.refresh()
        edit = self.b.client.sent[-1]
        self.assertEqual(edit["m.relates_to"], {"rel_type": "m.replace", "event_id": "$e1"})
        self.assertIn("#7 x — building", edit["m.new_content"]["body"])
        self.assertEqual(self.b.boards["!r"]["event_id"], "$e1")

    def test_pin_is_retried_until_allowed(self):
        """Seen live: the board was posted before @pm was made a moderator, and never pinned."""
        refused = {"yes": True}

        async def put_state(room_id, kind, content, **kw):
            if refused["yes"]:
                return SimpleNamespace(message="M_FORBIDDEN")
            if kind == "m.room.pinned_events":
                self.b.client.pins = content["pinned"]
            return RoomPutStateResponse("$p", room_id)
        self.b.client.room_put_state = put_state
        self.refresh()
        self.assertIsNone(self.b.client.pins)
        refused["yes"] = False                               # anna makes @pm a moderator
        self.refresh()                                       # same text: no repost, but the pin is retried
        self.assertEqual(self.b.client.pins, ["$other", "$e1"])
        self.assertEqual(len(self.b.client.sent), 1)

    def test_status_is_published_for_agents_to_read(self):
        """the owner: agents answered from their own memory ('awaiting room approval') after the project was done."""
        self.refresh()
        self.assertEqual(self.b.client.status["repo"], "your-name/hagency-hello")   # this room's own repo
        self.assertIn("Project status", self.b.client.status["text"])

    def test_an_unbound_room_gets_no_board(self):
        """GPT review 7: any local account could invite @pm and read the repo's issues."""
        with mock.patch.object(room_state, "bound", mock.AsyncMock(return_value=False)), \
                mock.patch.object(room_state, "repo_of", mock.AsyncMock(return_value=None)):
            self.refresh()
        self.assertEqual(self.b.client.sent, [])

    def test_board_survives_a_restart(self):
        self.refresh()
        fresh = Boards()
        fresh.board_file = self.b.board_file
        self.assertEqual(fresh.load_boards()["!r"]["event_id"], "$e1")



class PanelFirst(unittest.TestCase):
    """The owner: the side panel only came once a repo was bound, yet it is where the repo is picked."""
    PM = "@pm:bridge.localhost"

    def setUp(self):
        _p = mock.patch.object(room_state, "repo_of", mock.AsyncMock(return_value=None))   # no repo yet
        _p.start()
        self.addCleanup(_p.stop)
        self.b = Boards()
        self.b.client, self.b.state, self.b.open_issues = FakeClient(), {}, {}
        self.b.board_file = Path(tempfile.mkdtemp()) / "boards.json"
        self.b.boards, self.b.user_id, self.b.dm_rooms = {}, self.PM, {}
        self.b.is_welcome_room = lambda room: room.room_id == "!welcome"

    def room(self, room_id="!r", pm_level=50, name="Our project", users=("@anna:bridge.localhost",)):
        levels = SimpleNamespace(get_user_level=lambda u: pm_level if u == self.PM else 100)
        return SimpleNamespace(room_id=room_id, name=name, power_levels=levels, users={self.PM: 0, **{u: 0 for u in users}})

    def test_as_soon_as_i_am_a_moderator_with_no_repo_and_once(self):
        self.b.client.rooms = {"!r": self.room()}
        asyncio.run(self.b.refresh_boards())
        self.assertEqual(self.b.client.widget["name"], "Hagency")
        self.assertTrue(self.b.boards["!r"]["panel"])
        self.b.client.widget = None
        asyncio.run(self.b.refresh_boards())
        self.assertIsNone(self.b.client.widget)

    def test_not_before_i_am_a_moderator_nor_in_welcome_or_my_private_chats(self):
        self.b.dm_rooms = {"@anna:bridge.localhost": "!dm"}
        self.b.client.rooms = {"!r": self.room(pm_level=0), "!welcome": SimpleNamespace(**vars(self.room("!welcome")), canonical_alias=project_rooms.WELCOME_ALIAS),
                               "!dm": self.room("!dm"), "!old": self.room("!old", name="Hagency (private)")}
        asyncio.run(self.b.refresh_boards())
        self.assertFalse(hasattr(self.b.client, "widget"))


if __name__ == "__main__":
    unittest.main()
