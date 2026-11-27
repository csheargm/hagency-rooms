"""Automatic agent updates, the user flows (docs/adr/0024-automatic-agent-updates.md): a deploy → an idle agent
updates → one chat line; a busy agent → it waits (test_auto_update.AgentWaits), then updates; Auto-update off → the
Update button; a failure → Retry update with the reason, and a back-off; two agents of one owner one at a time; a
task newer than an agent's code → "needs an update first" and an update.
Run: .venv/bin/python -m unittest tests.test_auto_update_flows"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from nio import ToDeviceEvent, ToDeviceResponse

from common import code_report
from common import intents
from common import registry
from common import solo_review
from server import auto_update
from server import console
from server import console_update
from server import pm_solo_rules
from tests import test_more_agents_flows as flows
from tests import test_solo_review
from tests.test_auto_update import Panel, report
from tests.test_kind_report import FakeClient
from tests.test_more_agents_flows import CX, CXA, CXC
from tests.test_solo_review import BETTY
from tests.test_versions import NEW, OLD


class Flows(flows.Base):
    def setUp(self):
        super().setUp()
        self.online = {"cx-agent", "cx-codex-agent"}
        self.lp.agent_online = lambda a: True if a in self.online else None
        self.lp.ever_connected = lambda a: True
        self.lp.client = SimpleNamespace(to_device=mock.AsyncMock(side_effect=lambda m, tx: ToDeviceResponse(m)),
                                         rooms={"!r": SimpleNamespace(users={CX: 0}, invited_users={}, name="P", room_id="!r")})
        self.lp.is_person = lambda r, u: True
        for p in (mock.patch.object(console_update, "SERVER", NEW), mock.patch.object(auto_update, "summary", lambda: "agents update themselves")):
            p.start()
            self.addCleanup(p.stop)

    def start(self, runs, sender=CXA, waits=True):
        """The agent's start on its computer (code_report.send), then @pm receiving it (and its chat line, if any)."""
        client = FakeClient(sender)

        async def go():
            self.assertTrue(await code_report.send(client, flows.PM, report(runs, waits)))
            for event in client.delivered():
                await self.lp.on_kind_report(event)
        asyncio.run(go())

    def asks(self):
        return [c.args[0] for c in self.lp.client.to_device.await_args_list if c.args[0].type == code_report.UPDATE_TYPE]

    def answer(self, sender, result):
        ask = self.lp._updates[sender[1:].split(":")[0]]["id"]
        asyncio.run(self.lp.on_kind_report(ToDeviceEvent.parse_event(
            {"sender": sender, "type": code_report.RESULT_TYPE, "content": {"result": result, "id": ask}})))

    def row(self, name="cx-agent"):
        rows = console.my_agents(self.lp, SimpleNamespace(users={}, invited_users={}), "!r", CX)
        return next(r for r in rows if r["name"] == name)

    def later(self, agent, seconds):
        """Time passes for agent's open ask, and for the owner's stagger."""
        self.lp._updates[agent]["at"] -= seconds
        self.lp.__dict__.get("_auto_asked", {}).update({CX: 0})

    def test_deploy_then_an_idle_agent_updates_and_the_owner_is_told_once(self):
        with mock.patch.object(console_update, "SERVER", OLD):
            self.start(OLD)                   # before the deploy: up to date, nothing asked
            self.assertEqual(self.row()["code_says"], "")
        self.assertEqual(self.asks(), [])
        asyncio.run(auto_update.sweep(self.lp))   # @pm started on NEW (pm_presence.check_agents)
        (ask,) = self.asks()
        self.assertEqual((ask.recipient, ask.content["commit"], ask.content["auto"]), (CXA, NEW["commit"], True))   # the server's own
        row = self.row()
        self.assertEqual((row["code"], row["code_auto"], row["code_says"]), ("behind", True, auto_update.UPDATING))
        self.assertIn("Updating…", Panel.render(row))
        self.assertNotIn('data-a="selfupdate"', Panel.render(row))                     # no Update button
        asyncio.run(auto_update.sweep(self.lp, time.time() + 600))                    # its ask is open: never twice
        self.assertEqual(len(self.asks()), 1)
        self.answer(CXA, "updated")
        self.start(NEW)                                                               # restarted on the new code
        self.assertEqual(self.said, [("!dm", "Your agent **main** updated automatically to bbbbbbb: agents update themselves")])
        row = self.row()
        self.assertEqual((row["code"], row["code_says"], row["code_auto"]), ("current", "", True))
        self.assertIn("updates automatically", Panel.render(row))
        self.start(NEW)                                                               # another start: no second line
        self.assertEqual(len(self.said), 1)

    def test_an_older_report_is_asked_at_once(self):
        self.start(OLD)                       # its sign-in: it is online, so no wait for the presence check
        self.assertEqual(len(self.asks()), 1)

    def test_busy_is_not_a_failure_and_its_next_report_asks_again(self):
        self.start(OLD)
        self.answer(CXA, "busy")
        auto_update.settle(self.lp, "cx-agent")
        row = self.row()
        self.assertEqual((row["code_says"], row["code_retry"]), (console_update.SAYS["busy"], False))
        self.assertEqual(self.lp._auto_updates["cx-agent"]["fails"], 0)
        self.lp._auto_asked[CX] = 0
        self.start(OLD)                       # its next report: asked again
        self.assertEqual(len(self.asks()), 2)

    def test_a_closed_click_never_blocks_and_an_open_one_is_never_replaced(self):
        """GPT review: a refused hand click blocked the owner's automatic asks for good; a click a minute old was
        replaced while its agent still worked, so its real answer was dropped."""
        registry.update(CX, None, auto_update=False)
        self.start(OLD)
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "selfupdate", "", "cx-agent")), "ok")
        self.lp._updates["cx-agent"]["at"] -= console_update.CLICK_GAP_S + 1
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "selfupdate", "", "cx-agent")), console_update.UPDATING)
        self.assertEqual(len(self.asks()), 1)
        self.answer(CXA, "failed")                                     # closed now
        registry.update(CX, None, auto_update=True)
        asyncio.run(auto_update.sweep(self.lp))
        self.assertEqual(len(self.asks()), 2)

    def test_unreachable_counts_as_a_failure(self):
        self.lp.client.to_device.side_effect = OSError("down")
        self.start(OLD)
        st = self.lp._auto_updates["cx-agent"]
        self.assertEqual((st["fails"], st["reason"]), (1, "unreachable"))
        self.assertEqual(self.row()["code_says"], auto_update.WHY["unreachable"])

    def test_a_refused_report_asks_nothing(self):
        self.start(OLD)
        self.lp.client.to_device.reset_mock()
        self.lp._updates.clear()
        self.lp._auto_asked[CX] = 0
        event = ToDeviceEvent.parse_event({"sender": CXA, "type": code_report.EVENT_TYPE,
                                           "content": {**report(OLD), "api_key": "x"}})
        asyncio.run(self.lp.on_kind_report(event))
        self.assertEqual(self.asks(), [])

    def test_edited_offers_its_connect_page_not_a_retry_that_cant_help(self):
        self.start(OLD)
        self.answer(CXA, "edited")
        row = self.row()
        self.assertEqual((row["code_retry"], row["code_fallback"]), (False, True))
        self.assertIn('data-a="update" data-v="agents"', Panel.render(row))
        self.assertNotIn("Retry update", Panel.render(row))

    def test_a_failure_shows_retry_with_the_reason_and_backs_off(self):
        self.start(OLD)
        self.answer(CXA, "failed")
        row = self.row()
        self.assertEqual((row["code_retry"], row["code_says"]), (True, console_update.SAYS["failed"]))
        self.assertIn('data-a="selfupdate"', Panel.render(row))
        self.assertIn("Retry update", Panel.render(row))
        now, st = time.time(), self.lp._auto_updates["cx-agent"]
        self.assertAlmostEqual(st["next"] - now, auto_update.BASE_BACKOFF_S, delta=5)
        asyncio.run(auto_update.sweep(self.lp, now + 60))                            # backing off: not asked
        self.assertEqual(len(self.asks()), 1)
        for n in (2, 3):                      # failures double the wait, then it stops asking
            self.lp._auto_asked[CX] = 0
            asyncio.run(auto_update.sweep(self.lp, st["next"] + 1))
            self.assertEqual(len(self.asks()), n)
            self.answer(CXA, "failed")
            auto_update.settle(self.lp, "cx-agent")
        self.assertEqual(st["fails"], auto_update.MAX_FAILURES)
        self.lp._auto_asked[CX] = 0
        asyncio.run(auto_update.sweep(self.lp, time.time() + 10 * auto_update.MAX_BACKOFF_S))
        self.assertEqual(len(self.asks()), 3)                                          # no loop
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "selfupdate", "", "cx-agent")), "ok")   # Retry update
        self.assertEqual(self.asks()[-1].content["auto"], True)                       # it still waits for its work
        self.assertNotIn("cx-agent", self.lp._auto_updates)                          # the back-off starts over

    def test_a_refusal_retrying_cant_change_stops_at_once(self):
        self.start(OLD)
        self.answer(CXA, "edited")
        auto_update.settle(self.lp, "cx-agent")
        self.assertEqual(self.lp._auto_updates["cx-agent"]["fails"], auto_update.MAX_FAILURES)
        self.assertEqual(self.row()["code_says"], console_update.SAYS["edited"])

    def test_no_answer_counts_as_a_failure_after_the_agents_whole_wait(self):
        self.start(OLD)
        self.later("cx-agent", console_update.WAIT_S + 1)
        self.assertTrue(self.row()["code_updating"])                                  # it may be waiting for its work
        self.later("cx-agent", code_report.IDLE_WAIT_S + code_report.UPDATE_MAX_S)
        row = self.row()
        self.assertEqual((row["code_retry"], row["code_says"]), (True, auto_update.NO_ANSWER))

    def test_off_shows_the_update_button_and_nothing_is_asked(self):
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "autoupdate", "off", "")), "ok")
        self.assertFalse(auto_update.is_on(CX))
        self.start(OLD)
        asyncio.run(auto_update.sweep(self.lp))
        self.assertEqual(self.asks(), [])
        row = self.row()
        self.assertEqual((row["auto_update"], row["code_auto"]), (False, False))
        self.assertIn('data-a="selfupdate"', Panel.render(row))
        self.assertIn(">Update<", Panel.render(row))
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "autoupdate", "on", "")), "ok")   # on again: asked now
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual(asyncio.run(console.act(self.lp, "!r", CX, "autoupdate", "maybe", "")), "unknown action")

    def test_older_code_keeps_the_update_button_once(self):
        self.start(OLD, waits=False)          # code from before automatic updates: it wouldn't wait for its work
        asyncio.run(auto_update.sweep(self.lp))
        self.assertEqual(self.asks(), [])
        row = self.row()
        self.assertEqual((row["code_auto"], row["code_says"]), (False, auto_update.ONCE))
        self.assertIn('data-a="selfupdate"', Panel.render(row))

    def test_two_agents_of_one_owner_are_asked_one_at_a_time(self):
        """Live test 2026-11-27: both asked together raced on their shared install (test_self_update's race)."""
        registry.add_agent(CX, "codex")
        self.start(OLD)
        self.start(OLD, sender=CXC)
        self.assertEqual([a.recipient for a in self.asks()], [CXA])                   # the second waits
        asyncio.run(auto_update.sweep(self.lp, time.time() + auto_update.STAGGER_S + 1))
        self.assertEqual(len(self.asks()), 1)                                         # the first's ask is still open
        self.answer(CXA, "updated")
        self.start(NEW)
        self.assertEqual(self.said, [])       # codex is still to update: one line for both
        self.lp._auto_asked[CX] = 0
        asyncio.run(auto_update.sweep(self.lp))
        self.assertEqual([a.recipient for a in self.asks()], [CXA, CXC])
        self.answer(CXC, "updated")           # its install was already moved: it restarts onto it (agent_update.stale)
        self.start(NEW, sender=CXC)
        self.assertEqual(len(self.said), 1)   # one line per update batch
        self.assertEqual(self.said[0][1], "Your agents **main** and **codex** updated automatically to bbbbbbb: agents update themselves")

    def test_a_task_its_code_predates_asks_for_an_update_never_unknown_task_kind(self):
        from server.pm_solo_rules import AGAIN_OFF, AGAIN_ON, ASKED, BY_HAND, NEEDS_UPDATE, NOT_WANTED
        needs = NEEDS_UPDATE.format(agent="cx-agent")
        self.start(OLD)                       # cx-agent, on code from before solo review (its report asked it once)
        self.lp._updates.clear()
        asyncio.run(self.lp.needs_update("!r", CX, CXA))
        self.assertEqual(self.said[-1], ("!r", needs + ASKED.format(again=AGAIN_ON)))
        self.assertEqual(len(self.asks()), 1)                           # the owner's stagger holds: asked soon, not twice
        self.lp._auto_asked[CX] = 0
        asyncio.run(self.lp.needs_update("!r", CX, CXA, on=False, bypass=True))   # switching off: the bypass too
        self.assertEqual(self.said[-1], ("!r", needs + ASKED.format(again=AGAIN_OFF) + " " + NOT_WANTED))
        self.assertEqual((len(self.asks()), self.asks()[-1].recipient, self.asks()[-1].content["auto"]), (2, CXA, True))
        for _ in range(auto_update.MAX_FAILURES):
            auto_update.failed(self.lp, "cx-agent", "failed", time.time())
        self.lp._updates.clear()
        asyncio.run(self.lp.needs_update("!r", CX, CXA))                 # its back-off's cap holds too: by hand
        self.assertEqual(self.said[-1], ("!r", needs + BY_HAND.format(again=AGAIN_ON)))
        self.assertEqual(len(self.asks()), 2)

    def test_a_task_never_overrides_the_owners_switch_nor_updates_code_that_wont_wait(self):
        """GPT review: a room action (Solo review) must not update an agent whose owner turned Auto-update off,
        nor an older agent at once (it would update under a running turn)."""
        self.start(OLD, waits=False)
        asyncio.run(self.lp.needs_update("!r", CX, CXA))
        self.assertEqual(self.asks(), [])
        registry.update(CX, None, auto_update=False)
        self.start(OLD)
        asyncio.run(self.lp.needs_update("!r", CX, CXA))
        self.assertEqual(self.asks(), [])
        self.assertIn("Update it in My agents", self.said[-1][1])

    def test_the_solo_ruleset_goes_only_to_an_agent_whose_code_runs_it(self):
        from server import pm_solo_rules
        self.start(OLD)
        room = SimpleNamespace(room_id="!r")
        self.lp.agents_in = lambda r: [CXA]
        with mock.patch.object(pm_solo_rules.room_state, "owners", mock.AsyncMock(return_value=[CX])), \
                mock.patch.object(pm_solo_rules, "cannot", lambda o, a: None):
            self.assertEqual(asyncio.run(self.lp.ruleset_agent(room, CX)), (None, (CX, CXA)))
            with mock.patch.object(code_report, "TASK_SINCE", {"solo_ruleset": OLD["time"]}):
                self.assertEqual(asyncio.run(self.lp.ruleset_agent(room, CX)), (CXA, None))


