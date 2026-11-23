"""One GitHub sign-in for a person and their agent (ADR 0017): the agent proves its account with a gist."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client import github_check as client_proof
from common import github
from server import github_link
from server import github_proof

AGENT, OWNER = "anna-agent", "@anna:bridge.localhost"


def gist(nonce, login="anna-gh", gid=42, **kw):
    return {"files": {"hagency-check.txt": {"content": nonce}}, "owner": {"login": login, "id": gid}, **kw}


class Server(unittest.TestCase):
    def setUp(self):
        github_proof._pending.clear()
        github_proof._asked.clear()
        links = github_link.SavedDict(Path(tempfile.mkdtemp()) / "links.json")
        p = mock.patch.object(github_link, "links", lambda: links)
        p.start()
        self.addCleanup(p.stop)

    def prove(self, nonce, data, agent=AGENT, owner=OWNER, gist_id="a" * 32):
        with mock.patch.object(github_proof, "read_gist", return_value=data):
            return asyncio.run(github_proof.prove(agent, owner, nonce, gist_id))

    def test_the_gists_owner_from_github_becomes_the_persons_account(self):
        nonce = github_proof.challenge(AGENT, OWNER)
        self.assertEqual(self.prove(nonce, gist(nonce)), (200, {"login": "anna-gh"}))
        self.assertEqual((github_link.link_of(OWNER)["login"], github_link.link_of(OWNER)["id"]), ("anna-gh", 42))

    def test_a_check_works_once_and_only_for_its_own_agent(self):
        nonce = github_proof.challenge(AGENT, OWNER)
        self.assertEqual(self.prove(nonce, gist(nonce), agent="bob-agent")[0], 400)   # not its check
        self.assertEqual(self.prove(nonce, gist(nonce))[0], 400)                        # used up already

    def test_a_gist_without_the_check_or_a_fork_proves_nothing(self):
        for data in (gist("something else"), gist("N", fork_of={"id": "x"}), gist("N", gid=True), gist("N", login="bad login")):
            github_proof._asked.clear()
            nonce = github_proof.challenge(AGENT, OWNER)
            data = json.loads(json.dumps(data).replace('"N"', json.dumps(nonce)))
            self.assertEqual(self.prove(nonce, data)[0], 400, data)
        self.assertIsNone(github_link.link_of(OWNER))

    def test_a_different_linked_account_is_never_replaced(self):
        github_link.save_link(OWNER, "anna-old", 7)
        nonce = github_proof.challenge(AGENT, OWNER)
        status, out = self.prove(nonce, gist(nonce))
        self.assertEqual(status, 409)
        self.assertIn("@anna-old", out["error"])
        self.assertEqual(github_link.link_of(OWNER)["id"], 7)

    def test_bad_gist_ids_and_too_many_checks(self):
        nonce = github_proof.challenge(AGENT, OWNER)
        self.assertEqual(self.prove(nonce, gist(nonce), gist_id="../x")[0], 400)
        for _ in range(github_proof.PER_AGENT_PER_HOUR - 1):
            self.assertIsNotNone(github_proof.challenge(AGENT, OWNER))
        self.assertIsNone(github_proof.challenge(AGENT, OWNER))

    def test_only_an_agents_own_token_on_this_server_counts(self):
        for user, owner, ok in (("@anna-agent:bridge.localhost", OWNER, True), ("@anna:bridge.localhost", OWNER, False),
                                ("@anna-agent:other.server", OWNER, False), ("@anna-agent:bridge.localhost", None, False)):
            with mock.patch.object(github_proof.page_session, "whoami", return_value=user), \
                    mock.patch.object(github_proof.registry, "owner_of", return_value=owner), \
                    mock.patch.object(github_proof, "SERVER_NAME", "bridge.localhost"):
                self.assertEqual(github_proof.agent_and_owner("t" * 20) is not None, ok, user)


class Client(unittest.TestCase):
    CREDS = {"user_id": "@anna-agent:bridge.localhost", "homeserver": "https://m.example", "password": "pw"}

    def setUp(self):
        p = mock.patch.object(github, "IDENTITY_ROOT", Path(tempfile.mkdtemp()))
        p.start()
        self.addCleanup(p.stop)
        self.posts, self.gh = [], []

    def post(self, url, body, token=None):
        self.posts.append(url)
        if url.endswith("/login"):
            return {"access_token": "tok"}
        if url.endswith("/challenge"):
            return {"nonce": "N1"}
        if url.endswith("/proof"):
            return {"login": "anna-gh"} if body == {"nonce": "N1", "gist": "g1"} else {"error": "bad"}
        return {}

    def fake_gh(self, args, env, **kw):
        self.gh.append(args)
        assert env["GH_CONFIG_DIR"].endswith("anna-agent")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"id": "g1"}) if "POST" in args else "")

    def test_it_proves_with_a_gist_deletes_it_and_signs_out(self):
        with mock.patch("builtins.print"):
            login = client_proof.prove(self.CREDS, "https://rooms.example", run=self.fake_gh, post=self.post)
        self.assertEqual(login, "anna-gh")
        self.assertTrue(any("files[hagency-check.txt][content]=N1" in a for a in self.gh[0]))
        self.assertEqual(self.gh[-1][-1], "gists/g1")                        # deleted
        self.assertTrue(self.posts[-1].endswith("/logout"))                  # the one-off session ends

    def test_a_refusal_is_said_and_never_fatal(self):
        def post(url, body, token=None):
            return {"access_token": "tok"} if url.endswith("/login") else {"error": "too many checks"}
        with mock.patch("builtins.print") as said:
            self.assertIsNone(client_proof.prove(self.CREDS, "https://rooms.example", run=self.fake_gh, post=post))
        self.assertIn("too many checks", str(said.call_args_list))


if __name__ == "__main__":
    unittest.main()
