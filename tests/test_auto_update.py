"""Automatic agent updates (docs/adr/0024-automatic-agent-updates.md): server/auto_update.py, console_auto_update.py,
the agent's wait for no task running (agent_update.py) and version-gated tasks (pm_solo_rules.py).
The user flows (deploy → updated, busy → waits, off → button, failure → Retry): test_auto_update_flows.py.
Run: .venv/bin/python -m unittest tests.test_auto_update"""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nio import ToDeviceEvent

from adapters.agents import agent_update
from adapters.agents import self_update
from common import code_report
from common import version
from server import auto_update
from server import console_update
from tests.test_self_update import FakeBridge
from tests.test_versions import NEW, NODE, OLD, ROOT


def report(runs, waits=True):
    said = code_report.build(runs, False)
    return said if waits else {k: v for k, v in said.items() if k != code_report.WAITS}


class Reports(unittest.TestCase):
    def test_new_code_says_it_waits_and_pm_keeps_it(self):
        said = code_report.build(OLD, False)
        self.assertEqual(said[code_report.WAITS], "yes")
        self.assertTrue(code_report.valid(said)[code_report.WAITS])
        self.assertNotIn(code_report.WAITS, code_report.valid(report(OLD, waits=False)))   # older code: no such field
        self.assertIsNone(code_report.valid({**said, code_report.WAITS: "no"}))
        kept = code_report.valid(said)
        self.assertEqual(code_report.stored(kept), kept)
        self.assertIsNone(code_report.stored({**kept, code_report.WAITS: "yes"}))     # a damaged record: none
        self.assertIn("busy", code_report.RESULTS)

    def test_a_task_kind_newer_than_an_agent(self):
        since = code_report.TASK_SINCE["solo_ruleset"]
        self.assertFalse(code_report.runs_task(None, "solo_ruleset"))                   # no report: older still
        self.assertFalse(code_report.runs_task({"commit": "a" * 40, "time": since - 1}, "solo_ruleset"))
        self.assertTrue(code_report.runs_task({"commit": "a" * 40, "time": since}, "solo_ruleset"))
        self.assertTrue(code_report.runs_task(None, "build"))                          # every agent runs these

    def test_what_changed_is_the_commit_subject_or_the_pull_requests_title(self):
        d = Path(tempfile.mkdtemp())
        (d / "REVISION").write_text(f"{NEW['commit']}\n{NEW['time']}\nfix(panel): shorter caption\n\n")
        self.assertEqual((version.summary(d), version.of(d)), ("fix(panel): shorter caption", NEW))   # of() unchanged
        (d / "REVISION").write_text(f"{NEW['commit']}\n{NEW['time']}\nMerge pull request #70 from x/y\n\n"
                                    "feat(agents): <b>automatic</b> [updates](http://x)\n")
        self.assertEqual(version.summary(d), "feat(agents): bautomatic/b updates(http://x)")     # never markup
        (d / "REVISION").write_text(f"{NEW['commit']}\n{NEW['time']}\n" + "x" * 300)
        self.assertEqual(len(version.summary(d)), version.SUMMARY_MAX)
        (d / "REVISION").write_text(f"{NEW['commit']}\n{NEW['time']}\n")               # an older deploy's file
        self.assertEqual(version.summary(d), "")
        self.assertTrue(version.summary(ROOT))                                        # a checkout: git says
        script = (ROOT / "deploy" / "deploy.sh").read_text()
        self.assertIn("--format='%H%n%ct%n%s%n%b'", script)

    def test_one_line_names_the_agents_the_version_and_what_changed(self):
        with mock.patch.object(console_update, "SERVER", {**NEW, "number": "1.4.0"}), mock.patch.object(auto_update, "summary", lambda: "faster"):
            self.assertEqual(auto_update.line(["pi"]), "Your agent **pi** updated automatically to v1.4.0 · bbbbbbb: faster")
            self.assertEqual(auto_update.line(["pi", "cx", "*x*"]),
                             "Your agents **pi**, **cx** and **x** updated automatically to v1.4.0 · bbbbbbb: faster")
        with mock.patch.object(console_update, "SERVER", NEW), mock.patch.object(auto_update, "summary", lambda: "_fast_ ~x~"):
            self.assertEqual(auto_update.line(["my_pi"]),                   # GPT review: "_fast_" rendered as emphasis
                             r"Your agent **my\_pi** updated automatically to bbbbbbb: \_fast\_ \~x\~")

    def test_the_servers_wait_covers_the_agents_whole_update(self):
        """GPT review: the lock wait alone (960 s) was longer than the 300 s @pm waited for an answer."""
        self.assertGreaterEqual(code_report.UPDATE_MAX_S,
                                self_update.LOCK_WAIT_S + self_update.PIP_TIMEOUT_S + 4 * self_update.GIT_TIMEOUT_S)
        click = {"at": 0, "result": None, "auto": True}
        self.assertGreaterEqual(console_update.budget(click), code_report.IDLE_WAIT_S + code_report.UPDATE_MAX_S)
        click.update(result="updated", answered=10_000)                  # its drain counts from its answer
        self.assertTrue(console_update.is_open(click, 10_000 + console_update.DRAIN_S))
        self.assertFalse(console_update.is_open({**click, "result": "failed"}, 10_001))


