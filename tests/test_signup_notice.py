"""The operator hears about each new sign-up (server/pm_signups.py). Owner 2026-11-23: "how do we know if the
new person signed up"."""
import asyncio
import os
from unittest import mock

from server import loop  # noqa: F401 - first: test_welcome alone hits the known server import cycle
from server import pm_signups
from tests.test_welcome import ANNA, FAR, NEW, WELCOME_ROOM, Base, member


class Notice(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"HAGENCY_OPERATOR": "anna"})
        p.start()
        self.addCleanup(p.stop)
        self.dms = {}

        async def private_room(who):
            return self.dms.setdefault(who, f"!dm-{who}")
        self.lp.private_room = private_room

    def to_operator(self):
        return [body for room, body in self.said if room == f"!dm-{ANNA}"]

    def join(self, who, prev=None):
        async def go():                       # the notice runs in the background: let it finish
            await self.lp.on_welcome_member(WELCOME_ROOM, member(who, prev=prev))
            await asyncio.gather(*list(self.lp.busy))
        self.run_(go())

    def test_a_new_account_is_told_to_the_operator_once(self):
        self.join(NEW)
        self.join(NEW, prev="leave")                                            # rejoins: not new
        self.assertEqual(self.to_operator(), [pm_signups.NOTICE.format(who=r"@newbie1:bridge.localhost")])

    def test_never_the_operator_other_servers_agents_or_bots(self):
        for who in (ANNA, FAR, "@newbie1-agent:bridge.localhost", "@x-bot:bridge.localhost"):
            self.run_(pm_signups.tell_operator(self.lp, who))
        self.assertEqual(self.to_operator(), [])

    def test_the_start_up_scan_tells_nobody(self):
        """GPT review: a lost welcome record must not turn every existing member into a "new sign-up"."""
        self.run_(self.lp.greet_from_welcome("!welcome", NEW))
        self.assertEqual(self.to_operator(), [])

    def test_no_operator_set_or_a_bad_one_means_nobody_is_told(self):
        for value in ("", "Not A Name", "@anna:bridge.localhost"):
            with mock.patch.dict(os.environ, {"HAGENCY_OPERATOR": value}):
                self.assertIsNone(pm_signups.operator())

    def test_a_failure_never_gets_in_the_newcomers_way(self):
        async def broken(room_id, body, *a, **k):
            if room_id == f"!dm-{ANNA}":
                raise RuntimeError("Matrix refused")
            return "$offer"
        self.lp.say = broken
        with self.assertLogs("loop", level="WARNING"):
            self.join(NEW)
        self.assertIn(NEW, self.lp._welcomes)                                  # greeted all the same
