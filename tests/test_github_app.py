"""@pm's GitHub App: JWT, installation tokens, gh-shaped reads, and the gh fallback, against a local
fake of GitHub's API (tests/fake_github_api.py). Run: .venv/bin/python -m unittest tests.test_github_app"""
import asyncio
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from common import github
from common import github_app
from server import github_setup
from tests.fake_github_api import FakeGitHub

APP_ID = "424242"
REPO, OTHER = "octo/hello", "octo/other"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption()).decode()


def issue(n, title, state="open", labels=("agent-ok",), pr=False):
    raw = {"number": n, "title": title, "state": state, "body": "b", "labels": [{"name": lb} for lb in labels]}
    return {**raw, "pull_request": {"url": "x"}} if pr else raw


class AppCase(unittest.TestCase):
    """A configured App (key file 0600) talking to the fake; every test starts with no cached token."""

    def setUp(self):
        self.fake = FakeGitHub(APP_ID, _KEY.public_key())
        self.addCleanup(self.fake.close)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.key = Path(self.dir.name) / "github-app.pem"
        self.key.write_text(PEM)
        self.key.chmod(0o600)
        for name, value in (("APP_ID", APP_ID), ("KEY_PATH", str(self.key)), ("API", self.fake.url),
                            ("SLUG", "hagency-rooms"), ("_tokens", {})):
            p = mock.patch.object(github_app, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("common.github.ALLOWED_REPOS", frozenset({REPO, OTHER}))
        p.start()
        self.addCleanup(p.stop)
        self.fake.installations[REPO.lower()] = 7

    def minted(self):
        return [r for r in self.fake.requests if r[1].endswith("/access_tokens")]


class Jwt(AppCase):
    def test_claims_are_what_github_expects(self):
        now = 1_800_000_000
        claims = jwt.decode(github_app.app_jwt(now), _KEY.public_key(), algorithms=["RS256"],
                            options={"verify_exp": False, "verify_iat": False})
        self.assertEqual(claims, {"iss": APP_ID, "iat": now - 60, "exp": now + 540})
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)          # GitHub's maximum
        self.assertEqual(jwt.get_unverified_header(github_app.app_jwt(now))["alg"], "RS256")

    def test_a_key_others_can_read_is_refused(self):
        self.key.chmod(0o644)
        with self.assertRaises(github.GitHubError) as e:
            github_app.app_jwt()
        self.assertIn("mode 600", str(e.exception))
        self.assertNotIn("PRIVATE KEY", str(e.exception))

    def test_a_missing_or_broken_key_never_shows_its_text(self):
        self.key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n")
        with self.assertRaises(github.GitHubError) as e:
            github_app.app_jwt()
        self.assertNotIn("not-a-key", str(e.exception))
        self.key.unlink()
        with self.assertRaises(github.GitHubError) as e:
            github_app.app_jwt()
        self.assertIn("can't read", str(e.exception))


class Tokens(AppCase):
    def test_one_token_per_repo_cached_and_scoped_to_that_repo(self):
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}/labels"))
        self.assertEqual(len(self.minted()), 1)                           # cached: minted once
        tok = github_app._tokens[REPO.lower()][0]
        self.assertEqual(self.fake.tokens[tok], {REPO.lower()})           # that repo only
        self.assertEqual(self.fake.requests[0][0:2], ("GET", f"/repos/{REPO}/installation"))

    def test_a_token_close_to_expiry_is_renewed(self):
        self.fake.token_ttl_s = github_app.TOKEN_MARGIN_S - 10            # already inside the margin
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        self.assertEqual(len(self.minted()), 2)
        self.fake.token_ttl_s = 3600
        github_app._tokens.clear()
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        tok, _ = github_app._tokens[REPO.lower()]
        github_app._tokens[REPO.lower()] = (tok, time.time() + github_app.TOKEN_MARGIN_S - 5)   # ~55 min later
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))              # renewed
        self.assertEqual(len(self.minted()), 4)

    def test_not_installed_gives_the_install_link(self):
        self.assertFalse(asyncio.run(github_app.installed(OTHER)))
        self.assertTrue(asyncio.run(github_app.installed(REPO)))
        with self.assertRaises(github_app.NotInstalled) as e:
            asyncio.run(github.run_json(["issue", "list", "-R", OTHER, "--json", "number"]))
        self.assertIn("https://github.com/apps/hagency-rooms/installations/new", str(e.exception))

    def test_readiness_says_how_to_install_it(self):
        with mock.patch.object(github_setup, "identity", mock.AsyncMock(return_value={"login": "a", "push": True})):
            _, problems = asyncio.run(github_setup.readiness(OTHER, ["anna-agent"], detail=False))
        self.assertEqual(len(problems), 1)
        self.assertIn("isn't installed on octo/other", problems[0])
        self.assertIn("/apps/hagency-rooms/installations/new", problems[0])

    def test_a_collaborators_permission(self):
        self.fake.permissions[(REPO, "anna")] = "write"
        self.assertEqual(asyncio.run(github_app.permission(REPO, "anna")), "write")
        self.assertEqual(asyncio.run(github_app.permission(REPO, "nobody")), "none")
        with self.assertRaises(github.GitHubError):
            asyncio.run(github_app.permission(REPO, "../admin"))


