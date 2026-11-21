"""Unit tests: people from other Matrix servers get an agent here, identified by their FULL Matrix ID
everywhere (identity.py, registry.py, remote_agent.py, remote_people.py, pm_welcome.py, bridge.py,
connect.py). Run: .venv/bin/python -m unittest tests.test_remote_people"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from common import bridge_config
from client import connect
from common import identity
from common import registry
from server import remote_agent
from server import remote_people
from common import setup_accounts
from adapters.agents.bridge import Bridge

S = "bridge.localhost"
ALICE, EVE, ANNA = "@alice:matrix.org", "@alice:evil.org", f"@anna:{S}"
LOCALPART = re.compile(r"^[a-z0-9-]+$")


def use_registry(case, data):
    path = Path(tempfile.mkdtemp()) / "agents.json"
    path.write_text(json.dumps(data))
    p = mock.patch.object(registry, "PATH", path)
    p.start()
    case.addCleanup(p.stop)
    return path


class Names(unittest.TestCase):
    def test_people_here_keep_their_plain_agent_name(self):
        self.assertEqual(identity.agent_localpart("anna", S), "anna-agent")
        self.assertEqual(identity.agent_localpart(ANNA, S), "anna-agent")

    def test_a_persons_name_is_shown_as_they_wrote_it(self):
        """Seen live: "cx" was shown as "Cx". The display name as set, else the localpart unchanged."""
        room = SimpleNamespace(users={f"@cx:{S}": SimpleNamespace(display_name=None), ALICE: SimpleNamespace(
            display_name="  alice  van\nDijk "), ANNA: 0})
        self.assertEqual(identity.shown_name(room, f"@cx:{S}"), "cx")
        self.assertEqual(identity.shown_name(room, ALICE), "alice van Dijk")
        self.assertEqual(identity.shown_name(room, ANNA), "anna")                       # no profile known
        self.assertEqual(identity.shown_name(None, f"@iPhoneUser:{S}"), "iPhoneUser")  # not re-cased

    def test_another_servers_person_gets_a_distinct_valid_name(self):
        name = identity.agent_localpart(ALICE, S)
        self.assertTrue(name.startswith("alice-at-matrix-org-") and name.endswith("-agent"), name)
        self.assertRegex(name, LOCALPART)
        self.assertEqual(name, identity.agent_localpart(ALICE, S))                    # deterministic
        self.assertNotEqual(name, identity.agent_localpart(EVE, S))
        self.assertNotEqual(name, identity.agent_localpart("@alice:matrix-org", S))   # the hash keeps them apart
        self.assertNotEqual(name, identity.agent_localpart("@Alice:matrix.org", S))

    def test_odd_and_long_ids_are_cleaned_and_capped(self):
        for who in ("@Bob/../x=y:example.com:8448", "@" + "a" * 200 + ":" + "b" * 60 + ".org", f"@john.doe:{S}"):
            name = identity.agent_localpart(who, S)
            self.assertRegex(name, LOCALPART)
            self.assertLessEqual(len(name), 80)
            self.assertNotEqual(name.removesuffix("-agent"), name.removesuffix("-agent").replace("-", ""))  # never a plain name

    def test_a_plain_name_here_never_meets_a_derived_one(self):
        """Plain names have no "-": a person here can't be named like someone else's agent."""
        self.assertIsNone(identity.NAME_RE.match(identity.handle(ALICE, S)))


class Registry(unittest.TestCase):
    def test_old_files_keyed_by_bare_names_are_read_as_full_ids(self):
        use_registry(self, {"anna": {"agent": "hermes"}, "newbie1": {"agent": "remote", "follow": "ask"}})
        self.assertEqual(sorted(registry.load()), [ANNA, f"@newbie1:{S}"])
        self.assertEqual(registry.owner_of("newbie1-agent"), f"@newbie1:{S}")
        self.assertEqual(registry.follow(f"@newbie1:{S}"), "ask")
        self.assertEqual(registry.follow("newbie1"), "ask")                         # bare still means here
        registry.set_follow("newbie1", "never")
        self.assertEqual(json.loads(registry.PATH.read_text())[f"@newbie1:{S}"]["follow"], "never")   # saved migrated

    def test_the_full_id_entry_wins_over_an_old_bare_one(self):
        use_registry(self, {"anna": {"agent": "hermes"}, ANNA: {"agent": "claude"}})
        self.assertEqual(registry.load(), {ANNA: {"agent": "claude"}})

    def test_old_and_new_entries_for_one_person_are_merged(self):
        """GPT review 23: a half-migrated file must not lose the old entry's fields."""
        use_registry(self, {"anna": {"agent": "hermes", "dm": "!d"}, ANNA: {"follow": "ask"}})
        self.assertEqual(registry.load(), {ANNA: {"agent": "hermes", "dm": "!d", "follow": "ask"}})

    def test_an_agent_claimed_twice_belongs_to_nobody(self):
        """GPT review 23: a bad record must never hand the first match someone else's agent."""
        use_registry(self, {"anna": {}, "carl": {"agent_user": "anna-agent"}})
        self.assertIsNone(registry.owner_of("anna-agent"))
        self.assertFalse(registry.owns("anna", "anna-agent"))

    def test_ownership_is_by_full_id(self):
        use_registry(self, {"anna": {}, ALICE: {"agent": "remote"}})
        agent = identity.agent_localpart(ALICE, S)
        self.assertEqual(registry.owner_of(agent), ALICE)
        self.assertTrue(registry.owns(ALICE, agent))
        self.assertFalse(registry.owns(EVE, agent))
        self.assertFalse(registry.owns("@anna:evil.org", "anna-agent"))             # GPT review 11, still
        self.assertTrue(registry.owns("anna", "anna-agent"))
        self.assertEqual(registry.local_name(ALICE), None)
        self.assertEqual(registry.local_name(ANNA), "anna")


