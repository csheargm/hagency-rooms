"""Trial traffic, talk and builds, holds at most ONE Ollama slot, across processes, talk first
(common/model_slot.py, ADR 0027). Run: .venv/bin/python -m unittest tests.test_model_slot"""
import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from common import model_slot
from server import trial_agent

ROOT = Path(__file__).resolve().parent.parent


class OneSlot(unittest.TestCase):
    def setUp(self):
        self.path = model_slot.lock_path(Path(tempfile.mkdtemp()))

    def test_a_build_holding_the_slot_makes_talk_wait_then_talk_gets_it(self):
        async def go():
            build, talk = model_slot.Slot(self.path, model_slot.BUILD), model_slot.Slot(self.path, model_slot.TALK)
            await build.acquire()
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(talk.acquire(), 0.3)
            self.assertFalse(talk.talk_waiting())              # a cancelled wait leaves no marker behind
            build.release()
            await asyncio.wait_for(talk.acquire(), 1)
            talk.release()
        asyncio.run(go())

    def test_talk_goes_first_a_waiting_talk_answer_beats_a_waiting_build(self):
        async def go():
            holder = model_slot.Slot(self.path, model_slot.TALK)
            talk, build = model_slot.Slot(self.path, model_slot.TALK), model_slot.Slot(self.path, model_slot.BUILD)
            await holder.acquire()
            order = []

            async def take(slot, name):
                await slot.acquire()
                order.append(name)
                await asyncio.sleep(0.05)
                slot.release()
            waiting = [asyncio.create_task(take(build, "build")), asyncio.create_task(take(talk, "talk"))]
            await asyncio.sleep(0.2)
            holder.release()
            await asyncio.gather(*waiting)
            self.assertEqual(order, ["talk", "build"])
        asyncio.run(go())

    def test_a_stale_marker_from_a_dead_process_doesnt_block_builds(self):
        marker = self.path.parent / f"{self.path.name}.talk.999.1"
        marker.touch()
        old = time.time() - model_slot.STALE_MARKER_S - 5
        os.utime(marker, (old, old))
        build = model_slot.Slot(self.path, model_slot.BUILD)
        asyncio.run(asyncio.wait_for(build.acquire(), 1))
        build.release()

    def test_another_process_holding_it_holds_it_for_us_too(self):
        script = textwrap.dedent(f"""
            import asyncio, sys, time
            sys.path.insert(0, {str(ROOT)!r})
            from common import model_slot
            s = model_slot.Slot({str(self.path)!r}, model_slot.BUILD)
            asyncio.run(s.acquire()); print("held", flush=True); time.sleep(1.5); s.release()
        """)
        other = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(other.stdout.close)
        self.addCleanup(other.wait)
        self.assertEqual(other.stdout.readline().strip(), "held")
        talk = model_slot.Slot(self.path, model_slot.TALK)
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(asyncio.wait_for(talk.acquire(), 0.3))
        asyncio.run(asyncio.wait_for(talk.acquire(), 5))     # once it lets go
        talk.release()

    def test_the_talk_trial_uses_the_shared_slot(self):
        folder = Path(tempfile.mkdtemp())
        agent = trial_agent.TrialAgent(client=None, eligible=lambda u: True, data_dir=folder)
        self.assertIsInstance(agent.slot, model_slot.Slot)
        self.assertEqual((agent.slot.path, agent.slot.kind), (model_slot.lock_path(folder), model_slot.TALK))


if __name__ == "__main__":
    unittest.main()
