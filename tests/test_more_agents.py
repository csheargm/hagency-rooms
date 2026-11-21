"""Unit tests, step 4: a person can have several agents (ADR 0005). The model, the migration of the
live single-agent shape, names, caps, codes and sign-ins (identity.py, registry.py, remote_people.py,
pairing.py, remote_agent.py, bridge_config.py, connect.py). The chat and console flows, routing and
presence are in test_more_agents_flows.py. Run: .venv/bin/python -m unittest tests.test_more_agents"""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import bridge_config
from client import connect
from common import identity
from server import pairing
from common import registry
from server import remote_agent
from server import remote_people
from common.setup_accounts import AccountError
from tests import ROOT

S = "bridge.localhost"
CX, ALICE, BETTY = f"@cx:{S}", "@alice:matrix.org", f"@betty:{S}"
# The public server's agents.json today (room ids replaced): every person exactly one agent.
LIVE = {f"@anna:{S}": {}, f"@betty:{S}": {}, f"@pubtest1:{S}": {"agent": "remote", "dm": "!d1"},
        CX: {"agent": "remote", "dm": "!d2"}, f"@pubtest2:{S}": {"agent": "remote", "dm": "!d3"}}


def use_registry(case, data):
    path = Path(tempfile.mkdtemp()) / "agents.json"
    path.write_text(json.dumps(data))
    p = mock.patch.object(registry, "PATH", path)
    p.start()
    case.addCleanup(p.stop)
    return path


class Names(unittest.TestCase):
    def test_a_further_agent_adds_its_label(self):
        self.assertEqual(identity.agent_localpart(CX, S), "cx-agent")                   # the first: unchanged
        self.assertEqual(identity.agent_localpart(CX, S, "codex"), "cx-codex-agent")
        far = identity.agent_localpart(ALICE, S, "laptop")
        self.assertEqual(far, identity.agent_localpart(ALICE, S).removesuffix("-agent") + "-laptop-agent")
        self.assertRegex(far, r"^[a-z0-9-]+$")
        self.assertTrue(connect.USER_RE.match(f"@{identity.agent_localpart('@' + 'a' * 200 + ':' + 'b' * 60, S, 'abcdefghi')}:{S}"))

    def test_labels_never_meet_plain_names_or_hashes(self):
        for bad in ("Codex", "co-dex", "1abc", "a" * 10, "a5829e99c7"):
            with self.assertRaises(ValueError):
                identity.agent_localpart(CX, S, bad)
        names = {identity.agent_localpart(p, S, lab) for p in (CX, "@cxcodex:" + S, ALICE, BETTY)
                 for lab in (None, "codex", "laptop")}
        self.assertEqual(len(names), 12)                                                # collision-free
        self.assertIsNone(identity.NAME_RE.match("cx-codex"))                            # never a plain name

    def test_typed_names_are_cleaned(self):
        self.assertEqual(registry.clean_label("Codex"), "codex")
        self.assertEqual(registry.clean_label("My Laptop!"), "mylaptop")
        self.assertEqual(registry.clean_label("2nd box"), "ndbox")
        self.assertEqual(registry.clean_label("averyveryverylongname"), "averyvery")
        self.assertIsNone(registry.clean_label("!!! 42"))


