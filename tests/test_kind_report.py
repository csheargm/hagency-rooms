"""An agent tells @pm which AI it runs, and "My agents" shows it (common/kind_report.py,
server/pm_kind_reports.py, docs/adr/0008-agent-kinds.md). The flow: the agent's adapter signs in on
its person's computer and sends one to-device message from its credentials.json; @pm, fed that same
message as nio parses it, records it on the sender's own entry. Never an API key.
Run: .venv/bin/python -m unittest tests.test_kind_report"""
import asyncio
import json
from types import SimpleNamespace
from unittest import mock

from nio import ToDeviceEvent, ToDeviceResponse, UnknownToDeviceEvent

from common import kind_report
from common import pm_kinds
from common import registry
from server import console
from server import pm_kind_reports
from tests import test_more_agents_flows as flows
from tests.test_more_agents_flows import BA, CX, CXA, CXC, PM, S

OPENROUTER = "https://openrouter.ai/api/v1"
KEY = "sk-proj-abcdef0123456789abcdefXYZ"
# What connect.py saved on the owner's computer for cx-agent (added with "the one on my computer").
CX_CREDS = {"homeserver": "https://matrix.example", "user_id": CXA, "password": "the-sign-in-password",
            "owner": "cx", "agent": "claude"}


class FakeClient:
    """The agent adapter's nio client: what it sends to @pm, as Synapse would deliver it."""
    def __init__(self, user_id, fail=None, hang=False):
        self.user_id, self.fail, self.hang, self.sent = user_id, fail, hang, []

    async def to_device(self, message, tx_id=None):
        if self.fail:
            raise self.fail
        if self.hang:                         # nio retrying a 429 without end
            await asyncio.sleep(3600)
        self.sent.append(message)
        return ToDeviceResponse(message)

    def delivered(self):
        """Each message as @pm's sync parses it (Synapse sets "sender" to the signed-in account)."""
        return [ToDeviceEvent.parse_event({"sender": self.user_id, "type": m.type, "content": m.content}) for m in self.sent]


