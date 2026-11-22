"""User-flow tests (R6): two people pair agents on the same computer and user account. The second
one's pairing never replaces the first one's sign-in (signin_place.place): it gets its own folder,
log and service, as a further agent. Someone an older connect.py already hit is told plainly what to
do (signin_place.stranded), and their new code pairs into its own folder. The server, the CLIs and
the service manager are stood in for; every sign-in here is made up.
Run: .venv/bin/python -m unittest tests.test_signin_place"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from client import agent_service
from client import connect
from client import signin_place
from common import identity

S = "matrix.hagencylabs.xyz"
A = {"homeserver": f"https://{S}", "user_id": f"@cx-agent:{S}", "password": "made-up-pw-a", "owner": "cx"}
B_OWNER = "@chrisxie:matrix.org"                  # a person from another server
B = {"homeserver": f"https://{S}", "user_id": f"@{identity.agent_localpart(B_OWNER, S)}:{S}",
     "password": "made-up-pw-b", "owner": B_OWNER}
B_NAME = B["user_id"][1:].partition(":")[0]


def server(answer: dict) -> mock.MagicMock:
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(answer).encode()
    return mock.Mock(return_value=response)


class SameComputer(unittest.TestCase):
    def setUp(self):
        self.user_home = Path(tempfile.mkdtemp()).resolve()
        self.home = self.user_home / ".hagency"

    def run_connect(self, *argv, answer=None):
        """connect.py as the person runs it; (install call, what was shown, why it stopped or None)."""
        said = io.StringIO()
        with mock.patch.object(sys, "argv", ["connect.py", "--home", str(self.home), "--server", f"https://{S}",
                                             "--agent", "claude", *argv]), \
                mock.patch.object(connect.urllib.request, "urlopen", server(answer or A)), \
                mock.patch.object(connect.cli_path, "startable", lambda b: True), \
                mock.patch.object(agent_service, "kind", return_value="launchd"), \
                mock.patch.object(Path, "home", return_value=self.user_home), \
                mock.patch.object(agent_service, "install", return_value="launchd") as install, \
                mock.patch.object(connect.service_migration, "migrate", return_value=[]), \
                mock.patch.object(connect.os, "execve"), redirect_stdout(said), redirect_stderr(said):
            try:
                connect.main()
                failed = None
            except SystemExit as e:
                failed = str(e)
        return install.call_args, said.getvalue(), failed

    def saved(self, *where: str) -> dict:
        return json.loads(self.home.joinpath(*where, "credentials.json").read_text())

    def old_service(self, creds: dict, owner: str, env: bool = True) -> None:
        """The background service an earlier connect.py wrote for a first agent."""
        text = agent_service.plist(connect.APP, owner, "claude", connect.agent_name(creds),
                                   self.home / "credentials.json", self.home / "logs")
        if not env:                               # older still: no HAGENCY_CREDENTIALS at all
            text = text.replace(f"<key>HAGENCY_CREDENTIALS</key><string>{self.home / 'credentials.json'}</string>", "")
        path = agent_service.service_file("launchd", connect.agent_name(creds), self.user_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_second_person_gets_own_folder_and_first_is_untouched(self):
        self.run_connect("CODE-A")
        before = (self.home / "credentials.json").read_bytes()
        install, said, failed = self.run_connect("CODE-B", answer=B)
        self.assertIsNone(failed)
        self.assertEqual((self.home / "credentials.json").read_bytes(), before)       # A's sign-in: byte for byte
        self.assertEqual(self.saved(B_NAME)["user_id"], B["user_id"])
        self.assertEqual(install.args[3], B_NAME)                                     # its own service label
        creds = install.args[4]
        self.assertEqual(creds, self.home / B_NAME / "credentials.json")
        job = agent_service.plist(connect.APP, B_OWNER, "claude", B_NAME, creds, creds.parent / "logs")
        self.assertIn(f"<string>{creds}</string>", job)                                # HAGENCY_CREDENTIALS
        self.assertIn(f"{self.home / B_NAME / 'logs' / B_NAME}.log", job)               # its own log
        self.assertIn(f"--name {B_NAME} --stop", said)                                  # how to reach it again
        self.assertNotIn("can't start", said)

    def test_second_person_runs_again_without_a_code(self):
        self.run_connect("CODE-A")
        self.run_connect("CODE-B", answer=B)
        install, _, failed = self.run_connect("--name", B_NAME)
        self.assertIsNone(failed)
        self.assertEqual(install.args[4], self.home / B_NAME / "credentials.json")
        self.assertEqual(self.saved()["user_id"], A["user_id"])

    def test_repairing_the_same_agent_replaces_its_own_file(self):
        self.run_connect("CODE-A")
        install, _, failed = self.run_connect("CODE-A2", answer={**A, "password": "made-up-pw-a2"})
        self.assertIsNone(failed)
        self.assertEqual(self.saved()["password"], "made-up-pw-a2")
        self.assertEqual(install.args[4], self.home / "credentials.json")
        self.assertFalse((self.home / "cx-agent").exists())

    def test_repairing_an_agent_kept_aside_replaces_it_there(self):
        self.run_connect("CODE-A")
        self.run_connect("CODE-B", answer=B)
        self.run_connect("CODE-B2", answer={**B, "password": "made-up-pw-b2"})
        self.assertEqual(self.saved(B_NAME)["password"], "made-up-pw-b2")
        self.assertEqual(self.saved()["password"], A["password"])

    def test_someone_already_hit_is_told_and_their_new_code_goes_to_its_own_folder(self):
        self.old_service(A, "cx", env=False)                          # A paired with an older connect.py
        connect.save(self.home / "credentials.json", {**B, "agent": "claude"})   # ... whose next pairing replaced it
        self.old_service(B, B_OWNER)
        _, said, failed = self.run_connect()                          # any later run of the line says so
        self.assertIsNone(failed)
        self.assertIn("Your agent cx-agent can't start: its sign-in was replaced. "
                      "Ask @pm for a new code and run the line it gives you.", said)
        self.assertNotIn(f"Your agent {B_NAME}", said)
        install, said, failed = self.run_connect("CODE-A-NEW")        # A's new code from @pm
        self.assertIsNone(failed)
        self.assertEqual(self.saved()["user_id"], B["user_id"])       # B's sign-in stays
        self.assertEqual(self.saved("cx-agent")["user_id"], A["user_id"])
        self.assertEqual((install.args[3], install.args[4]), ("cx-agent", self.home / "cx-agent" / "credentials.json"))
        self.assertNotIn("can't start", said)

    def test_no_notice_when_each_service_has_its_own_sign_in(self):
        self.run_connect("CODE-A")
        self.old_service(A, "cx")
        self.assertEqual(signin_place.stranded(self.home, user_home=self.user_home), [])

    def test_notice_reads_systemd_units_too(self):
        connect.save(self.home / "credentials.json", B)
        unit = agent_service.unit(connect.APP, "cx", "claude", "cx-agent", self.home / "credentials.json",
                                  self.home / "logs")
        path = agent_service.service_file("systemd", "cx-agent", self.user_home)
        path.parent.mkdir(parents=True)
        path.write_text(unit)
        with mock.patch.object(agent_service, "kind", return_value="systemd"):
            self.assertEqual(signin_place.stranded(self.home, user_home=self.user_home), ["cx-agent"])
            self.assertEqual(signin_place.stranded(self.home, skip="cx-agent", user_home=self.user_home), [])

    def test_a_further_agent_never_replaces_another_agents_file(self):
        folder = self.home / "cx-codex-agent"
        connect.save(folder / "credentials.json", {**A, "user_id": "@cx-codex-agent:elsewhere.example"})
        with self.assertRaises(SystemExit) as e:
            signin_place.place(self.home, folder, {**A, "user_id": f"@cx-codex-agent:{S}", "label": "codex"})
        self.assertIn("nothing was replaced", str(e.exception))


if __name__ == "__main__":
    unittest.main()
