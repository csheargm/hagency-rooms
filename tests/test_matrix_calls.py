"""Unit tests: Matrix calls whose expected "no" stays out of the log (matrix_calls.py).
Run: .venv/bin/python -m unittest tests.test_matrix_calls"""
import asyncio
import json
import logging
import unittest
from unittest import mock

from nio import AsyncClient

from common import matrix_calls


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.text, self.released = status, body if isinstance(body, str) else json.dumps(body), False

    async def json(self, content_type=None):
        return json.loads(self.text)

    def release(self):
        self.released = True


def client_answering(status, body):
    client = AsyncClient("http://127.0.0.1:1", "@pm:x")
    client.access_token = "tok"
    resp = FakeResponse(status, body)
    client.send = mock.AsyncMock(return_value=resp)
    return client, resp


def call(coro_fn, client, arg):
    """Run it, and fail if nio (or anything) logged a warning on the way: the noise seen live."""
    with unittest.TestCase().assertNoLogs(level=logging.WARNING):
        return asyncio.run(coro_fn(client, arg))


class Quiet(unittest.TestCase):
    def test_the_lobby_before_it_exists_is_quiet(self):
        """Seen live at @pm's start: "Error validating response: 'room_id' is a required property"."""
        client, resp = client_answering(404, {"errcode": "M_NOT_FOUND", "error": "Room alias #lobby:x not found"})
        self.assertIsNone(call(matrix_calls.room_of_alias, client, "#lobby:x"))
        self.assertEqual(client.send.call_args.args[:2], ("GET", "/_matrix/client/v3/directory/room/%23lobby%3Ax"))
        self.assertEqual(client.send.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})
        self.assertTrue(resp.released)

    def test_an_existing_alias_gives_its_room(self):
        client, _ = client_answering(200, {"room_id": "!l:x", "servers": ["x"]})
        self.assertEqual(call(matrix_calls.room_of_alias, client, "#lobby:x"), "!l:x")

    def test_a_deactivated_accounts_presence_is_quiet(self):
        """Seen live: "Error validating response: 'presence' is a required property" every minute."""
        for status, body in ((200, {}), (404, {"errcode": "M_UNKNOWN"}), (403, {"errcode": "M_FORBIDDEN"})):
            client, _ = client_answering(status, body)
            self.assertIsNone(call(matrix_calls.presence, client, "@pubtest1-agent:x"), (status, body))

    def test_presence(self):
        for body, online in (({"presence": "online"}, True), ({"presence": "offline"}, False),
                             ({"presence": "unavailable", "currently_active": True}, True)):
            client, _ = client_answering(200, body)
            self.assertEqual(call(matrix_calls.presence, client, "@a-agent:x"), online)

    def test_directory_listing(self):
        for body, listed in (({"visibility": "public"}, True), ({"visibility": "private"}, False)):
            client, resp = client_answering(200, body)
            self.assertEqual(call(matrix_calls.in_directory, client, "!l:x"), listed)
            self.assertEqual(client.send.call_args.args[:2], ("GET", "/_matrix/client/v3/directory/list/room/%21l%3Ax"))
        for status, body in ((200, {}), (404, {"errcode": "M_NOT_FOUND"}), (502, "<html>bad gateway</html>")):
            client, resp = client_answering(status, body)
            with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):              # unknown: never a guess
                asyncio.run(matrix_calls.in_directory(client, "!l:x"))
            self.assertTrue(resp.released)

    def test_real_errors_are_raised_for_the_caller_to_log(self):
        for fn, arg in ((matrix_calls.room_of_alias, "#lobby:x"), (matrix_calls.presence, "@a-agent:x")):
            for status, body in ((500, {"errcode": "M_UNKNOWN"}), (502, "<html>bad gateway</html>"), (200, {"odd": 1}),
                                 (404, "<html>no route</html>")):   # GPT: a proxy's 404 never means "create it"
                if fn is matrix_calls.presence and status == 404:
                    continue                  # presence: any 404 means no such account
                if fn is matrix_calls.presence and status == 200:
                    continue                  # an answer without "presence": the deactivated case above
                client, resp = client_answering(status, body)
                with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                    asyncio.run(fn(client, arg))
                self.assertTrue(resp.released)


if __name__ == "__main__":
    unittest.main()
