"""User flow across the three changes of batch 12–14 (R6): a person asks @pm for another agent of a
kind (in the chat or with the panel's picker), @pm sends that agent's Connect page and line, the
person runs connect.py with the code, POST /connect answers with the kind (agent_kind / model /
base_url, ADR 0008), and connect.py runs that kind. Only the network edges are faked: the account
set-up (Synapse), the urlopen from connect.py to @pm (it gets the real /connect answer) and the
service manager. Run: .venv/bin/python -m unittest tests.test_kind_flow"""
import asyncio
import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from common import agent_kinds
from server import console
from server import pairing
from server import registry_removal
from server import remote_agent
from tests import test_connect_kinds as on_computer
from tests import test_more_agents_flows as flows
from tests.test_more_agents_flows import CX, text

OPENROUTER = "https://openrouter.ai/api/v1"
CODE_RE = re.compile(r"`([A-Z2-9]{5}-[A-Z2-9]{5})`")


class AnyPassword(dict):
    """The server's .env: every agent account @pm made has a password (setup_accounts is faked)."""
    def get(self, key, default=None):
        return "agent-pw"


class KindFlow(flows.Provisioning):
    def setUp(self):
        super().setUp()
        for target, name, value in ((remote_agent, "CODES", pairing.Codes(Path(tempfile.mkdtemp()) / "codes.json")),
                                    (remote_agent, "LIMIT", pairing.Limiter()),
                                    (remote_agent, "load_env", lambda: AnyPassword())):
            p = mock.patch.object(target, name, value)      # real codes: the one @pm sends is the one redeemed
            p.start()
            self.addCleanup(p.stop)
        self.computer = on_computer.Flow()                   # the person's computer (connect.py's own harness)
        self.computer.setUp()

    def tell(self, words: str) -> str:
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(CX, words)))
        return self.said[-1][1]

    def connect_answer(self, code: str) -> dict:
        """POST /connect as connect.py sends it, through @pm's real route."""
        async def go():
            app = web.Application()
            app.add_routes(remote_agent.routes())
            async with TestClient(TestServer(app)) as client:
                r = await client.post("/connect", json={"code": code})
                self.assertEqual(r.status, 200, await r.text())
                return await r.json()
        return asyncio.run(go())

    def run_on_computer(self, message: str, *argv):
        """The person copies the code from @pm's message and runs the line; connect.py gets the real answer."""
        code = CODE_RE.search(message).group(1)
        answer = self.connect_answer(code)
        install, said, secret, failed = self.computer.run_connect(code, *argv, answer=answer)
        self.assertIsNone(failed, said)
        saved, = self.computer.home.rglob("credentials.json")   # a further agent: in its own folder (connect.agent_dir)
        return answer, install, json.loads(saved.read_text()), secret

    def assert_message_names(self, message: str, kind: str | None):
        link = re.search(r"\((https?://[^)]+/connect/start#[^)]+)\)", message).group(1)
        line = re.search(r"`(curl -fsSL [^`]+)`", message).group(1)
        if kind:
            self.assertTrue(link.endswith(f"&kind={kind}"), link)            # the page shows that kind's checklist
            self.assertTrue(line.endswith(f" --agent {kind}"), line)         # and the same line the page shows
        else:
            self.assertNotIn("&kind=", link)
            self.assertNotIn("--agent", line)


class EachKindFromTheChat(KindFlow):
    CASES = (("pi", "ollama qwen3:8b", {"agent_kind": "ollama", "model": "qwen3:8b"}),
             ("cl", "claude", {"agent_kind": "claude"}),
             ("cx2", "codex", {"agent_kind": "codex"}),
             ("he", "hermes", {"agent_kind": "hermes"}),
             ("rm", f"remote {OPENROUTER} openai/gpt-4o", {"agent_kind": "remote", "model": "openai/gpt-4o",
                                                          "base_url": OPENROUTER}))

    def test_add_another_agent_using_each_kind_runs_that_kind(self):
        self.assertEqual({kind["agent_kind"] for *_, kind in self.CASES}, set(agent_kinds.KINDS))   # every kind
        for label, how, kind in self.CASES:
            with self.subTest(kind=kind["agent_kind"]), mock.patch.dict("os.environ", {"HAGENCY_AGENTS_PER_PERSON": "9"}), \
                    mock.patch.object(on_computer.connect_kinds, "problem", return_value=None), \
                    mock.patch.object(on_computer.connect_kinds.subprocess, "run", return_value=mock.Mock(returncode=0)):
                self.computer.setUp()                         # a fresh computer for each agent
                message = self.tell(f"@pm add another agent called {label} using {how}")
                self.assertIn(f"**cx-{label}-agent**", message)
                self.assert_message_names(message, kind["agent_kind"])
                answer, install, saved, secret = self.run_on_computer(message)
                self.assertEqual({k: answer[k] for k in agent_kinds.OFFER_KEYS if k in answer}, kind)
                self.assertEqual(answer["user_id"], f"@cx-{label}-agent:{flows.S}")
                self.assertNotIn("api_key", json.dumps(answer).lower())
                self.assertEqual(install.args[2], kind["agent_kind"])          # connect.py ran THAT kind
                self.assertEqual(saved["agent"], kind["agent_kind"])
                self.assertEqual(saved.get("model"), kind.get("model"))
                self.assertEqual(saved.get("base_url"), kind.get("base_url"))
                self.assertEqual(secret.called, kind["agent_kind"] == "remote")   # the key: typed there, only for remote