class Reports(flows.Base):
    def card(self):
        loop = SimpleNamespace(agent_online=lambda a: None, ever_connected=lambda a: True)
        return {a["name"]: a["runs"] for a in console.my_agents(loop, SimpleNamespace(users={}, invited_users={}), "!r", CX)}

    def report(self, sender, content):
        event = isinstance(content, dict) and ToDeviceEvent.parse_event({"sender": sender, "type": kind_report.EVENT_TYPE,
                                                                         "content": content})
        if isinstance(event, UnknownToDeviceEvent):
            asyncio.run(self.lp.on_kind_report(event))
        else:                                 # nio never hands this one to the callback: the check behind it, directly
            pm_kind_reports.record(sender, content)

    def start_agent(self, creds, sender=CXA):
        """The adapter's start on the person's computer, then @pm receiving what it sent."""
        client = FakeClient(sender)
        self.assertTrue(asyncio.run(kind_report.send(client, PM, creds)))
        self.assertEqual([(m.recipient, m.recipient_device) for m in client.sent], [(PM, "*")])
        for event in client.delivered():
            self.assertIsInstance(event, UnknownToDeviceEvent)   # what listen_early's callback filters on
            asyncio.run(self.lp.on_kind_report(event))
        return client

    def test_an_agent_added_with_no_kind_reports_claude_and_shows_claude_code(self):
        self.assertEqual(self.card()["cx-agent"], pm_kinds.NOT_REPORTED)
        client = self.start_agent(CX_CREDS)
        self.assertEqual(client.sent[0].content, {"agent_kind": "claude"})   # nothing else from the file
        self.assertNotIn("the-sign-in-password", json.dumps(client.sent[0].content))
        self.assertEqual(self.card()["cx-agent"], "Claude Code")
        before = registry.PATH.stat().st_mtime_ns
        self.start_agent(CX_CREDS)                                         # every start: no change, no write
        self.assertEqual(registry.PATH.stat().st_mtime_ns, before)
        self.assertEqual(registry.find(CX, None)["reported"], {"agent_kind": "claude"})

    def test_ollama_and_remote_show_as_describe_does(self):
        registry.add_agent(CX, "codex")
        self.start_agent({**CX_CREDS, "agent": "ollama", "model": "llama3.2", "base_url": OPENROUTER})   # stray URL: not sent
        sent = self.start_agent({**CX_CREDS, "user_id": CXC, "label": "codex", "agent": "remote", "model": "openai/gpt-4o",
                                 "base_url": "https://OpenRouter.ai/secret-path/api/v1"}, sender=CXC).sent[0].content
        self.assertEqual(sent, {"agent_kind": "remote", "model": "openai/gpt-4o", "host": "openrouter.ai"})   # never the path
        self.assertEqual(self.card(), {"cx-agent": "Ollama (a model on your computer): llama3.2",
                                       "cx-codex-agent": "Remote: openai/gpt-4o at openrouter.ai"})
        self.assertNotIn("secret-path", registry.PATH.read_text())
        self.start_agent({**CX_CREDS, "agent": "ollama", "model": "Qwen3CoderNextInstruct"})   # a long local name (GPT review)
        self.assertEqual(self.card()["cx-agent"], "Ollama (a model on your computer): Qwen3CoderNextInstruct")

    def test_what_runs_is_shown_but_the_pick_and_the_controls_stay_ats(self):
        registry.update(CX, None, agent_kind="ollama", model="llama3.2")
        self.start_agent({**CX_CREDS, "agent": "ollama", "model": "qwen3:8b"})
        self.start_agent(CX_CREDS)                                         # switched to Claude Code: the old model goes
        found = registry.find(CX, None)
        self.assertEqual(found["reported"], {"agent_kind": "claude"})
        self.assertEqual(self.card()["cx-agent"], "Claude Code")
        self.assertEqual((found["agent_kind"], found["model"]), ("ollama", "llama3.2"))   # a new code still offers the pick
        self.start_agent({**CX_CREDS, "agent": "hermes"})                  # a sign-in claiming Hermes (GPT review):
        loop = SimpleNamespace(agent_online=lambda a: None, ever_connected=lambda a: True)
        mine = console.my_agents(loop, SimpleNamespace(users={}, invited_users={}), "!r", CX)
        self.assertEqual((mine[0]["runs"], mine[0]["hermes"]), ("Hermes", False))   # shown; Stop / Start stay

    def test_invalid_kind_model_or_address_is_refused(self):
        for bad in ({"agent_kind": "gpt5"}, {}, {"agent_kind": "ollama", "model": "bad model"},
                    {"agent_kind": "ollama", "model": "../../etc"}, {"agent_kind": "claude", "model": "x"},
                    {"agent_kind": "remote", "model": "m", "base_url": OPENROUTER},   # the whole address: never taken
                    {"agent_kind": "remote", "model": "m", "host": "u:p@x.example"},
                    {"agent_kind": "remote", "model": "m", "host": "x.example/v1"},
                    {"agent_kind": "remote", "model": "m", "host": "x.example:8443"},
                    {"agent_kind": "remote", "model": "m", "host": "x.example?a=1"}, {"agent_kind": "remote", "model": "m", "host": "a b"},
                    {"agent_kind": "remote", "model": "m"}, {"agent_kind": "codex", "host": "openrouter.ai"},
                    {"agent_kind": ["claude"]}, "claude", None):
            with self.assertLogs("loop", "WARNING"):
                self.report(CXA, bad)
            self.assertEqual(self.card()["cx-agent"], pm_kinds.NOT_REPORTED, bad)
            self.assertIsNone(pm_kinds.reported(bad), bad)
        self.assertEqual(pm_kinds.reported({"agent_kind": "remote", "model": "m", "host": "::1"})["host"], "::1")

    def test_no_key_is_ever_accepted_or_stored(self):
        for keyed in ({"agent_kind": "remote", "model": "m", "host": "openrouter.ai", "api_key": KEY},
                      {"agent_kind": "claude", "key": KEY}, {"agent_kind": "remote", "model": KEY, "host": "openrouter.ai"},
                      {"agent_kind": "remote", "model": "m", "host": f"{KEY}.example"}):
            with self.assertLogs("loop", "WARNING") as logs:
                self.report(CXA, keyed)
            self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn(KEY, registry.PATH.read_text())
        self.assertEqual(self.card()["cx-agent"], pm_kinds.NOT_REPORTED)
        sent = kind_report.from_credentials({**CX_CREDS, "agent": "remote", "model": "m", "base_url": OPENROUTER,
                                             "api_key": KEY, "HAGENCY_REMOTE_API_KEY": KEY})
        self.assertEqual(sent, {"agent_kind": "remote", "model": "m", "host": "openrouter.ai"})   # only these fields

    def test_another_account_cant_set_someone_elses_agent(self):
        for sender in (BA, CX, PM, f"@cx-agent:evil.example", "@cx-agent", "cx-agent", None):
            self.report(sender, {"agent_kind": "codex"})
        self.assertEqual(self.card()["cx-agent"], pm_kinds.NOT_REPORTED)
        self.assertEqual(registry.find(flows.BETTY, None)["reported"], {"agent_kind": "codex"})   # Betty's agent: its own entry

    def test_not_reported_yet_only_for_agents_on_a_persons_computer(self):
        registry.update(CX, None, agent="server")
        self.assertEqual(self.card()["cx-agent"], "")                 # run by this server: no badge, as before
        self.assertEqual(pm_kinds.NOT_REPORTED, "AI: not reported yet")

    def test_no_kind_in_the_file_or_a_failed_send_is_quiet(self):
        self.assertFalse(asyncio.run(kind_report.send(FakeClient(CXA), PM, {**CX_CREDS, "agent": None})))
        self.assertFalse(asyncio.run(kind_report.send(FakeClient(CXA), PM, {})))   # a server-run adapter: no file
        with self.assertLogs("bridge", "WARNING"):
            self.assertFalse(asyncio.run(kind_report.send(FakeClient(CXA, fail=OSError("down")), PM, CX_CREDS)))
        with mock.patch.object(kind_report, "SEND_TIMEOUT_S", 0.01), self.assertLogs("bridge", "WARNING"):   # GPT review:
            self.assertFalse(asyncio.run(kind_report.send(FakeClient(CXA, hang=True), PM, CX_CREDS)))   # never holds up the start

    def test_pm_listens_before_its_first_sync(self):
        seen = []
        self.lp.client = SimpleNamespace(add_event_callback=lambda cb, kind: seen.append(kind.__name__),
                                         add_to_device_callback=lambda cb, kind: seen.append(kind.__name__))
        self.lp.listen_early()
        self.assertEqual(seen, ["InviteMemberEvent", "UnknownToDeviceEvent"])
        other = ToDeviceEvent.parse_event({"sender": CXA, "type": "m.something", "content": {"agent_kind": "codex"}})
        asyncio.run(self.lp.on_kind_report(other))                  # another type: never a report
        self.assertEqual(self.card()["cx-agent"], pm_kinds.NOT_REPORTED)
        self.assertEqual(S, CXA.partition(":")[2])


if __name__ == "__main__":
    import unittest
    unittest.main()
