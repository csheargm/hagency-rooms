"""Connect GitHub: one sign-in's code is posted once in @pm's private chat, however often it's asked
for (pm_github.start_github). Run: .venv/bin/python -m unittest tests.test_github_code_once"""
import asyncio
import unittest

from server import console
from server import github_link
from tests.test_github_link import ANNA, ANNA_ID, LinkCase, message

STILL_RE = r"^The GitHub code I sent above still works for 1[45] more minutes: enter it at https://"


class CodeOnce(LinkCase):
    def test_panel_click_then_asking_in_chat_posts_the_code_once(self):
        """Screen test: Connect GitHub in the panel, then "connect my github" in @pm's chat while that
        sign-in was pending: the same code message twice. The second time: the code above still works."""
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 3)

        async def flow():
            await console.act(self.lp, "!r", ANNA, "github", "connect")
            self.assertTrue(await self.lp.on_github_text(self.dm, message(ANNA, "connect my github")))
            await github_link.FLOWS[ANNA]["task"]
        asyncio.run(flow())
        dm = self.in_room("!dm")
        self.assertEqual(sum("ABCD-1000" in body for body in dm), 1)
        self.assertRegex(dm[1], STILL_RE)

    def test_asking_in_chat_while_the_panels_code_is_still_being_sent(self):
        """GPT review: the chat's ask came while the panel's send was still waiting on Matrix, before
        it was recorded, so both posted the code."""
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 500)   # still pending throughout
        say = self.lp.say

        async def slow(room_id, body, *a, **k):
            await asyncio.sleep(0.05)                     # Matrix takes a moment to accept it
            return await say(room_id, body, *a, **k)
        self.lp.say = slow

        async def flow():
            click = asyncio.create_task(console.act(self.lp, "!r", ANNA, "github", "connect"))
            while ANNA not in github_link.FLOWS:          # GitHub answered: the code is on its way
                await asyncio.sleep(0.001)
            sign_in = github_link.FLOWS[ANNA]["task"]
            await self.lp.on_github_text(self.dm, message(ANNA, "connect my github"))
            await click
            sign_in.cancel()
        asyncio.run(flow())
        dm = self.in_room("!dm")
        self.assertEqual(sum("ABCD-1000" in body for body in dm), 1)
        self.assertRegex(dm[1], STILL_RE)

    def test_a_failed_first_send_gets_the_code_the_next_time(self):
        self.fake.approve("anna-gh", ANNA_ID, answers=["authorization_pending"] * 3)
        say = self.lp.say

        async def refused(room_id, body, *a, **k):
            raise RuntimeError("M_LIMIT_EXCEEDED")
        self.lp.say = refused

        async def flow():
            await console.act(self.lp, "!r", ANNA, "github", "connect")
            self.lp.say = say
            await self.lp.on_github_text(self.dm, message(ANNA, "connect my github"))
            await github_link.FLOWS[ANNA]["task"]
        asyncio.run(flow())
        self.assertIn("ABCD-1000", self.in_room("!dm")[0])


if __name__ == "__main__":
    unittest.main()
