"""Which AI a further agent runs, chosen in "Add an agent" (the panel and the chat: pm_kinds.py), and a
removed agent's name used again. @pm records the choice and hands it to that agent's computer with its
sign-in (agent_kind, model, base_url); an API key is never taken here. Run: .venv/bin/python -m unittest tests.test_agent_picker"""
import json
import re
import shutil
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from common import agent_kinds
from common import pm_kinds
from server import console
from server import panel
from common import registry
from server import registry_removal
from server import remote_agent
from tests import test_more_agents_flows as flows
from tests.test_more_agents_flows import CX, text

OPENROUTER = "https://openrouter.ai/api/v1"


class PickInChat(flows.Provisioning):
    def tell(self, words):
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(CX, words)))
        return self.said[-1][1]

    def test_each_kind_said_in_plain_words_is_recorded(self):
        for words, kind in (("add another agent called pi using claude", {"agent_kind": "claude"}),
                            ("add another agent called cx2 using claude code", {"agent_kind": "claude"}),
                            ("add another agent called lo using ollama llama3.2", {"agent_kind": "ollama", "model": "llama3.2"}),
                            ("add another agent called rm using remote https://openrouter.ai/api/v1 openai/gpt-4o",
                             {"agent_kind": "remote", "model": "openai/gpt-4o", "base_url": OPENROUTER})):
            with mock.patch.dict("os.environ", {"HAGENCY_AGENTS_PER_PERSON": "9"}):
                self.tell(words)
            label = words.split()[4]
            found = registry.find(CX, label)
            self.assertEqual({k: found[k] for k in pm_kinds.FIELDS if k in found}, kind, words)

    def test_the_kind_reaches_its_computer_with_the_sign_in(self):
        registry.add_agent(CX, "rm", picked={"agent_kind": "remote", "model": "openai/gpt-4o", "base_url": OPENROUTER})
        registry.update(CX, "rm", ready=True)
        with mock.patch.object(remote_agent, "load_env", return_value={"PW_CX_RM_AGENT": "pw"}):
            creds = remote_agent.credentials_for(CX, "cx-rm-agent")
        self.assertEqual((creds["agent_kind"], creds["model"], creds["base_url"]), ("remote", "openai/gpt-4o", OPENROUTER))
        self.assertNotIn("api_key", json.dumps(creds))
        registry.add_agent(CX, "plain")
        with mock.patch.object(remote_agent, "load_env", return_value={"PW_CX_PLAIN_AGENT": "pw"}):
            self.assertNotIn("agent_kind", remote_agent.credentials_for(CX, "cx-plain-agent"))   # no kind: as before

    def test_an_api_key_typed_in_the_chat_is_refused_and_never_repeated(self):
        answer = self.tell(f"add another agent called rm using remote {OPENROUTER} gpt-4o sk-or-v1-abcdef0123456789abcd")
        self.assertEqual(answer, pm_kinds.NO_KEYS)
        self.assertNotIn("sk-or", json.dumps(self.said))
        self.assertEqual(self.made, [])

    def test_unknown_or_unsafe_choices_say_how(self):
        for how in ("gpt5", "remote http://plain.example/v1 m", "remote https://u:p@x.example/v1 m",
                    "codex o3", "remote https://x.example/v1", "ollama ../etc", "remote https://x.example/v1?k=1 m"):
            self.assertIsInstance(pm_kinds.from_words(how), str, how)
        self.assertEqual(pm_kinds.from_words("remote http://localhost:1234/v1 qwen3:8b"),        # their own computer
                         {"agent_kind": "remote", "model": "qwen3:8b", "base_url": "http://localhost:1234/v1"})
        self.tell("add another agent called pi using gpt5")
        self.assertEqual(self.made, [])

    def test_asked_for_a_name_the_choice_is_kept(self):
        self.tell("add another agent using hermes")
        self.tell("herm")
        self.assertEqual(registry.find(CX, "herm")["agent_kind"], "hermes")

    def test_the_panel_picker(self):
        loop = self.lp
        loop.client = SimpleNamespace(rooms={"!r": SimpleNamespace(users={CX: 0}, invited_users={})})
        loop.is_person = lambda r, u: True
        picked = json.dumps({"name": "lo", "agent_kind": "ollama", "model": "qwen2.5-coder:7b", "base_url": ""})
        self.assertIn("Setting up lo", self.run_(console.act(loop, "!r", CX, "add", picked)))
        self.assertEqual(registry.find(CX, "lo")["model"], "qwen2.5-coder:7b")
        keyed = json.dumps({"name": "rm", "agent_kind": "remote", "model": "sk-proj-abcdef0123456789xyz", "base_url": OPENROUTER})
        self.assertEqual(self.run_(console.act(loop, "!r", CX, "add", keyed)), pm_kinds.NO_KEYS)
        mine = console.my_agents(SimpleNamespace(agent_online=lambda a: None, ever_connected=lambda a: False),
                                 SimpleNamespace(users={}, invited_users={}), "!r", CX)
        self.assertEqual([a["runs"] for a in mine], [pm_kinds.NOT_REPORTED, "Ollama (a model on your computer): qwen2.5-coder:7b"])

    def test_remove_codex_then_add_codex_again(self):
        """Owner request: the name is theirs to use again; its closed account never comes back."""
        self.tell("add another agent called codex")
        registry_removal.forget_agent(CX, "cx-codex-agent")
        self.tell("add another agent called codex using codex")
        again = registry.find(CX, "codex")
        self.assertEqual((again["localpart"], registry.name_of(again), again["agent_kind"]), ("cx-codex2-agent", "codex", "codex"))
        self.assertEqual(self.codes[-1], "cx-codex2-agent")
        self.assertIn("**cx-codex2-agent**", self.said[-1][1])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PanelPicker(unittest.TestCase):
    def node(self, call: str) -> str:
        esc = re.search(r"^const esc = .*$", panel.PAGE, re.M).group(0)
        source = "\n".join([esc, "const cap = t => t;", panel.SCRIPTS["panel-agents.js"].read_text(), call])
        done = subprocess.run([shutil.which("node"), "-e", source], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        return done.stdout

    def test_the_picker_lists_the_servers_kinds_and_each_agent_shows_what_it_runs(self):
        state = {"agents": [{"name": "cx-agent", "label": "main", "first": True, "follow": "always", "remote": True,
                             "online": True, "runs": "Remote: m at openrouter.ai"}], "can_add": True,
                 "kinds": agent_kinds.kinds()}
        mac = "Object.defineProperty(globalThis, 'navigator', {value: {platform: 'MacIntel'}});"
        html = self.node(f"{mac} process.stdout.write(agentCards({json.dumps(state)}))")
        self.assertEqual(re.findall(r'<option value="(\w*)"', html), ["", *agent_kinds.KINDS])   # exactly the list
        for k in agent_kinds.kinds():
            only = "" if k["windows"] else " (Mac/Linux only)"
            self.assertRegex(html, rf'<option value="{k["kind"]}" data-needs="Needs on your computer: [^"]+">'
                                   rf'{re.escape(k["name"] + only)}</option>')
            self.assertIn(k["needs"][0].split(" (")[0].replace("'", "&#39;"), html)                       # what it needs, from the one list
        self.assertIn('<span class="chip">Remote: m at openrouter.ai</span>', html)
        self.assertIn("API key is asked for on your computer", html)

    def test_on_windows_hermes_is_greyed_out_and_says_mac_linux_only(self):
        state = {"agents": [], "can_add": True, "kinds": agent_kinds.kinds()}
        win = "Object.defineProperty(globalThis, 'navigator', {value: {platform: 'Win32'}});"
        html = self.node(f"{win} process.stdout.write(agentCards({json.dumps(state)}))")
        self.assertRegex(html, r'<option value="hermes" data-needs="[^"]+" disabled>Hermes \(Mac/Linux only\)</option>')
        self.assertEqual(re.findall(r'<option value="(\w+)"[^>]* disabled', html), list(agent_kinds.NOT_ON_WINDOWS))

    def test_choosing_a_kind_shows_what_it_needs(self):
        state = {"agents": [], "can_add": True, "kinds": agent_kinds.kinds()}
        out = self.node("let h; const els = {newneeds: {textContent: ''}}; const document = {addEventListener: (t, f) => h = f,"
                        " getElementById: id => els[id]}; agentCards(" + json.dumps(state) + ");"
                        "h({target: {id: 'newkind', selectedOptions: [{dataset: {needs: 'Needs on your computer: X.'}}]}});"
                        "process.stdout.write(els.newneeds.textContent)")
        self.assertEqual(out, "Needs on your computer: X.")

    def test_add_sends_the_choice_or_a_plain_name(self):
        fields = {"newagent": "lo", "newkind": "ollama", "newmodel": "llama3.2", "newbase": ""}
        out = self.node(f"const f = {json.dumps(fields)}; const document = {{getElementById: id => ({{value: f[id]}})}};"
                        "process.stdout.write(addValue() + '|'); f.newkind = ''; process.stdout.write(addValue());")
        picked, plain = out.split("|")
        self.assertEqual(json.loads(picked), {"name": "lo", "agent_kind": "ollama", "model": "llama3.2", "base_url": ""})
        self.assertEqual(json.loads(plain), {"name": "lo", "agent_kind": "", "model": "", "base_url": ""})   # GPT review
        self.assertEqual(pm_kinds.from_panel(plain), ("lo", None))             # no kind: as before
        self.assertEqual(pm_kinds.from_panel(picked), ("lo", {"agent_kind": "ollama", "model": "llama3.2"}))


class ReviewFixes(flows.Provisioning):
    """GPT review of the picker and reusable names: each adopted point, as a test."""
    def tell(self, words):
        self.run_(self.lp.on_welcome_text(SimpleNamespace(room_id="!dm"), text(CX, words)))
        return self.said[-1][1]

    def test_keys_without_a_prefix_or_in_a_url_or_an_extra_field_are_refused(self):
        for how in ("ollama a8F3kQ9zL2mN7pR4tV6wX1yB", "remote https://x.example/v1/a8F3kQ9zL2mN7pR4tV6wX1yB m"):
            self.assertEqual(pm_kinds.from_words(how), pm_kinds.NO_KEYS, how)
        extra = json.dumps({"name": "rm", "agent_kind": "remote", "model": "m", "base_url": OPENROUTER, "api_key": "x"})
        self.assertEqual(pm_kinds.from_panel(extra)[1], pm_kinds.NO_KEYS)
        for model in ("qwen2.5-coder:7b", "meta-llama/llama-3.1-70b-instruct", "hf.co/org/m:Q4_K_M"):
            self.assertIsInstance(pm_kinds.from_words(f"ollama {model}"), dict, model)   # real names still pass

    def test_a_long_word_of_letters_only_is_a_key_too(self):
        self.assertEqual(pm_kinds.from_words("ollama " + "q" * 40), pm_kinds.NO_KEYS)

    def test_a_new_slot_never_takes_another_agents_given_name(self):
        """GPT re-check: codex's old slot retired, another agent called codex2: codex must not get slot codex2."""
        self.tell("add another agent called codex")
        registry_removal.forget_agent(CX, "cx-codex-agent")
        registry.add_agent(CX, "other")
        registry.update(CX, "other", display="codex2")
        self.assertEqual(registry.add_agent(CX, "codex")["label"], "codex3")

    def test_a_finished_agent_keeps_its_ai_when_its_code_is_resent(self):
        registry.add_agent(CX, "lo", picked={"agent_kind": "ollama", "model": "llama3.2"})
        registry.update(CX, "lo", ready=True)
        with mock.patch.object(remote_agent.setup_accounts, "ensure_user", return_value=("pw", False)), \
                mock.patch.object(remote_agent.setup_accounts, "shared_secret", return_value="s"), \
                mock.patch.object(remote_agent, "load_env", return_value={}), \
                mock.patch.object(remote_agent, "open_dm", return_value="!d"):
            flows.Provisioning.real_provision(CX, "lo", {"agent_kind": "claude"})
        self.assertEqual(registry.find(CX, "lo")["agent_kind"], "ollama")

    def test_a_malformed_port_is_refused_as_on_their_computer(self):
        self.assertIsNone(pm_kinds.clean_url("https://example.com:bad/v1"))

    def test_another_agents_slot_name_is_never_taken_or_resent(self):
        self.tell("add another agent called codex")
        registry_removal.forget_agent(CX, "cx-codex-agent")
        self.tell("add another agent called codex")                        # slot codex2, called codex
        self.made.clear()
        answer = self.tell("add another agent called codex2 using claude")
        self.assertIn("already have an agent called codex2", answer)
        self.assertEqual(self.made, [])                                     # codex's code was never resent
        self.assertEqual([a["localpart"] for a in self.lp.removable(CX, "codex")], ["cx-codex2-agent"])

    def test_finishing_an_unfinished_agent_takes_the_ai_chosen_now(self):
        registry.add_agent(CX, "lo", picked={"agent_kind": "ollama", "model": "bad"})
        found = registry.find(CX, "lo")
        with mock.patch.object(remote_agent.setup_accounts, "ensure_user", return_value=("pw", True)), \
                mock.patch.object(remote_agent.setup_accounts, "shared_secret", return_value="s"), \
                mock.patch.object(remote_agent, "load_env", return_value={}), \
                mock.patch.object(remote_agent, "open_dm", return_value="!d"):
            flows.Provisioning.real_provision(CX, "lo", {"agent_kind": "ollama", "model": "llama3.2"})
        self.assertEqual(registry.find(CX, found["localpart"])["model"], "llama3.2")


class Describe(unittest.TestCase):
    def test_what_my_agents_shows(self):
        self.assertEqual(pm_kinds.describe({"agent_kind": "claude"}), "Claude Code")
        self.assertEqual(pm_kinds.describe({"agent_kind": "remote", "model": "m", "base_url": OPENROUTER}),
                         "Remote: m at openrouter.ai")
        self.assertEqual(pm_kinds.describe({}), "")


if __name__ == "__main__":
    unittest.main()
