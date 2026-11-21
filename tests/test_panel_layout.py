"""At start, older project rooms get the Hagency panel on top, as new rooms do (seen live: in the dev
room it sat in Element's right-side Extensions list, and the owner thought it was missing).
Run: .venv/bin/python -m unittest tests.test_panel_layout"""
import asyncio
import unittest
from types import SimpleNamespace

from nio import RoomPutStateResponse

from server import panel
from server import project_rooms
from server.loop import Loop

PM = "@pm:bridge.localhost"
WIDGET = {"type": "m.custom", "url": panel.widget_url(), "name": "Hagency", "id": "hagency"}
OTHER = {"container": "right", "index": 1}


class LiftPanels(unittest.TestCase):
    def setUp(self):
        self.state = {}                                       # (room, kind) → content
        self.broken = set()                                   # rooms whose state can't be read
        self.lp = Loop.__new__(Loop)
        self.lp.user_id, self.lp.dm_rooms = PM, {}

        async def get(rid, kind, key=""):
            if rid in self.broken:
                return SimpleNamespace(status_code="M_UNKNOWN", message="boom")
            if (rid, kind) not in self.state:
                return SimpleNamespace(status_code="M_NOT_FOUND")
            return SimpleNamespace(content=self.state[(rid, kind)])

        async def put(rid, kind, content, state_key=""):
            self.state[(rid, kind)] = content
            return RoomPutStateResponse("$s", rid)
        room = lambda rid: SimpleNamespace(room_id=rid, users={PM: None, "@anna:x": None, "@betty:x": None},
                                           canonical_alias=None, name="p")
        self.lp.client = SimpleNamespace(rooms={r: room(r) for r in ("!old", "!new", "!broken", "!bare", "!lobby")},
                                         room_get_state_event=get, room_put_state=put)
        for rid in ("!old", "!new", "!broken", "!lobby"):
            self.state[(rid, panel.WIDGET_KIND)] = WIDGET
        self.state[("!old", panel.LAYOUT_KIND)] = {"widgets": {"hagency": {"container": "right"}, "jitsi": OTHER}}
        self.state[("!new", panel.LAYOUT_KIND)] = {"widgets": {"hagency": dict(panel.TOP)}}
        self.state[("!lobby", panel.LAYOUT_KIND)] = {"widgets": {"hagency": {"container": "right"}}}
        self.broken.add("!broken")
        project_rooms.FRONT_DOOR_IDS.add("!lobby")
        self.addCleanup(project_rooms.FRONT_DOOR_IDS.discard, "!lobby")

    def test_an_old_right_side_panel_moves_on_top_and_others_stay(self):
        with self.assertLogs("loop", "INFO") as logs:
            asyncio.run(self.lp.lift_panels())
        self.assertEqual(self.state[("!old", panel.LAYOUT_KIND)], {"widgets": {"hagency": panel.TOP, "jitsi": OTHER}})
        self.assertIn("moved the Hagency panel to the top of !old", "\n".join(logs.output))
        self.assertIn("couldn't move the Hagency panel in !broken", "\n".join(logs.output))   # and the rest went on

    def test_rooms_already_on_top_without_a_panel_or_front_doors_are_left_alone(self):
        writes = []
        real = self.lp.client.room_put_state

        async def put(rid, kind, content, state_key=""):
            writes.append(rid)
            return await real(rid, kind, content, state_key)
        self.lp.client.room_put_state = put
        with self.assertLogs("loop", "INFO"):
            asyncio.run(self.lp.lift_panels())
        self.assertEqual(writes, ["!old"])
        self.assertEqual(self.state[("!lobby", panel.LAYOUT_KIND)], {"widgets": {"hagency": {"container": "right"}}})
        self.assertNotIn(("!bare", panel.LAYOUT_KIND), self.state)

    def test_another_page_under_the_panels_key_is_never_promoted(self):
        """GPT review: anyone allowed to write the widget state could put their own page there."""
        self.state[("!old", panel.WIDGET_KIND)] = {**WIDGET, "url": "https://evil.example/panel"}
        self.assertFalse(asyncio.run(panel.lift_to_top(self.lp.client, "!old")))
        self.assertEqual(self.state[("!old", panel.LAYOUT_KIND)]["widgets"]["hagency"], {"container": "right"})

    def test_a_panel_with_no_layout_yet_gets_one(self):
        self.state[("!bare", panel.WIDGET_KIND)] = WIDGET
        self.assertTrue(asyncio.run(panel.lift_to_top(self.lp.client, "!bare")))
        self.assertEqual(self.state[("!bare", panel.LAYOUT_KIND)], {"widgets": {"hagency": panel.TOP}})
        self.assertFalse(asyncio.run(panel.lift_to_top(self.lp.client, "!bare")))   # the next start: nothing more


if __name__ == "__main__":
    unittest.main()
