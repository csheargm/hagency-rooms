"""Unit tests: the trial agent's model call to Ollama Cloud, against a local fake of its /api/chat
(ADR 0025). Run: .venv/bin/python -m unittest tests.test_trial_model"""
import asyncio
import os
import unittest
from unittest import mock

from aiohttp import web

from server import trial_model

KEY = "ollama-test-key-0123456789"


class Model(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.got, self.answer, self.status, self.delay = [], {"message": {"role": "assistant", "content": "Hello there."}}, 200, 0

        async def chat(request):
            self.got.append((dict(request.headers), await request.json()))
            await asyncio.sleep(self.delay)
            return web.json_response(self.answer, status=self.status)
        app = web.Application()
        app.add_routes([web.post("/api/chat", chat)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        env = mock.patch.dict(os.environ, {"OLLAMA_API_KEY": KEY, "TRIAL_API_URL": f"http://127.0.0.1:{port}/api/chat",
                                           "TRIAL_MODELS": "glm-5.3,kimi-k2.7-code", "TRIAL_MAX_TOKENS": "200", "TRIAL_TIMEOUT_S": "2"})
        env.start()
        self.addCleanup(env.stop)

    async def asyncTearDown(self):
        await self.runner.cleanup()

    async def test_one_chat_call_with_the_key_the_model_and_the_reply_cap(self):
        answer = await trial_model.ask(trial_model.messages([], "What is a pull request?"))
        self.assertEqual(answer, "Hello there.")
        headers, body = self.got[0]
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual((body["model"], body["stream"], body["options"]["num_predict"]), ("glm-5.3", False, 200))
        self.assertNotIn("tools", body)                  # talk only: the model is offered no tools
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("cannot run code", body["messages"][0]["content"])
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "What is a pull request?"})

    async def test_an_empty_answer_is_a_failure_never_a_blank_reply(self):
        for empty in ({"message": {"content": "  "}}, {"message": "x"}, {}, []):
            self.answer = empty
            with self.assertRaises(trial_model.TrialModelError):
                await trial_model.ask(trial_model.messages([], "hi"))

    async def test_a_refusal_or_timeout_never_shows_the_key(self):
        self.status, self.answer = 401, {"error": f"bad key {KEY}"}
        with self.assertRaises(trial_model.TrialModelError) as e:
            await trial_model.ask(trial_model.messages([], "hi"))
        self.assertNotIn(KEY, str(e.exception))
        self.assertIn("401", str(e.exception))
        self.status, self.answer, self.delay = 200, {"message": {"content": "late"}}, 3
        with self.assertRaises(trial_model.TrialModelError) as e:
            await trial_model.ask(trial_model.messages([], "hi"))
        self.assertIn("in time", str(e.exception))

    async def test_secrets_are_cut_both_ways(self):
        token = "ghp_" + "A" * 30
        chat = trial_model.messages([{"role": "user", "content": f"old {token}"}, {"role": "system", "content": "x"}],
                                    f"my key is {token}", diff=None)
        self.assertNotIn(token, str(chat))
        self.assertEqual([m["role"] for m in chat], ["system", "user", "user"])   # a smuggled "system" turn is dropped
        self.answer = {"message": {"content": f"here: {token} and /Users/someone/secret.txt"}}
        answer = await trial_model.ask(chat)
        self.assertNotIn(token, answer)
        self.assertNotIn("/Users/someone", answer)

    async def test_a_long_message_is_cut(self):
        chat = trial_model.messages([], "x" * 10_000)
        self.assertEqual(len(chat[-1]["content"]), trial_model.MAX_INPUT_CHARS)

    async def test_a_diff_is_labelled_as_data(self):
        chat = trial_model.messages([], "review it", diff="--- a.py\n+print(1)")
        self.assertIn("read-only data", chat[-1]["content"])
        self.assertIn("+print(1)", chat[-1]["content"])


if __name__ == "__main__":
    unittest.main()
