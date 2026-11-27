"""A trial message that waited for the model slot is never sent to Ollama once the visitor left the chat or
connected an agent of their own meanwhile (GPT whole-batch review), and it isn't counted against their day.
Run: .venv/bin/python -m unittest tests.test_trial_queued"""
import asyncio
import unittest

from tests.test_trial_agent import NEW, Base, message


class QueuedThenGone(Base):
    def waits_then(self, change):
        self.enrol()
        dm = self.client.rooms["!trialdm"]

        async def go():
            await self.agent.slot.acquire()          # another visitor's answer holds the one slot
            await self.agent.on_message(dm, message(NEW, "hello"))
            await asyncio.sleep(0)
            change(dm)                               # meanwhile: they leave, or connect their own agent
            self.agent.slot.release()
            await self.agent.drain()
        self.run_(go())

    def test_left_while_queued_nothing_reaches_the_model(self):
        self.waits_then(lambda dm: dm.users.pop(NEW))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.agent.usage.used(NEW), 0)

    def test_connected_an_own_agent_while_queued_nothing_reaches_the_model(self):
        self.waits_then(lambda dm: self.eligible.discard(NEW))
        self.assertEqual(self.asked, [])
        self.assertEqual(self.agent.usage.used(NEW), 0)

    def test_still_here_and_eligible_it_is_answered(self):
        self.waits_then(lambda dm: None)
        self.assertEqual(len(self.asked), 1)


if __name__ == "__main__":
    unittest.main()
