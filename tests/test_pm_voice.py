"""Voice messages: each listener (@pm, each agent) hears only what is addressed to it.
Run: .venv/bin/python -m unittest tests.test_pm_voice"""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from adapters.agents import bridge
from server import loop
from common import voice

PM, ANNA, BETTY, BETTY_AGENT = "@pm:x", "@anna:x", "@betty:x", "@betty-agent:x"


def audio(words_reply_to=None, duration=4000, sender=ANNA):
    content = {"msgtype": "m.audio", "info": {"duration": duration}}
    if words_reply_to:
        content["m.relates_to"] = {"m.in_reply_to": {"event_id": words_reply_to}}
    return SimpleNamespace(sender=sender, url="mxc://x/abc", event_id="$v", source={"content": content})


class PmHears(unittest.TestCase):
    def setUp(self):
        self.lp = loop.Loop.__new__(loop.Loop)
        self.lp.user_id, self.lp.pending, self.lp.busy = PM, {}, set()
        self.lp.client = SimpleNamespace(room_get_event=mock.AsyncMock(return_value=SimpleNamespace(event=SimpleNamespace(sender=ANNA))))
        self.said, self.heard, self.replies = [], [], []

        async def say(room_id, body, mention=None, task=None, **kw):
            self.said.append(body)
            self.replies.append(kw.get("reply_to"))
            return "$e"

        async def on_human(room, event):
            self.heard.append((event.body, event.source["content"]["m.mentions"]["user_ids"]))
        self.lp.say, self.lp.on_human = say, on_human
        self.room = SimpleNamespace(room_id="!r", users={PM: 0, ANNA: 0, BETTY: 0, BETTY_AGENT: 0})

    def hear(self, words, ev=None):
        with mock.patch.object(voice, "listen", mock.AsyncMock(return_value=words)):
            asyncio.run(self.lp.hear(self.room, ev or audio()))

    def test_saying_pm_first_is_for_pm_and_shows_a_caption(self):
        self.hear("PM, who are you?")
        self.assertEqual(self.said, ["📝 Transcript: “PM, who are you?”"])
        self.assertEqual(self.replies, ["$v"])              # attached to the voice message
        self.assertEqual(self.heard, [("who are you?", [PM])])

    def test_a_question_for_bettys_agent_is_none_of_pms_business(self):
        """the owner: "I'm asking betty's agent, why pm is involved?" """
        self.hear("Betty's agent, what is the time now? It's 3 pm here.")
        self.assertEqual((self.said, self.heard), ([], []))

    def test_people_talking_gets_nothing_from_pm(self):
        self.hear("Let's meet tomorrow about the toggle.")
        self.assertEqual((self.said, self.heard), ([], []))

    def test_a_voice_reply_to_pm_is_for_pm(self):
        self.lp.client.room_get_event = mock.AsyncMock(return_value=SimpleNamespace(event=SimpleNamespace(sender=PM)))
        self.hear("Who are you?", audio(words_reply_to="$pm"))
        self.assertEqual(self.heard, [("Who are you?", [PM])])

    def test_ok_go_ahead_to_a_waiting_question_needs_no_name(self):
        self.lp.pending["!r"] = {"kind": "plan", "event_id": "$p", "until": time.time() + 60, "by": ANNA}
        self.hear("Okay, go ahead.")
        self.assertEqual(self.heard, [("Okay, go ahead.", [PM])])  # on_human then asks for the 👍 click

    def test_too_long_is_refused_without_downloading(self):
        client = SimpleNamespace(download=mock.AsyncMock())
        with self.assertRaises(voice.VoiceError):
            asyncio.run(voice.listen(client, audio(duration=10 * 60 * 1000)))
        client.download.assert_not_called()

    def test_no_speech_to_text_says_so(self):
        with mock.patch.object(voice, "STT", None):
            with self.assertRaises(voice.VoiceError):
                asyncio.run(voice.transcribe(b"x"))


class AgentHears(unittest.TestCase):
    """Betty's agent hears "Betty's agent, …" itself; @pm isn't involved."""
    def setUp(self):
        self.b = bridge.Bridge.__new__(bridge.Bridge)
        self.b.owner, self.b.owner_name, self.b.owner_id, self.b.user_id = "betty", "betty", BETTY, BETTY_AGENT
        self.b.allow_members, self.b.tasks, self.b.refused, self.b.last_message = False, set(), {}, {}
        self.b.client = SimpleNamespace(room_get_event=mock.AsyncMock(return_value=None),
                                        room_send=mock.AsyncMock(return_value=SimpleNamespace(event_id="$a")))
        self.answered = []

        async def answer_locked(room, event, line):
            self.answered.append(line)
        self.b.answer_locked = answer_locked
        self.room = SimpleNamespace(room_id="!r", users={PM: 0, ANNA: 0, BETTY: 0, BETTY_AGENT: 0}, invited_users={},
                                    joined_count=4, invited_count=0, user_name=lambda u: u.split(":")[0][1:].capitalize())

    def hear(self, words, sender, reply_to=None):
        async def go():
            with mock.patch.object(voice, "listen", mock.AsyncMock(return_value=words)):
                await self.b.hear(self.room, audio(words_reply_to=reply_to, sender=sender))
                await asyncio.gather(*self.b.tasks)
        asyncio.run(go())

    def sent(self):
        return [c.args[2]["body"] for c in self.b.client.room_send.call_args_list]

    def test_the_owner_asks_by_name_and_gets_an_answer(self):
        self.hear("Betty's agent, what is the time now?", BETTY)
        self.assertEqual(self.answered, ["Betty (@betty:x) said by voice: Betty's agent, what is the time now?"])

    def test_someone_else_replying_by_voice_is_told_why_not_once(self):
        """Seen live: anna asked Betty's agent and nothing happened at all."""
        self.b.client.room_get_event = mock.AsyncMock(return_value=SimpleNamespace(event=SimpleNamespace(sender=BETTY_AGENT)))
        self.hear("What is the time now?", ANNA, reply_to="$agent-msg")
        self.hear("Are you there?", ANNA, reply_to="$agent-msg")
        self.assertEqual(self.answered, [])
        self.assertEqual(self.sent(), ["I only take requests from betty."])   # as written, never "Betty"

    def test_the_refusal_uses_the_owners_display_name_as_they_set_it(self):
        """Seen live: cx was called "Cx". A display name is used exactly, and can't add markdown."""
        self.room.users[BETTY] = SimpleNamespace(display_name="bé *T*")
        self.b.client.room_get_event = mock.AsyncMock(return_value=SimpleNamespace(event=SimpleNamespace(sender=BETTY_AGENT)))
        self.hear("What is the time now?", ANNA, reply_to="$agent-msg")
        self.assertEqual(self.sent(), ["I only take requests from bé *T*."])
        self.assertNotIn("<em>", self.b.client.room_send.call_args.args[2]["formatted_body"])

    def test_someone_elses_voice_not_for_me_is_never_transcribed(self):
        """GPT review 15: a non-owner's clips used this computer's transcription before any check."""
        with mock.patch.object(voice, "listen", mock.AsyncMock()) as listen:
            asyncio.run(self.b.hear(self.room, audio(sender=ANNA)))
        listen.assert_not_called()

    def test_named_only_at_the_start(self):
        self.assertTrue(self.b.named_me("Hey Betty's agent, what time is it?"))
        self.assertFalse(self.b.named_me("PM, tell Betty's agent to stop"))

    def test_not_named_means_not_for_me(self):
        self.hear("PM, who are you?", BETTY)
        self.assertEqual((self.answered, self.sent()), ([], []))


if __name__ == "__main__":
    unittest.main()