class Provision(unittest.TestCase):
    def setUp(self):
        use_registry(self, {"anna": {"agent": "hermes"}})
        self.ensure = mock.patch.object(setup_accounts, "ensure_user", return_value=("pw", True)).start()
        mock.patch.object(setup_accounts, "shared_secret", return_value="secret").start()
        self.env = {}
        mock.patch.object(remote_agent, "load_env", side_effect=lambda: self.env).start()
        self.dm = mock.patch.object(remote_agent, "open_dm", return_value="!dm").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_person_from_another_server_gets_an_agent_here_for_their_full_id(self):
        agent = remote_agent.provision(ALICE)
        self.assertEqual(agent, identity.agent_localpart(ALICE, S))
        self.assertEqual(self.ensure.call_args.args[1], agent)
        self.dm.assert_called_once_with(agent, "pw", ALICE)                         # invited by full ID
        entry = registry.load()[ALICE]
        self.assertEqual((entry["agent"], entry["dm"]), ("remote", "!dm"))
        self.assertIsInstance(entry["created"], float)
        self.assertEqual(registry.owner_of(agent), ALICE)

    def test_one_agent_per_full_id(self):
        first = remote_agent.provision(ALICE)
        created = registry.load()[ALICE]["created"]
        self.assertEqual(remote_agent.provision(ALICE), first)                     # again: the same one
        self.assertEqual(registry.load()[ALICE]["created"], created)
        self.assertNotEqual(remote_agent.provision(EVE), first)                    # same name, other server: its own
        self.assertEqual(self.dm.call_count, 2)

    def test_at_most_so_many_new_ones_an_hour(self):
        with mock.patch.dict("os.environ", {"HAGENCY_REMOTE_PER_HOUR": "2"}):
            remote_agent.provision("@a1:x.org")
            remote_agent.provision("@a2:x.org")
            with self.assertRaisesRegex(setup_accounts.AccountError, "try again later"):
                remote_agent.provision("@a3:x.org")
            remote_agent.provision("@a1:x.org")                                    # an existing one still works
            remote_agent.provision("newbie1")                                      # people here aren't counted
        with mock.patch.object(remote_people, "now", return_value=registry.load()["@a1:x.org"]["created"] + 3601):
            with mock.patch.dict("os.environ", {"HAGENCY_REMOTE_PER_HOUR": "2"}):
                remote_agent.provision("@a3:x.org")                                # an hour later

    def test_the_server_can_turn_it_off(self):
        with mock.patch.dict("os.environ", {"HAGENCY_ALLOW_REMOTE": "0"}):
            with self.assertRaisesRegex(setup_accounts.AccountError, "other servers"):
                remote_agent.provision(ALICE)
            remote_agent.provision("newbie1")                                      # people here still can
        self.ensure.assert_called_once()

    def test_a_failed_chat_still_counts_toward_the_hour(self):
        """GPT review 23: the cap counted only finished set-ups, so failing on purpose was unlimited."""
        self.dm.side_effect = setup_accounts.AccountError("their server refused the invite")
        with mock.patch.dict("os.environ", {"HAGENCY_REMOTE_PER_HOUR": "1"}):
            with self.assertRaises(setup_accounts.AccountError):
                remote_agent.provision("@a1:x.org")
            self.assertIn("created", registry.load()["@a1:x.org"])                 # reserved before registering
            with self.assertRaisesRegex(setup_accounts.AccountError, "try again later"):
                remote_agent.provision("@a2:x.org")
            self.dm.side_effect = None
            remote_agent.provision("@a1:x.org")                                    # their retry finishes
        self.assertEqual(registry.load()["@a1:x.org"]["dm"], "!dm")

    def test_turned_off_means_no_new_sign_ins_either(self):
        agent = remote_agent.provision(ALICE)
        self.env = {setup_accounts.password_key(agent): "s3cret"}
        with mock.patch.dict("os.environ", {"HAGENCY_ALLOW_REMOTE": "0"}):
            self.assertIsNone(remote_agent.credentials_for(ALICE))
            with self.assertRaises(setup_accounts.AccountError):
                remote_agent.provision(ALICE)                                      # no new code for them either
        self.assertIsNotNone(remote_agent.credentials_for(ALICE))

    def test_an_overridden_record_never_hands_out_a_sign_in(self):
        """GPT review 23: connect.py checks the derived name; an odd record must fail before the code is used."""
        use_registry(self, {ALICE: {"agent": "remote", "agent_user": "anna-agent"}})
        self.env = {"PW_ANNA_AGENT": "s3cret"}
        self.assertIsNone(remote_agent.credentials_for(ALICE))

    def test_a_typo_in_the_hourly_cap_is_never_unlimited(self):
        with mock.patch.dict("os.environ", {"HAGENCY_REMOTE_PER_HOUR": "lots"}):
            self.assertEqual(remote_people.per_hour(), 10)

    def test_a_doubly_claimed_or_overridden_name_is_refused(self):
        """GPT review 23 re-check: never set up an agent /connect would refuse, or one that is two people's."""
        agent = identity.agent_localpart(ALICE, S)
        for data in ({"anna": {"agent_user": agent}, "carl": {"agent_user": agent}},
                     {ALICE: {"agent": "remote", "agent_user": "alice-x-agent"}}):
            use_registry(self, data)
            with self.assertRaisesRegex(setup_accounts.AccountError, "taken"):
                remote_agent.provision(ALICE)
        self.ensure.assert_not_called()

    def test_a_taken_agent_name_is_refused_not_shared(self):
        agent = identity.agent_localpart(ALICE, S)
        use_registry(self, {"anna": {"agent_user": agent}})                         # an operator's odd record
        with self.assertRaisesRegex(setup_accounts.AccountError, "taken"):
            remote_agent.provision(ALICE)
        self.ensure.assert_not_called()

    def test_the_sign_in_names_the_full_id_and_their_own_account(self):
        agent = remote_agent.provision(ALICE)
        self.env = {setup_accounts.password_key(agent): "s3cret"}
        creds = remote_agent.credentials_for(ALICE)
        self.assertEqual((creds["owner"], creds["user_id"]), (ALICE, f"@{agent}:{S}"))
        self.assertIsNone(remote_agent.credentials_for(EVE))
        self.assertTrue(bridge_config.credentials_match(creds["user_id"], creds["owner"]))

    def test_codes_issued_before_full_ids_still_work(self):
        remote_agent.provision("newbie1")
        self.env = {"PW_NEWBIE1_AGENT": "s3cret"}
        self.assertEqual(remote_agent.credentials_for(f"@newbie1:{S}")["owner"], "newbie1")   # older connect.py too