class AgentWaits(unittest.TestCase):
    """The agent's side: an automatic request waits until no task runs; a click doesn't."""
    def request(self, b, auto=True, result="updated", work=None, wait_s=5.0):
        async def go():
            with mock.patch.object(self_update, "apply", return_value=result) as apply, \
                    mock.patch.object(agent_update, "IDLE_POLL_S", 0.01), mock.patch.object(code_report, "IDLE_WAIT_S", wait_s):
                if work:
                    b.tasks.add(asyncio.create_task(work()))
                content = {"commit": "a" * 40, "id": "k1", **({"auto": True} if auto else {})}
                await b.on_update_request(ToDeviceEvent.parse_event({"sender": agent_update.PM_ID, "type": code_report.UPDATE_TYPE,
                                                                     "content": content}))
                await asyncio.gather(*b.tasks)
            return apply
        return asyncio.run(go())

    def test_an_idle_agent_updates_at_once(self):
        b = FakeBridge()
        self.request(b).assert_called_once()
        self.assertEqual(b.sent[0][2]["result"], "updated")

    def test_a_busy_agent_waits_for_its_work_then_updates(self):
        b, order = FakeBridge(), []

        async def build():                    # a build in flight
            await asyncio.sleep(0.2)
            order.append("build done")
        with mock.patch.object(self_update, "apply", side_effect=lambda *a: order.append("update") or "updated"):
            asyncio.run(self.run_with(b, build))
        self.assertEqual(order, ["build done", "update"])                 # never cut off
        self.assertTrue(b.updated)

    async def run_with(self, b, work):
        with mock.patch.object(agent_update, "IDLE_POLL_S", 0.01):
            b.tasks.add(asyncio.create_task(work()))
            event = ToDeviceEvent.parse_event({"sender": agent_update.PM_ID, "type": code_report.UPDATE_TYPE,
                                               "content": {"commit": "a" * 40, "id": "k1", "auto": True}})
            await b.on_update_request(event)
            await asyncio.gather(*b.tasks)

    def test_still_busy_at_the_end_of_the_wait_it_says_so_and_keeps_working(self):
        b = FakeBridge()

        async def long_review():
            await asyncio.sleep(0.5)
        self.assertFalse(self.request(b, work=long_review, wait_s=0.1).called)
        self.assertEqual(b.sent, [(code_report.RESULT_TYPE, agent_update.PM_ID, {"result": "busy", "id": "k1"})])
        self.assertFalse(b.updated)
        b.tasks.clear()                       # the review finished meanwhile
        self.assertTrue(self.request(b).called)                                     # not stuck: asked again, it updates

    def test_work_that_came_in_during_the_update_finishes_before_the_restart(self):
        """GPT review: idle was checked once; a turn or sign-in that started while git and pip ran was drained short."""
        b, order = FakeBridge(), []

        async def turn():
            await asyncio.sleep(0.2)
            order.append("turn done")

        def apply(*a):                       # a message arrives while the update runs (in its thread)
            loop.call_soon_threadsafe(lambda: b.tasks.add(loop.create_task(turn())))
            time.sleep(0.05)
            return "updated"

        async def go():
            nonlocal loop
            loop = asyncio.get_running_loop()
            with mock.patch.object(self_update, "apply", side_effect=apply), mock.patch.object(agent_update, "IDLE_POLL_S", 0.01):
                await b.update_to("a" * 40, "k1", auto=True)
                order.append("restart" if b.sync_point.stop.is_set() else "no restart")
        loop = None
        asyncio.run(go())
        self.assertEqual(order, ["turn done", "restart"])

    def test_a_github_sign_in_counts_as_work(self):
        b = FakeBridge()

        async def sign_in():
            await asyncio.sleep(0.5)

        async def go():
            b._gh_job = asyncio.create_task(sign_in())
            self.assertFalse(b.idle())
            await b._gh_job
            self.assertTrue(b.idle())
        asyncio.run(go())


@unittest.skipIf(NODE is None, "node not installed")
class Panel(unittest.TestCase):
    @staticmethod
    def render(row: dict) -> str:
        src = (ROOT / "static" / "panel_agents.js").read_text()
        prog = (src + "\nconst esc = t => String(t).replace(/[&<>\"']/g, c => '&#' + c.charCodeAt(0) + ';');"
                "const cap = t => t.charAt(0).toUpperCase() + t.slice(1);"
                "process.stdout.write(agentRow(JSON.parse(process.argv[1]), null, false, false));")
        return subprocess.run([NODE, "-e", prog, json.dumps({**row, "label": "main", "first": True, "follow": "always"})],
                              capture_output=True, text=True, timeout=30, check=True, env=dict(os.environ)).stdout

    def test_the_switch_shows_its_state(self):
        src = (ROOT / "static" / "panel_agents.js").read_text()
        prog = (src + "\nconst esc = t => String(t); const cap = t => t.charAt(0).toUpperCase() + t.slice(1);"
                "process.stdout.write(autoSwitch({agents: [{remote: true, auto_update: true}]}) + '|' + autoSwitch({agents: []}));")
        out = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30, check=True).stdout
        on, none = out.split("|")
        self.assertIn('data-a="autoupdate" data-v="on"', on)
        self.assertIn("class=on>On", on.replace("\n", "").replace("    ", " ").replace(" >", ">"))
        self.assertEqual(none, "")


if __name__ == "__main__":
    unittest.main()
