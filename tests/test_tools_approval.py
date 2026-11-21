"""Unit tests: tools beyond the project, with the owner's say-so. Run: .venv/bin/python -m unittest tests.test_tools_approval"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("HAGENCY_AGENT", "anna-agent")
from adapters.agents import agents  # noqa: E402
from common import intents  # noqa: E402
from mcp_tools import mcp_core as core  # noqa: E402
from mcp_tools import mcp_web  # noqa: E402
from server.loop import Loop  # noqa: E402

S = "bridge.localhost"
ANNA, BETTY, AGENT = f"@anna:{S}", f"@betty:{S}", f"@anna-agent:{S}"


class RetiredWebSwitch(unittest.TestCase):
    """ADR 0006 retired the per-room web grant: the owner's own turns always have the web, loop work
    never does. The old phrases still reach @pm, which explains instead of pretending to switch."""
    def test_phrases_are_recognised_but_change_nothing(self):
        for text in ("let my agent search the web here", "stop letting my agent use the web",
                     "enable web search for my agent", "can you enable web search for my agent?",
                     "disable web search for my agent", "turn off web search for my agent"):
            self.assertEqual(intents.classify(text), intents.WEB, text)
        self.assertNotIn(intents.WEB, intents.SETTINGS)          # never asks for a 👍: nothing to confirm
        self.assertEqual(intents.classify("stop"), intents.OFF)

    def test_a_question_is_not_the_old_switch(self):
        self.assertNotEqual(intents.classify("should I let my agent search the web?"), intents.WEB)

    def test_pm_explains_and_writes_no_state(self):
        lp, said = Loop.__new__(Loop), []

        async def say(room_id, body, *a, **k):
            said.append(body)
        lp.say, lp.client = say, SimpleNamespace(room_put_state=mock.AsyncMock())
        asyncio.run(lp.act(SimpleNamespace(room_id="!r"), BETTY, intents.WEB))
        self.assertIn("no web switch", said[-1])
        lp.client.room_put_state.assert_not_called()
        self.assertFalse(hasattr(lp, "set_grant"))


class WebTools(unittest.TestCase):
    def test_the_owners_room_needs_no_grant(self):
        """ADR 0006: no per-room grant gates the hagency web tools; the owner's room is enough."""
        with mock.patch.object(mcp_web, "owners_room", lambda: "!r"), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mcp_web.LOOP_ONLY_ENV, None)
            self.assertEqual(asyncio.run(mcp_web.web_room()), ("!r", None))

    def test_loop_work_never_has_the_web(self):
        with mock.patch.object(mcp_web, "owners_room", lambda: "!r"), mock.patch.dict(os.environ, {mcp_web.LOOP_ONLY_ENV: "1"}):
            self.assertIn("no web access", asyncio.run(mcp_web.web_search("x"))["error"])

    def test_refused_when_the_room_is_ambiguous(self):
        with mock.patch.object(mcp_web, "owners_room", lambda: None):
            self.assertIn("can't tell which room", asyncio.run(mcp_web.fetch_page("https://example.com"))["error"])

    def test_never_this_machine_or_its_network(self):
        from common import safe_fetch
        for url in ("http://localhost:8648/_synapse/admin/v1/users", "http://127.0.0.1:8650/data", "http://2130706433/",
                    "http://[::ffff:127.0.0.1]/", "http://[::1]/", "http://192.168.1.1/", "http://224.0.0.1/",
                    "http://169.254.169.254/latest/meta-data", "file:///etc/passwd"):
            with self.assertRaises(safe_fetch.Refused, msg=url):
                safe_fetch.fetch(url)

    def test_each_hop_connects_to_the_address_that_was_checked(self):
        """GPT review 8: the check and the connection resolved the name separately (DNS rebinding).
        Now _one_hop resolves once and connects to that exact IP; a redirect hop is resolved anew."""
        from common import safe_fetch
        connected = []

        class Conn:
            def __init__(self, host, port=None, timeout=None, **kw):
                connected.append(host)

            def request(self, *a, **k):
                pass

            def getresponse(self):
                return SimpleNamespace(status=302, getheader=lambda h: "http://rebind.example/x", read=lambda n: b"")

            def close(self):
                pass
        with mock.patch.object(safe_fetch, "resolve", side_effect=["93.184.216.34", safe_fetch.Refused("private")]), \
                mock.patch.object(safe_fetch.http.client, "HTTPConnection", Conn):
            with self.assertRaises(safe_fetch.Refused):
                safe_fetch.fetch("http://example.com/")
        self.assertEqual(connected, ["93.184.216.34"])           # the checked IP, never the name again
        self.assertIsNone(safe_fetch.public_ip("::ffff:10.0.0.1"))

    def test_page_text(self):
        self.assertEqual(mcp_web.page_text("<p>Hi <b>there</b></p><script>evil()</script>"), "Hi there")


class AskOwner(unittest.TestCase):
    def run_with(self, reactions, wait=0.05, owner=ANNA, action="write the plan into the team's Google Doc"):
        def api(method, path, body=None, token=None):
            if method == "PUT":
                return {"event_id": "$ask"}
            return {"chunk": [{"sender": s, "content": {"m.relates_to": {"key": k}}} for s, k in reactions]}

        async def no_wait(s):
            return None
        with mock.patch.object(core, "api", api), mock.patch.object(core, "token", lambda: "t"), \
                mock.patch.object(mcp_web, "owners_room", lambda: "!r"), mock.patch.object(core, "owner_id", lambda: owner), \
                mock.patch.object(mcp_web, "APPROVAL_WAIT_S", wait), mock.patch.object(mcp_web.asyncio, "sleep", no_wait):
            return asyncio.run(mcp_web.ask_owner(action))

    def test_the_owners_ok_approves_exactly_that_request(self):
        out = self.run_with([(ANNA, "✅")], wait=1)
        self.assertEqual((out["approved"], out["for"]), (True, "write the plan into the team's Google Doc"))

    def test_someone_elses_ok_doesnt(self):
        self.assertFalse(self.run_with([(BETTY, "✅")])["approved"])

    def test_a_denial_wins_and_silence_means_no(self):
        self.assertIn("said no", self.run_with([(ANNA, "✅"), (ANNA, "👎")], wait=1)["why"])
        self.assertIn("didn't answer", self.run_with([])["why"])

    def test_an_agent_cant_be_its_own_owner_or_hide_a_long_request(self):
        self.assertFalse(self.run_with([(core.USER_ID, "✅")], owner=core.USER_ID)["approved"])
        self.assertIn("under", self.run_with([(ANNA, "✅")], action="x" * 1200)["why"])


class BridgeWeb(unittest.TestCase):
    def test_locked_turns_never_get_web_and_owner_turns_use_the_owners_own_settings(self):
        """ADR 0006: no per-room web grant for the agent adapter's CLI any more. A locked turn (loop task,
        @pm's brain, a member's question) has no web; the owner's own turn always has web (unless they disabled it)."""
        self.assertNotIn("WebSearch", " ".join(agents._claude_argv(Path("/w"), None)))
        self.assertNotIn("--tools", agents._claude_argv(Path("/w"), None, owner=True))
        with mock.patch.dict("os.environ", {"CODEX_HOME": tempfile.mkdtemp()}):
            self.assertIn('web_search="disabled"', agents._codex_argv(Path("/w"), None))
            self.assertIn('web_search="live"', agents._codex_argv(Path("/w"), None, owner=True))   # no grant needed


if __name__ == "__main__":
    unittest.main()
