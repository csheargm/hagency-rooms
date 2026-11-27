"""Live bug: the agent asked "Tell me a city or zip code", its owner answered "95130" without an
@mention, and the agent ignored it. Now an open question offers the owner's next messages
(bridge_followup.py). Run: .venv/bin/python -m unittest tests.test_followup"""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from adapters.agents import bridge
from adapters.agents import bridge_followup
from adapters.agents import workdirs

S = "bridge.localhost"
ANNA, BETTY, AGENT = f"@anna:{S}", f"@betty:{S}", f"@anna-agent:{S}"
ROOM = "!dev:x"


def msg(sender, body, eid, mentions=None, reply_to=None):
    content = {"body": body, "msgtype": "m.text"}
    if mentions is not None:
        content["m.mentions"] = {"user_ids": mentions}
    if reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
    return SimpleNamespace(sender=sender, body=body, event_id=eid, source={"content": content},
                           server_timestamp=int(time.time() * 1000))


class FollowUp(unittest.TestCase):
    def setUp(self):
        self.b = bridge.Bridge("anna", "claude")
        self.room = SimpleNamespace(room_id=ROOM, display_name="dev", joined_count=3, invited_count=0,
                                    users={ANNA: None, BETTY: None, AGENT: None}, invited_users={},
                                    user_name=lambda u: u)
        self.replies, self.sent, self.prompts = [], [], []

        async def run_turn(backend, workdir, prompt, **kw):
            self.prompts.append(prompt)
            return self.replies.pop(0)

        async def send(room_id, body, reply_to=None, **kw):
            self.sent.append(body)
            return f"$sent{len(self.sent)}"
        self.b.send, self.b.keep_typing = send, mock.AsyncMock()
        self.b.client.room_typing = mock.AsyncMock()
        for p in (mock.patch.object(bridge.agents, "run_turn", run_turn),
                  mock.patch.object(bridge.room_context, "snapshot", lambda *a: {"status": "", "conversation": ""}),
                  mock.patch.object(workdirs, "WORK_HOME", Path(tempfile.mkdtemp())),
                  mock.patch.object(bridge.lifecycle, "is_stale", lambda *a: False)):
            p.start()
            self.addCleanup(p.stop)

    def deliver(self, *events):
        async def go():
            for ev in events:
                await self.b.on_message(self.room, ev)
                await asyncio.gather(*list(self.b.tasks))
        asyncio.run(go())

    def ask(self):
        self.replies.append("Sure. Tell me a city or zip code?")
        self.deliver(msg(ANNA, "anna-agent what's the weather?", "$q", [AGENT]))
        self.assertEqual(len(self.sent), 1)

    def test_the_answer_after_unrelated_chatter_is_answered(self):
        self.ask()
        self.replies.append("Sunny in 95130.")
        self.deliver(msg(BETTY, "lunch anyone", "$b1"), msg(ANNA, "95130", "$a1"))
        self.assertEqual(self.sent[-1], "Sunny in 95130.")
        self.assertIn(bridge_followup.NO_REPLY, self.prompts[-1])           # it was told it may not be for it
        self.assertNotIn(ROOM, self.b.open_questions)                        # answered: closed

    def test_an_unrelated_owner_message_stays_silent(self):
        self.ask()
        self.replies.append("NO_REPLY")
        self.deliver(msg(ANNA, "betty, see you at 3", "$a1"))
        self.assertEqual(len(self.sent), 1)                                  # nothing posted
        self.b.client.room_typing.assert_awaited_with(ROOM, False)          # typing not left on
        self.assertIn(ROOM, self.b.open_questions)                           # still waiting for the answer

    def test_other_peoples_messages_never_trigger(self):
        self.ask()
        self.deliver(msg(BETTY, "95130", "$b1"))
        self.assertEqual((len(self.sent), self.prompts[1:]), (1, []))

    def test_talking_to_someone_else_is_not_an_answer(self):
        self.ask()
        self.deliver(msg(ANNA, "95130", "$a1", mentions=[BETTY]), msg(ANNA, "ok", "$a2", reply_to="$b0"))
        self.assertEqual(self.prompts[1:], [])

    def test_the_window_expires(self):
        self.ask()
        self.b.open_questions[ROOM]["at"] -= bridge_followup.OPEN_QUESTION_S + 1
        self.deliver(msg(ANNA, "95130", "$a1"))
        self.assertEqual(self.prompts[1:], [])
        self.assertNotIn(ROOM, self.b.open_questions)

    def test_at_most_a_few_messages_are_offered(self):
        self.ask()
        n = bridge_followup.MAX_CANDIDATES
        self.replies += ["NO_REPLY"] * n
        self.deliver(*(msg(ANNA, f"chat {i}", f"$a{i}") for i in range(n + 2)))
        self.assertEqual(len(self.prompts), 1 + n)

    def test_a_reply_that_asks_nothing_opens_no_question(self):
        self.replies.append("It's sunny.")
        self.deliver(msg(ANNA, "anna-agent weather?", "$q", [AGENT]), msg(ANNA, "thanks", "$a1"))
        self.assertEqual(len(self.prompts), 1)

    def test_queued_candidates_dont_run_once_the_question_is_answered(self):
        """GPT review 2: candidates queued behind the room lock are re-checked before they run."""
        self.ask()
        self.replies.append("Sunny.")

        async def go():
            for ev in (msg(ANNA, "95130", "$a1"), msg(ANNA, "or 95014", "$a2")):
                await self.b.on_message(self.room, ev)     # both admitted before either runs
            await asyncio.gather(*list(self.b.tasks))
        asyncio.run(go())
        self.assertEqual((len(self.prompts), self.sent[-1]), (2, "Sunny."))

    def test_a_refusal_note_or_progress_line_still_counts_as_no_reply(self):
        self.assertTrue(bridge_followup.silent("Checking…\n\nNO_REPLY\n\n⚠️ Refused: Bash"))

    def test_a_failed_follow_up_turn_posts_nothing(self):
        self.ask()

        async def boom(*a, **kw):
            raise bridge.agents.AgentError("claude exited 1")
        with mock.patch.object(bridge.agents, "run_turn", boom):
            self.deliver(msg(ANNA, "unrelated", "$a1"))
        self.assertEqual(len(self.sent), 1)

    def test_a_members_answer_doesnt_close_the_owners_question(self):
        self.ask()
        self.b.note_reply(ROOM, "$x", "ok.", BETTY)
        self.assertIn(ROOM, self.b.open_questions)

    def test_naming_another_agent_in_plain_text_is_not_an_answer(self):
        """GPT review 3: "anna-codex-agent, 95130" is for her other agent, not this one."""
        self.ask()
        self.deliver(msg(ANNA, "anna-codex-agent: 95130", "$a1"))
        self.assertEqual(self.prompts[1:], [])

    def test_a_message_for_the_server_agent_is_not_an_answer(self):
        """GPT review (server agent for everyone): "Server agent, what is a rebase?" is for it, not this one."""
        self.ask()
        self.deliver(msg(ANNA, "Server agent, what is a rebase?", "$a1"))
        self.assertEqual(self.prompts[1:], [])

    def test_a_queued_candidate_never_answers_a_newer_question(self):
        """GPT review 3: the queued turn carries the question it was offered for."""
        self.ask()
        asked = self.b.may_answer(self.room, msg(ANNA, "95130", "$a1"))
        self.b.note_reply(ROOM, "$newer", "And which day?", ANNA)          # a newer question replaced it
        asyncio.run(self.b.answer(self.room, msg(ANNA, "95130", "$a1"), "x", follow_up=asked))
        self.assertEqual(self.prompts[1:], [])

    def test_what_counts_as_asking(self):
        self.assertTrue(bridge_followup.asks("Which city?"))
        self.assertTrue(bridge_followup.asks("Which city? Or a zip code."))
        self.assertFalse(bridge_followup.asks("See https://x.com/?q=1 and `a?b`"))
        self.assertFalse(bridge_followup.asks("> did you mean x?\nNo."))
        self.assertTrue(bridge_followup.silent(" NO_REPLY. "))
        self.assertFalse(bridge_followup.silent("NO_REPLY but here is more"))


if __name__ == "__main__":
    unittest.main()