class ThePanelPicker(KindFlow):
    def test_the_picker_choice_reaches_connect_py(self):
        loop = self.lp
        loop.client = SimpleNamespace(rooms={"!r": SimpleNamespace(users={CX: 0}, invited_users={})})
        loop.is_person = lambda r, u: True
        picked = json.dumps({"name": "lo", "agent_kind": "ollama", "model": "qwen3:8b", "base_url": ""})
        self.assertIn("Setting up lo", self.run_(console.act(loop, "!r", CX, "add", picked)))
        message = self.said[-1][1]
        self.assert_message_names(message, "ollama")
        answer, install, saved, _ = self.run_on_computer(message)
        self.assertEqual((answer["agent_kind"], answer["model"]), ("ollama", "qwen3:8b"))
        self.assertEqual((install.args[2], saved["model"]), ("ollama", "qwen3:8b"))

    def test_the_picker_offers_exactly_the_kinds_connect_py_knows(self):
        self.lp.client = SimpleNamespace(rooms={})
        offered = console.agent_kinds.kinds()
        self.assertEqual([k["kind"] for k in offered], list(agent_kinds.KINDS))
        self.assertEqual([k["kind"] for k in offered if not k["windows"]], ["hermes"])

    def test_pm_checks_with_the_computers_rules_not_a_copy(self):
        """R3: one list and one set of checks (agent_kinds); @pm accepts exactly what connect.py will."""
        from common import pm_kinds
        self.assertFalse({"KINDS", "MODEL_RE", "LOCAL_HOSTS", "URL_MAX"} & set(vars(pm_kinds)))
        self.assertIs(pm_kinds.FIELDS, agent_kinds.OFFER_KEYS)
        for url in ("https://x.example/v1", "http://localhost:1234/v1", "http://example.com/v1", "https://u:p@x.example",
                    "https://x.example/v1?q=1", "https://x.example:bad/v1", 'https://x.example/"v1'):
            self.assertEqual(pm_kinds.clean_url(url) is not None, agent_kinds.valid_base_url(url), url)
        for model in ("qwen3:8b", "hf.co/org/m:Q4_K_M", "a..b", "-x", "m\n"):
            self.assertEqual(isinstance(pm_kinds.choice("ollama", model), dict), agent_kinds.valid_model(model), model)


class NamesAndOldRecords(KindFlow):
    def test_a_reused_name_gets_a_new_account_and_keeps_its_kind(self):
        self.tell("add another agent called codex")
        registry_removal.forget_agent(CX, "cx-codex-agent")
        message = self.tell("add another agent called codex using ollama qwen3:8b")
        self.assertIn("**cx-codex2-agent**", message)                           # the closed account never comes back
        self.assert_message_names(message, "ollama")
        answer, install, saved, _ = self.run_on_computer(message)
        self.assertEqual(answer["user_id"], f"@cx-codex2-agent:{flows.S}")
        self.assertEqual((answer["agent_kind"], answer["model"]), ("ollama", "qwen3:8b"))
        self.assertEqual(install.args[2], "ollama")

    def test_an_agent_recorded_without_a_kind_connects_as_before(self):
        self.computer.installed = {"codex"}
        message = self.tell("add another agent called laptop")                 # no "using …": as before
        self.assert_message_names(message, None)
        self.assertIn("where Codex or Claude Code is installed", message)
        answer, install, saved, _ = self.run_on_computer(message)
        self.assertFalse(set(agent_kinds.OFFER_KEYS) & set(answer))
        self.assertEqual((install.args[2], saved["agent"]), ("codex", "codex"))   # connect.py picks, as today

    def test_the_first_agent_has_no_kind_and_its_page_has_none(self):
        self.computer.installed = {"claude"}
        self.run_(self.lp.send_code(CX, None))
        message = self.said[-1][1]
        self.assert_message_names(message, None)
        answer, install, _, _ = self.run_on_computer(message)
        self.assertEqual(answer["user_id"], f"@cx-agent:{flows.S}")
        self.assertEqual(install.args[2], "claude")


class WhatTheirComputerNeeds(KindFlow):
    """Screen test 4: an Ollama (or remote) agent's Connect message said "On the computer where Codex is
    installed"; it says what that kind needs there (agent_kinds.ABOUT)."""
    SAID = (("ol", "ollama qwen3:8b", "where Ollama and Codex are installed,"),
            ("rm", f"remote {OPENROUTER} openai/gpt-4o", "where Codex is installed (have the service's API key at hand"),
            ("he", "hermes", "where Hermes is installed (with Hagency's Matrix invite patch"),
            ("cl", "claude", "where Claude Code is installed,"),
            ("cx2", "codex", "where Codex is installed,"))

    def test_each_kinds_connect_message_says_what_must_be_installed(self):
        with mock.patch.dict("os.environ", {"HAGENCY_AGENTS_PER_PERSON": "9"}):
            for label, how, said in self.SAID:
                with self.subTest(how=how):
                    self.assertIn(f"On the computer {said}", self.tell(f"@pm add another agent called {label} using {how}"))
        self.assertEqual({how.split()[0] for _, how, _ in self.SAID}, set(agent_kinds.KINDS))