class NoSecrets(AppCase):
    def test_no_key_jwt_or_token_in_errors_or_logs(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("loop").debug("start")                       # assertLogs needs one record
            asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
            tok = github_app._tokens[REPO.lower()][0]
            errors = []
            for args in (["api", f"repos/{REPO}/nope"], ["pr", "view", "1", "-R", REPO, "--json", "state"],
                         ["issue", "view", "99", "-R", REPO, "--json", "state"]):
                try:
                    asyncio.run(github.run_json(args))
                except github.GitHubError as e:
                    errors.append(str(e))
            self.fake.tokens.clear()                                       # GitHub forgets the token...
            self.fake.installations.clear()                                # ...and the App was uninstalled
            try:
                asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
            except github.GitHubError as e:
                errors.append(str(e))
        self.assertEqual(len(errors), 4)
        jwts = [r[2] for r in self.fake.requests if r[2].startswith("ey")]
        secrets = [tok, PEM.splitlines()[1], *jwts]
        for text in [*errors, *logs.output]:
            for secret in secrets:
                self.assertNotIn(secret, text)


    def test_an_upstream_that_echoes_the_credential_is_scrubbed(self):
        """GPT review, GitHub App: GitHub's own message text reaches rooms and logs; never a credential in it."""
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        tok = github_app._tokens[REPO.lower()][0]
        self.fake.echo_auth = True
        with self.assertRaises(github.GitHubError) as e:
            asyncio.run(github_app.get(REPO, f"/repos/{REPO}/nope"))
        self.assertNotIn(tok, str(e.exception))
        self.assertIn("<redacted>", str(e.exception))
        self.assertEqual(github_app.scrub("x eyJa.b-c.d_e ghs_abcdefgh -----BEGIN RSA PRIVATE KEY-----"),
                         "x <redacted> <redacted> <redacted>")


class Retry(AppCase):
    def test_a_refused_token_is_dropped_and_the_read_retried_once(self):
        """GPT review, GitHub App: a revoked token stayed cached for up to 55 minutes."""
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        self.fake.tokens.clear()                                          # GitHub revoked it
        self.assertEqual(asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))["default_branch"], "main")
        self.assertEqual(len(self.minted()), 2)

    def test_refused_twice_keeps_no_token_and_installed_never_trusts_the_cache(self):
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        with mock.patch.object(self.fake, "repo_read", lambda *a: (401, {"message": "Bad credentials"})):
            with self.assertRaises(github.GitHubError):
                asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))
        self.assertNotIn(REPO.lower(), github_app._tokens)
        asyncio.run(github_app.get(REPO, f"/repos/{REPO}"))              # cached again...
        self.fake.installations.clear()                                   # ...then the App is uninstalled
        self.assertFalse(asyncio.run(github_app.installed(REPO)))

    def test_installed_means_a_token_can_be_had(self):
        """GPT review, GitHub App: a found installation that can't give a token (suspended, repo not selected)."""
        self.fake.installations[OTHER.lower()] = 7                        # listed, but the mint will refuse it
        with mock.patch.object(self.fake, "mint", lambda inst, body: (403, {"message": "suspended"})):
            with self.assertRaises(github.GitHubError):
                asyncio.run(github_app.installed(OTHER))


class Fallback(unittest.TestCase):
    def test_unconfigured_reads_use_the_hosts_gh(self):
        with mock.patch.object(github_app, "APP_ID", ""), \
                mock.patch.object(github, "run", mock.AsyncMock(return_value='[{"number": 1}]')) as run, \
                mock.patch("common.github_app_reads.run_json", mock.AsyncMock()) as app:
            out = asyncio.run(github.run_json(["issue", "list", "-R", REPO, "--json", "number"]))
            self.assertTrue(asyncio.run(github_app.installed(REPO)))       # nothing to install
        self.assertEqual(out, [{"number": 1}])
        run.assert_awaited_once()
        app.assert_not_awaited()

    def test_agents_always_use_their_own_gh_identity(self):
        with mock.patch.object(github_app, "APP_ID", APP_ID), \
                mock.patch.object(github, "run", mock.AsyncMock(return_value='{"login": "a"}')) as run:
            asyncio.run(github.run_json(["api", "user"], "anna-agent"))
        run.assert_awaited_once_with(["api", "user"], "anna-agent")


if __name__ == "__main__":
    unittest.main()