class Migration(unittest.TestCase):
    def test_the_live_shape_reads_as_one_agent_each_and_is_never_rewritten(self):
        path = use_registry(self, copy.deepcopy(LIVE))
        before = path.read_text()
        for person, info in LIVE.items():
            found = registry.agents_of(person)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["localpart"], identity.agent_localpart(person, S))
            self.assertEqual((found[0]["label"], found[0]["first"], found[0]["follow"]), ("main", True, "always"))
            self.assertEqual(found[0]["agent"], info.get("agent", "server"))
            self.assertEqual(registry.owner_of(found[0]["localpart"]), person)
        self.assertEqual(registry.agents_of("@nobody:" + S), [])
        self.assertEqual(path.read_text(), before)                                       # reading changes nothing

    def test_adding_one_keeps_the_first_agents_fields(self):
        path = use_registry(self, copy.deepcopy(LIVE))
        added = registry.add_agent(CX, "codex")
        self.assertEqual((added["localpart"], added["follow"], added["first"]), ("cx-codex-agent", "never", False))
        saved = json.loads(path.read_text())[CX]
        self.assertEqual((saved["agent"], saved["dm"]), ("remote", "!d2"))                # what older code reads
        self.assertEqual([a["localpart"] for a in registry.agents_of(CX)], ["cx-agent", "cx-codex-agent"])
        self.assertEqual(registry.owner_of("cx-codex-agent"), CX)
        self.assertEqual(registry.agent_of(CX), "cx-agent")                              # "your agent": the first

    def test_per_agent_follow_and_fields(self):
        use_registry(self, copy.deepcopy(LIVE))
        registry.add_agent(CX, "codex")
        registry.set_follow(CX, "always", "cx-codex-agent")
        registry.update(CX, "codex", dm="!d9")
        self.assertEqual((registry.follow(CX), registry.follow(CX, "cx-codex-agent")), ("always", "always"))
        registry.set_follow(CX, "never")
        self.assertEqual((registry.follow(CX), registry.find(CX, "codex")["dm"]), ("never", "!d9"))

    def test_damaged_further_entries_are_never_agents(self):
        use_registry(self, {CX: {"agent": "remote", "more": [{"label": "codex"}, {"label": "codex"}, "x",
                                                            {"label": "BAD-1"}, {"nolabel": 1}]}})
        self.assertEqual([a["label"] for a in registry.agents_of(CX)], ["main", "codex"])
        use_registry(self, {CX: {"agent": "remote", "label": ["x"], "agent_user": {"a": 1}, "dm": 5,
                                 "more": [{"label": "codex\n"}]}, BETTY: {}})
        self.assertEqual([(a["label"], a["localpart"], a["dm"]) for a in registry.agents_of(CX)], [("main", "cx-agent", None)])
        self.assertEqual(registry.owner_of("betty-agent"), BETTY)                        # GPT 24: one bad line breaks nobody
        self.assertFalse(bridge_config.credentials_match(f"@cx-codex-agent:{S}", "cx", "codex\n"))
        self.assertIsNone(registry.clean_label("\n"))
        with self.assertRaises(ValueError):
            registry.update(CX, "laptop", dm="!x")                                       # never the first's fields
        use_registry(self, {CX: {"agent": "remote", "more": "oops"}})
        self.assertEqual(len(registry.agents_of(CX)), 1)
        self.assertEqual(registry.add_agent(CX, "codex")["localpart"], "cx-codex-agent")   # a damaged list: replaced


class Caps(unittest.TestCase):
    def test_per_person_cap_names_and_first_agent(self):
        use_registry(self, copy.deepcopy(LIVE))
        registry.add_agent(CX, "codex")
        registry.add_agent(CX, "laptop")
        with self.assertRaisesRegex(ValueError, "the most one person"):
            registry.add_agent(CX, "third")                                              # default 3
        with mock.patch.dict(os.environ, {"HAGENCY_AGENTS_PER_PERSON": "4"}):
            registry.add_agent(CX, "third")
        with mock.patch.dict(os.environ, {"HAGENCY_AGENTS_PER_PERSON": "lots"}):
            self.assertEqual(registry.per_person(), 3)                                   # a typo: never unlimited
        with self.assertRaisesRegex(ValueError, "already have an agent called codex"):
            registry.add_agent(CX, "codex")
        with self.assertRaisesRegex(ValueError, "don't have an agent"):
            registry.add_agent("@nobody:" + S, "codex")
        with self.assertRaisesRegex(ValueError, "one short word"):
            registry.add_agent(BETTY, "my-box")

    def test_an_account_name_someone_else_claims_gets_a_new_one(self):
        use_registry(self, {CX: {"agent": "remote"}, BETTY: {"agent_user": "cx-codex-agent"}})
        added = registry.add_agent(CX, "codex")
        self.assertEqual((added["localpart"], registry.name_of(added)), ("cx-codex2-agent", "codex"))

    def test_the_hourly_cap_for_other_servers_counts_further_agents(self):
        people = {ALICE: {"agent": "remote", "created": 1000, "more": [{"label": "b", "created": 1001}]}}
        with mock.patch.dict(os.environ, {"HAGENCY_REMOTE_PER_HOUR": "2"}):
            remote_people.check_new(people, ALICE, at=1100)                              # theirs: not counted again
            with self.assertRaises(AccountError):
                remote_people.check_new(people, ALICE, at=1100, further=True)            # 2 this hour: full
            remote_people.check_new(people, ALICE, at=1000 + remote_people.HOUR_S + 5, further=True)
            local = {CX: {"agent": "remote", "more": [{"label": "a", "created": 1000}, {"label": "b", "created": 1000}]}}
            remote_people.check_new(registry.from_elsewhere(local), ALICE, at=1100)       # GPT 24: ours don't count

    def test_caddy_sends_the_my_agents_script_to_pm(self):
        """GPT 24: /panel-agents.js fell through to Element, so the panel had no "My agents"."""
        line = next(l for l in (ROOT / "deploy" / "Caddyfile").read_text().splitlines() if "@pm path" in l)
        for path in ("/panel", "/panel-agents.js", "/panel-project.js", "/console", "/connect/*"):
            self.assertIn(f" {path}", line)


