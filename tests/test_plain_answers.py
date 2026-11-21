"""the owner: "asking a question and then reply to the question, that's all". An agent answers without
quoting the question (unless others spoke in between), and its 🔎 web requests go in a thread
under the question instead of the chat. Run: .venv/bin/python -m unittest tests.test_plain_answers"""
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("HAGENCY_AGENT", "anna-hermes-agent")
os.environ.setdefault("HAGENCY_ROOM", "!r:bridge.localhost")
from adapters.agents import bridge  # noqa: E402
from mcp_tools import mcp_core as core  # noqa: E402
from mcp_tools import mcp_web  # noqa: E402


class PlainAnswer(unittest.TestCase):
    def setUp(self):
        self.b = bridge.Bridge.__new__(bridge.Bridge)
        self.b.last_message = {}
        self.b.client = SimpleNamespace(room_send=mock.AsyncMock(return_value=SimpleNamespace(event_id="$a")))
        self.question = SimpleNamespace(event_id="$q", sender="@anna:x", source={"content": {}})

    def test_a_question_in_a_thread_is_answered_in_that_thread(self):
        """GPT review 15: an answer to a thread question landed in the main timeline."""
        self.question.source = {"content": {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}}}
        asyncio.run(self.b.send("!r", "The news…", reply_to=self.question))
        self.assertEqual((self.sent()["m.relates_to"]["rel_type"], self.sent()["m.relates_to"]["event_id"]),
                         ("m.thread", "$root"))

    def sent(self):
        return self.b.client.room_send.call_args.args[2]

    def test_answer_right_after_the_question_has_no_quote(self):
        asyncio.run(self.b.note_last(SimpleNamespace(room_id="!r"), SimpleNamespace(event_id="$q", source={"content": {}})))
        asyncio.run(self.b.send("!r", "The news…", reply_to=self.question))
        self.assertNotIn("m.relates_to", self.sent())
        self.assertEqual(self.sent()["m.mentions"], {"user_ids": ["@anna:x"]})   # anna is still notified

    def test_others_spoke_in_between_so_it_quotes(self):
        for ev in ("$q", "$betty-said-something"):
            asyncio.run(self.b.note_last(SimpleNamespace(room_id="!r"), SimpleNamespace(event_id=ev, source={"content": {}})))
        asyncio.run(self.b.send("!r", "The news…", reply_to=self.question))
        self.assertEqual(self.sent()["m.relates_to"], {"m.in_reply_to": {"event_id": "$q"}})

    def test_edits_dont_count_as_someone_speaking(self):
        asyncio.run(self.b.note_last(SimpleNamespace(room_id="!r"), SimpleNamespace(event_id="$q", source={"content": {}})))
        board_edit = SimpleNamespace(event_id="$e", source={"content": {"m.relates_to": {"rel_type": "m.replace"}}})
        asyncio.run(self.b.note_last(SimpleNamespace(room_id="!r"), board_edit))
        asyncio.run(self.b.send("!r", "The news…", reply_to=self.question))
        self.assertNotIn("m.relates_to", self.sent())


class WebInAThread(unittest.TestCase):
    def test_web_requests_go_in_a_thread_under_the_question(self):
        posted = []
        with mock.patch.object(mcp_web, "owners_asks", lambda: [{"room_id": "!r", "event_id": "$q"}]), \
                mock.patch.object(core, "api", lambda *a: posted.append(a[2])), mock.patch.object(core, "token", lambda: "t"):
            asyncio.run(mcp_web.note_request("!r", "search BBC news"))
        self.assertEqual(posted[0]["m.relates_to"]["rel_type"], "m.thread")
        self.assertEqual(posted[0]["m.relates_to"]["event_id"], "$q")
        self.assertIn("🔎 web: search BBC news", posted[0]["body"])          # still shown: only moved


if __name__ == "__main__":
    unittest.main()
