"""Any GitHub repo a room's owner can write to (owner decision, batch 12–14): with the GitHub App and no
operator list, a room may choose any repo; the App on it and the asker's write access are still
required, and HAGENCY_REPOS, when set, still restricts. Each refusal names the one missing step
(repo_refusals.py). Run: .venv/bin/python -m unittest tests.test_any_repo"""
import asyncio
from unittest import mock

from common import github
from common import github_app
from server import github_link
from server import repo_refusals
from common import room_state
from tests.test_github_app_flows import ANNA
from tests.test_github_link import ANNA_ID, BindCase

ANY = "someone-else/brand-new"                # never listed anywhere


class AnyRepo(BindCase):
    def setUp(self):
        super().setUp()
        github_link.save_link(ANNA, "anna-gh", ANNA_ID)
        p = mock.patch.object(github, "OPERATOR_REPOS", frozenset())   # HAGENCY_REPOS unset
        p.start()
        self.addCleanup(p.stop)

    def choose(self, repo=ANY):
        return asyncio.run(self.lp.set_repo(self.room, ANNA, repo))

    def test_an_unlisted_repo_with_the_app_and_write_access_is_bound(self):
        self.fake.installations[ANY.lower()] = 9
        self.fake.permissions[(ANY, "anna-gh")] = "write"
        self.assertIn(f"now works on **{ANY}**", self.choose())
        self.assertEqual(self.written, [room_state.content("off", ANY)])
        self.assertEqual(room_state.repo_in(self.written[0]), ANY)          # and read back as bound

    def test_without_the_app_it_names_the_install_step(self):
        answer = self.choose()
        self.assertEqual(answer, f"Install the Hagency Rooms app on {ANY} (read-only): {github_app.install_url()}. "
                                 "I'll finish setting up this room as soon as it's installed.")
        self.assertEqual(self.written, [])

    def test_without_write_access_it_names_their_account(self):
        self.fake.installations[ANY.lower()] = 9
        self.fake.permissions[(ANY, "anna-gh")] = "read"
        self.assertEqual(self.choose(), f"Your GitHub account @anna-gh can't write to {ANY}, so this room can't use it.")
        self.assertEqual(self.written, [])

    def test_an_operator_list_still_restricts(self):
        self.fake.installations[ANY.lower()] = 9
        self.fake.permissions[(ANY, "anna-gh")] = "write"
        with mock.patch.object(github, "OPERATOR_REPOS", frozenset({"acme/only"})), \
                mock.patch.object(github, "ALLOWED_REPOS", frozenset({"acme/only"})):
            self.assertEqual(self.choose(), f"This server only allows certain repos, and {ANY} isn't one of them.")
            self.assertIsNone(room_state.repo_in(room_state.content("off", ANY)))   # a list set later unbinds it
        self.assertEqual(self.written, [])
        self.assertFalse(any(ANY.lower() in r[1].lower() for r in self.fake.requests))   # GitHub never asked

    def test_without_the_app_only_the_list_counts(self):
        """Local dev reads GitHub with the host's own gh login: no free choice there."""
        with mock.patch.object(github_app, "APP_ID", ""):
            self.assertFalse(github.any_repo())
            self.assertIsNone(github.allowed_repo(ANY))
            listed = sorted(github.ALLOWED_REPOS)[0]
            self.assertEqual(github.allowed_repo(listed.upper()), listed)        # the list, in its spelling
        self.assertTrue(github.any_repo())
        self.assertEqual(github.allowed_repo(ANY), ANY)
        self.assertIsNone(github.allowed_repo("not a repo"))

    def test_an_agents_process_reads_what_pm_bound(self):
        """An agent's process (no App settings, no list) reads @pm's binding through room_context."""
        from adapters.agents import room_context
        event = {"sender": room_context.PM_ID, "content": room_state.content("off", ANY)}
        with mock.patch.object(github_app, "APP_ID", ""), mock.patch.object(room_context, "api", return_value=event):
            self.assertEqual(room_context.room_repo("tok", "!r"), ANY)
            with mock.patch.object(room_context, "api", return_value={**event, "sender": "@mallory:x"}):
                self.assertIsNone(room_context.room_repo("tok", "!r"))          # only @pm's own state

    def test_githubs_own_spelling_is_kept(self):
        """GPT review: "Someone-Else/Brand-New" and "someone-else/brand-new" are one repo, one key."""
        self.fake.installations[ANY.lower()] = 9
        self.fake.permissions[(ANY.upper(), "anna-gh")] = "write"
        self.fake.names[ANY.lower()] = ANY                                       # as GitHub spells it
        self.assertIn(f"now works on **{ANY}**", self.choose(ANY.upper()))
        self.assertEqual(self.written, [room_state.content("off", ANY)])