class Codes(unittest.TestCase):
    def test_each_agent_has_its_own_code(self):
        codes = pairing.Codes(Path(tempfile.mkdtemp()) / "p.json")
        first = codes.issue(CX, now=0)
        extra = codes.issue(CX, now=0, agent="cx-codex-agent")
        again = codes.issue(CX, now=0, agent="cx-codex-agent")                           # replaces only its own
        self.assertEqual(codes.redeem_agent(extra, now=1), (None, None, 0))
        self.assertEqual(codes.redeem_agent(again, now=1), (CX, "cx-codex-agent", 0))
        self.assertEqual(codes.redeem_agent(first, now=1), (CX, None, 0))
        a, b = codes.issue(CX, now=0), codes.issue(CX, now=0, agent="cx-codex-agent")
        codes.revoke(CX, "cx-codex-agent")
        self.assertEqual((codes.redeem(b, now=1), codes.redeem(a, now=1)), (None, CX))
        c = codes.issue(CX, now=0)
        codes.codes[pairing.digest(c)] = {**codes.codes[pairing.digest(c)], "agent": ["cx-codex-agent"]}
        self.assertEqual(codes.redeem_agent(c, now=1), (None, None, 0))                  # GPT 24: damaged, not the first's


class SignIn(unittest.TestCase):
    def setUp(self):
        use_registry(self, copy.deepcopy(LIVE))
        registry.add_agent(CX, "codex")
        env = {"PW_CX_AGENT": "pw1", "PW_CX_CODEX_AGENT": "pw2"}
        p = mock.patch.object(remote_agent, "load_env", lambda: dict(env))
        p.start()
        self.addCleanup(p.stop)

    def test_a_code_signs_in_exactly_its_agent(self):
        first, extra = remote_agent.credentials_for(CX), remote_agent.credentials_for(CX, "cx-codex-agent")
        self.assertEqual((first["user_id"], first["password"], "label" in first), (f"@cx-agent:{S}", "pw1", False))
        self.assertEqual((extra["user_id"], extra["password"], extra["label"]), (f"@cx-codex-agent:{S}", "pw2", "codex"))
        self.assertIsNone(remote_agent.credentials_for(CX, "codex"))                     # codes name the account
        self.assertIsNone(remote_agent.credentials_for(BETTY, "cx-codex-agent"))         # never someone else's
        self.assertTrue(bridge_config.credentials_match(extra["user_id"], "cx", "codex"))
        self.assertFalse(bridge_config.credentials_match(extra["user_id"], "cx"))
        self.assertFalse(bridge_config.credentials_match(extra["user_id"], "cx", "laptop"))
        self.assertFalse(bridge_config.credentials_match(f"@cx-agent:{S}", "cx", "x-y"))

    def test_a_code_from_before_a_disconnect_never_gets_the_new_password(self):
        """GPT 24: /connect takes the code, then waits for the lock while a disconnect rotates."""
        registry.update(CX, "codex", disconnected=500.0)
        self.assertIsNone(remote_agent.credentials_for(CX, "cx-codex-agent", issued=499.0))
        self.assertIsNone(remote_agent.credentials_for(CX, "cx-codex-agent", issued=500.0))
        self.assertEqual(remote_agent.credentials_for(CX, "cx-codex-agent", issued=501.0)["password"], "pw2")
        self.assertEqual(remote_agent.credentials_for(CX, None, issued=1.0)["password"], "pw1")   # the first: not cut

    def test_disconnect_never_resets_an_account_we_never_set_up(self):
        """GPT 24: a failed reservation (the name existed with someone else's password) must not let
        its claimant reset that account and then fetch the new password."""
        registry.add_agent(CX, "laptop")                                                 # no password held
        with mock.patch.object(remote_agent.local_signup, "admin_call") as admin, \
                mock.patch.object(remote_agent.setup_accounts, "save_env") as saved:
            with self.assertRaisesRegex(AccountError, "never set up"):
                remote_agent.disconnect(CX, "cx-laptop-agent")
            with self.assertRaisesRegex(AccountError, "isn't your agent"):
                remote_agent.disconnect(BETTY, "cx-codex-agent")
            with mock.patch.object(remote_agent, "_signs_in", return_value=False) as signs_in:
                with self.assertRaisesRegex(AccountError, "never set up"):
                    remote_agent.disconnect(CX, "cx-codex-agent")    # a saved password that isn't the account's
            signs_in.assert_called_once_with("cx-codex-agent", "pw2")
            registry.update(CX, "codex", ready=True)                 # provision_more: the account is ours
            admin.return_value = {}
            remote_agent.disconnect(CX, "cx-codex-agent")
        self.assertEqual(admin.call_count, 1)
        self.assertEqual(saved.call_count, 1)
        self.assertIsInstance(registry.find(CX, "codex")["disconnected"], float)
        self.assertIsNone(registry.find(CX, None)["disconnected"])

    def test_connect_keeps_a_further_agent_in_its_own_folder(self):
        creds = {"homeserver": "https://h.example", "user_id": f"@cx-codex-agent:{S}", "password": "pw2",
                 "owner": "cx", "label": "codex"}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(creds).encode()
        with mock.patch.object(connect.urllib.request, "urlopen", return_value=resp):
            got = connect.redeem("https://c.example", "CODE")
        self.assertEqual(got["label"], "codex")
        home = Path("/h/.hagency")
        self.assertEqual(connect.agent_dir(home, got), home / "cx-codex-agent")
        self.assertEqual(connect.agent_dir(home, {**got, "label": None}), home)
        self.assertEqual(connect.name_arg(got), "--name cx-codex-agent ")
        resp.__enter__.return_value.read.return_value = json.dumps({**creds, "label": "laptop"}).encode()
        with mock.patch.object(connect.urllib.request, "urlopen", return_value=resp), self.assertRaises(SystemExit):
            connect.redeem("https://c.example", "CODE")                                  # another agent's name: refused

    def test_the_bridge_runs_as_the_agent_in_its_credentials(self):
        with mock.patch.object(bridge_config, "CREDENTIALS", {"user_id": f"@cx-codex-agent:{S}", "owner": "cx"}), \
                mock.patch.object(bridge_config, "SERVER_NAME", S):
            self.assertEqual(bridge_config.agent_handle(CX), "cx-codex")
            with self.assertRaises(SystemExit):
                bridge_config.agent_handle(BETTY)                                        # not betty's agent
        with mock.patch.object(bridge_config, "CREDENTIALS", {}):
            self.assertEqual(bridge_config.agent_handle(CX), "cx")