class SoloTask(test_solo_review.Base):
    def test_unknown_task_kind_is_said_as_needs_an_update_never_raw(self):
        """Live test 2026-11-27: an agent on older code answered "unknown task kind 'solo_ruleset'"."""
        self.enable()
        job = self.job()
        with mock.patch("server.console_auto_update.update_for_task", mock.AsyncMock(return_value=True)) as ask:
            self.answer(job, error="unknown task kind 'solo_ruleset'")
        ask.assert_awaited_once()
        self.assertIn("needs an update first", self.said[-1][0])
        self.assertNotIn("unknown task kind", self.said[-1][0])

    def test_a_late_unknown_task_kind_follows_the_switch_as_it_is_now(self):
        """GPT re-check: an "on" ask answered after Solo review was switched off told them to switch it on again."""
        self.enable()
        job = self.job()
        self.run_(self.lp.act(self.room, BETTY, intents.SOLO_OFF))
        with mock.patch("server.console_auto_update.update_for_task", mock.AsyncMock(return_value=True)):
            self.answer(job, error="unknown task kind 'solo_ruleset'")
        self.assertIn(pm_solo_rules.AGAIN_OFF, self.said[-1][0])
        self.assertNotIn(pm_solo_rules.NOT_WANTED, self.said[-1][0])         # nothing was added: nothing to take out
        self.assertEqual(self.tasks(solo_review.TASK_KIND)[-1]["task_id"], job["task_id"])   # no follow-up to it again


if __name__ == "__main__":
    unittest.main()