class OwnComputer(unittest.TestCase):
    """connect.py and the bridge on a matrix.org person's computer."""
    agent = identity.agent_localpart(ALICE, "rooms.example.org")
    creds = {"homeserver": "https://matrix.example.org", "user_id": f"@{agent}:rooms.example.org",
             "password": "s3cret", "owner": ALICE}

    def redeem(self, answer):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(answer).encode()
        with mock.patch.object(connect.urllib.request, "urlopen", return_value=response):
            return connect.redeem("https://rooms.example.org", "ABCDE-FGHJK")

    def test_connect_accepts_their_sign_in(self):
        self.assertEqual(self.redeem(self.creds)["owner"], ALICE)

    def test_connect_refuses_a_sign_in_for_someone_elses_agent(self):
        for bad in ({**self.creds, "owner": EVE}, {**self.creds, "owner": "alice"}, {**self.creds, "owner": "@x y:z"},
                    {**self.creds, "user_id": "@anna-agent:rooms.example.org"}):
            with self.assertRaises(SystemExit):
                self.redeem(bad)

    def test_the_credentials_file_must_match_the_full_id(self):
        path = Path(tempfile.mkdtemp()) / "c.json"
        path.write_text(json.dumps(self.creds))
        self.assertEqual(bridge_config.load_credentials(str(path))["owner"], ALICE)
        path.write_text(json.dumps({**self.creds, "owner": EVE}))
        with self.assertRaises(SystemExit):
            bridge_config.load_credentials(str(path))

    def test_the_bridge_obeys_only_the_full_id(self):
        b = Bridge(ALICE, "codex")
        self.assertEqual((b.owner_id, b.user_id), (ALICE, f"@{identity.agent_localpart(ALICE, S)}:{S}"))
        self.assertRegex(b.owner, LOCALPART)                                       # folders and device names
        room = SimpleNamespace(users={ALICE: None, EVE: None, b.user_id: None}, invited_users={})
        msg = lambda who: SimpleNamespace(sender=who, body="hi", source={"content": {"body": "hi"}})
        self.assertIsNone(b.should_ignore(room, msg(ALICE)))
        self.assertIsNotNone(b.should_ignore(room, msg(EVE)))                       # same name, other server
        self.assertEqual(Bridge("anna", "codex").user_id, f"@anna-agent:{S}")      # people here: unchanged

    def test_the_password_is_only_for_the_files_own_account(self):
        with mock.patch.object(bridge_config, "CREDENTIALS", self.creds), \
                mock.patch.object(bridge_config, "SERVER_NAME", "rooms.example.org"):
            self.assertEqual(bridge_config.agent_password(self.agent), "s3cret")
            with self.assertRaises(SystemExit):
                bridge_config.agent_password("anna-agent")

    def test_on_their_computer_the_agent_knows_its_owner(self):
        with mock.patch.object(registry, "load", return_value={}), \
                mock.patch.object(bridge_config, "CREDENTIALS", {**self.creds, "user_id": f"@{self.agent}:{S}"}):
            self.assertEqual(registry.owner_of(self.agent), ALICE)
            self.assertIsNone(registry.owner_of("anna-agent"))


if __name__ == "__main__":
    unittest.main()