class ProvisionMore(unittest.TestCase):
    def setUp(self):
        self.path = use_registry(self, copy.deepcopy(LIVE))
        for name, value in (("load_env", lambda: {}), ("open_dm", lambda agent, pw, person: "!new")):
            p = mock.patch.object(remote_agent, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(remote_agent.setup_accounts, "shared_secret", return_value="s")
        p.start()
        self.addCleanup(p.stop)

    def test_made_counted_first_and_finished_on_a_retry(self):
        with mock.patch.object(remote_agent.setup_accounts, "ensure_user", side_effect=AccountError("down")):
            with self.assertRaises(AccountError):
                remote_agent.provision_more(CX, "codex")
        self.assertEqual(len(registry.agents_of(CX)), 2)                                 # counted before the account
        with mock.patch.object(remote_agent.setup_accounts, "ensure_user", return_value=("pw", True)) as made:
            self.assertEqual(remote_agent.provision_more(CX, "codex"), "cx-codex-agent")   # the same one, finished
        made.assert_called_once_with({}, "cx-codex-agent", "s", verify=True)
        self.assertEqual(registry.find(CX, "codex")["dm"], "!new")
        self.assertTrue(registry.find(CX, "codex")["ready"])
        self.assertEqual(len(registry.agents_of(CX)), 2)

    def test_refused_without_a_first_agent_or_over_the_cap(self):
        with mock.patch.object(remote_agent.setup_accounts, "ensure_user", return_value=("pw", True)) as made:
            with self.assertRaisesRegex(AccountError, "don't have an agent"):
                remote_agent.provision_more("@nobody:" + S, "codex")
            remote_agent.provision_more(CX, "codex")
            remote_agent.provision_more(CX, "laptop")
            with self.assertRaisesRegex(AccountError, "the most one person"):
                remote_agent.provision_more(CX, "third")
            with self.assertRaisesRegex(AccountError, "already have an agent called main"):
                remote_agent.provision_more(BETTY, "main")
        self.assertEqual(made.call_count, 2)                                             # never an account past the cap


if __name__ == "__main__":
    unittest.main()
