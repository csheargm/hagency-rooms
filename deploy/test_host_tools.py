"""Unit tests for hostfiles.py, release.py, admin_user.py and configure_synapse.install.
Run from deploy/: python3 -m unittest discover -p 'test_*.py'"""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admin_user
import configure_synapse
import hostfiles
import release
from test_configure_synapse import ENV, GENERATED


class HostFiles(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_replace_never_writes_through_a_planted_link(self):
        """GPT review 22: root wrote a predictable .tmp a container could have made a symlink."""
        victim = self.dir / "victim"
        victim.write_text("keep")
        target = self.dir / "file"
        (self.dir / "file.tmp").symlink_to(victim)
        (self.dir / ".file.planted").symlink_to(victim)
        hostfiles.replace(target, "new", 0o644)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_public_configs_are_readable_whatever_the_umask(self):
        old = os.umask(0o077)
        try:
            hostfiles.public_configs({"MATRIX_DOMAIN": "m.example.org", "SERVER_NAME": "m.example.org",
                                      "ADMIN_DOMAIN": "admin.example.org", "ROOMS_DOMAIN": "r.example.org"}, self.dir)
        finally:
            os.umask(old)
        for name in ("element-config.json", "synapse-admin-config.json"):
            self.assertEqual((self.dir / name).stat().st_mode & 0o777, 0o644, name)
        self.assertEqual(json.loads((self.dir / "synapse-admin-config.json").read_text()),
                         {"restrictBaseUrl": "https://admin.example.org"})

    def test_only_the_secret_is_copied_read_only(self):
        hs = self.dir / "homeserver.yaml"
        hs.write_text(GENERATED)
        with mock.patch.object(os, "fchown"):
            hostfiles.copy_secret(hs, self.dir / "pm-secret", 1000)
        copy = self.dir / "pm-secret" / hostfiles.SECRET_NAME
        self.assertEqual((copy.read_text(), copy.stat().st_mode & 0o777), ("keep-me", 0o400))
        hs.write_text("server_name: x\n")
        with self.assertRaises(SystemExit):
            hostfiles.copy_secret(hs, self.dir / "pm-secret", 1000)

    def test_a_linked_config_is_refused(self):
        real = self.dir / "elsewhere"
        real.write_text(GENERATED)
        (self.dir / "homeserver.yaml").symlink_to(real)
        with self.assertRaises(SystemExit):
            configure_synapse.install(self.dir / "homeserver.yaml", ENV)

    def test_configure_reports_a_change_and_writes_the_marker_safely(self):
        hs = self.dir / "homeserver.yaml"
        hs.write_text(GENERATED)
        victim = self.dir / "victim"
        victim.write_text("keep")
        (self.dir / configure_synapse.MARKER).symlink_to(victim)
        self.assertTrue(configure_synapse.install(hs, ENV))
        self.assertFalse(configure_synapse.install(hs, ENV))                       # again: nothing to restart
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse((self.dir / configure_synapse.MARKER).is_symlink())


class Release(unittest.TestCase):
    def test_only_the_commit_and_pms_state_survive(self):
        root = Path(tempfile.mkdtemp())
        for rel in ("loop.py", "deploy/guard.py", "old.py", "json.py", ".venv/bin/python", ".env", "agents.json",
                    "data/pairing.json", "loop-state-dms.json", "deploy/stale.sh", "site/index.html"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x")
        (root / "requirements.txt").symlink_to(root / ".env")                  # a planted link at a tracked path
        (root / "logs").symlink_to(Path.home())                                  # a link in place of state
        (root / "loop-state-x.json").mkdir()                                     # a folder posing as a state file
        tracked = {"loop.py", "deploy/guard.py", "requirements.txt", "site"}  # site/ became a file
        release.clear(root, tracked)
        left = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink())
        self.assertEqual(left, [".env", "agents.json", "data/pairing.json", "deploy/guard.py", "loop-state-dms.json", "loop.py"])


class AdminUser(unittest.TestCase):
    def test_the_first_password_is_random_and_never_kept(self):
        with mock.patch.object(admin_user.setup_accounts, "shared_secret", return_value="s"), \
                mock.patch.object(admin_user.setup_accounts, "register", return_value={"user_id": "@owner:x"}) as reg, \
                mock.patch.object(admin_user.setup_accounts, "save_env") as save:
            self.assertEqual(admin_user.ensure("owner"), "created")
        name, password, secret = reg.call_args.args
        self.assertEqual((name, secret, reg.call_args.kwargs), ("owner", "s", {"admin": True}))
        self.assertGreaterEqual(len(password), 32)
        save.assert_not_called()
        with mock.patch.object(admin_user.setup_accounts, "shared_secret", return_value="s"), \
                mock.patch.object(admin_user.setup_accounts, "register", return_value={"_status": 400, "errcode": "M_USER_IN_USE"}):
            with mock.patch.object(admin_user.local_signup, "admin_call", return_value={"admin": True}):
                self.assertEqual(admin_user.ensure("owner"), "exists")
            with mock.patch.object(admin_user.local_signup, "admin_call", return_value={"admin": False}), \
                    self.assertRaises(SystemExit):
                admin_user.ensure("owner")                                       # someone's ordinary account

    def test_the_owners_password_is_typed_twice_and_long(self):
        answers = ["a-long-password", "a-long-password"]
        self.assertEqual(admin_user.ask_password(lambda _p: answers.pop(0)), "a-long-password")
        for answers in (["short", "short"], ["a-long-password", "another-password"]):
            with self.assertRaises(SystemExit):
                admin_user.ask_password(lambda _p, a=answers: a.pop(0))
        with mock.patch.object(admin_user.local_signup, "admin_call", return_value={}) as call:
            admin_user.set_password("owner", "a-long-password")
        self.assertIn("/_synapse/admin/v1/reset_password/", call.call_args.args[1])
        self.assertTrue(call.call_args.args[2]["logout_devices"])




class EnvUpgrade(unittest.TestCase):
    def test_settings_added_later_get_their_defaults(self):
        import envfile
        d = Path(tempfile.mkdtemp())
        (d / ".env.example").write_text("ROOMS_DOMAIN=r.example.org\nADMIN_DOMAIN=admin.example.org\nPOSTGRES_PASSWORD=\n")
        env = d / ".env"
        env.write_text("ROOMS_DOMAIN=rooms.mine.org\nPOSTGRES_PASSWORD=" + "ab" * 32 + "\n")
        env.chmod(0o600)
        with mock.patch.object(envfile, "ENV", env), mock.patch.object(envfile, "EXAMPLE", d / ".env.example"):
            values = envfile.ensure()
        self.assertEqual((values["ROOMS_DOMAIN"], values["ADMIN_DOMAIN"], values["POSTGRES_PASSWORD"]),
                         ("rooms.mine.org", "admin.example.org", "ab" * 32))      # kept, added, kept
        self.assertEqual(values["PM_UID"], str(os.getuid()))                    # whoever runs bootstrap owns ~/hagency/pm

    def test_the_remote_people_settings_are_optional_and_checked(self):
        import envfile
        base = {"ROOMS_DOMAIN": "r.org", "MATRIX_DOMAIN": "m.org", "SERVER_NAME": "m.org", "ADMIN_DOMAIN": "a.org",
                "POSTGRES_PASSWORD": "ab" * 32, "PM_UID": "1001"}
        self.assertEqual(envfile.validate(base), [])
        self.assertEqual(envfile.validate({**base, "ALLOW_REMOTE": "0", "REMOTE_PER_HOUR": "5"}), [])
        self.assertEqual(len(envfile.validate({**base, "ALLOW_REMOTE": "yes please", "REMOTE_PER_HOUR": "-1"})), 2)

    def test_the_trial_agent_settings_are_optional_checked_and_the_key_never_printed(self):
        """ADR 0025: no OLLAMA_API_KEY is the default (trial off); docker-compose passes every setting to @pm."""
        import envfile
        base = {"ROOMS_DOMAIN": "r.org", "MATRIX_DOMAIN": "m.org", "SERVER_NAME": "m.org", "ADMIN_DOMAIN": "a.org",
                "POSTGRES_PASSWORD": "ab" * 32, "PM_UID": "1001"}
        good = {"OLLAMA_API_KEY": "a1b2c3d4e5f6.g7h8i9j0k1l2m3n4", "TRIAL_MODELS": "glm-5.3,kimi-k3.1-pro*2,gpt-oss:120b", "TRIAL_PER_VISITOR": "50",
                "TRIAL_PER_SERVER": "1000", "TRIAL_MAX_TOKENS": "1500", "TRIAL_TIMEOUT_S": "90",
                "TRIAL_MAX_SHARE": "0.5", "TRIAL_MONTHLY_TOKENS": "5000000", "TRIAL_QUEUE_WAIT_S": "20", "TRIAL_PAUSE_S": "900"}
        self.assertEqual(envfile.validate({**base, "OLLAMA_API_KEY": ""}), [])
        self.assertEqual(envfile.validate({**base, **good}), [])
        bad = {"OLLAMA_API_KEY": "has a space", "TRIAL_MODELS": "glm-5.3, ../x", "TRIAL_PER_VISITOR": "-1", "TRIAL_TIMEOUT_S": "9999",
               "TRIAL_MAX_SHARE": "1.5"}
        self.assertEqual(len(envfile.validate({**base, **bad})), 5)
        self.assertIn("OLLAMA_API_KEY", envfile.PRIVATE)
        compose = (Path(__file__).parent / "docker-compose.yml").read_text()
        for name in good:
            self.assertIn(f"{name}: ${{{name}:-", compose)
        example = (Path(__file__).parent / ".env.example").read_text()
        self.assertIn("\nOLLAMA_API_KEY=\n", example)                 # empty by default: off

    def test_the_github_app_settings_are_optional_and_checked(self):
        import envfile
        base = {"ROOMS_DOMAIN": "r.org", "MATRIX_DOMAIN": "m.org", "SERVER_NAME": "m.org", "ADMIN_DOMAIN": "a.org",
                "POSTGRES_PASSWORD": "ab" * 32, "PM_UID": str(os.getuid())}
        self.assertEqual(envfile.app_key_problems(base), [])                      # no App: no key needed
        d = Path(tempfile.mkdtemp())
        key = d / "github-app.pem"
        body = base64.encodebytes(b"\x30\x82\x04\xac" + b"\x01" * 1196).decode()   # one DER SEQUENCE, not a real key
        key.write_text(f"-----BEGIN RSA PRIVATE KEY-----\n{body}-----END RSA PRIVATE KEY-----\n")
        key.chmod(0o600)
        good = {"GITHUB_APP_ID": "123456", "GITHUB_APP_SLUG": "hagency-rooms", "HAGENCY_REPOS": "a/b,c-d/e.f",
                "GITHUB_APP_CLIENT_ID": "Iv23liAbCdEf012345"}
        bad = {"GITHUB_APP_ID": "0", "GITHUB_APP_SLUG": "../x", "HAGENCY_REPOS": "a/b c/d",
               "GITHUB_APP_CLIENT_ID": "0123456789abcdef0123456789abcdef01234567"}   # a client secret's shape
        self.assertEqual(envfile.validate({**base, **good}), [])
        self.assertEqual(len(envfile.validate({**base, **bad})), 4)
        self.assertEqual(envfile.validate({**base, **good, "GITHUB_APP_CLIENT_ID": ""}), [])   # optional
        self.assertEqual(envfile.validate({**base, **good, "HAGENCY_REPOS": ""}), [])   # optional: any repo
        key.unlink()
        self.assertEqual(envfile.validate({**base, **good}), [])      # `get` and configure_synapse never read the key
        key.write_text(f"-----BEGIN RSA PRIVATE KEY-----\n{body}-----END RSA PRIVATE KEY-----\n")
        with mock.patch.object(envfile, "APP_KEY", key):
            # GPT review, GitHub App: `check` (deploy.sh runs it before @pm stops) refuses a bad key
            key.chmod(0o644)
            problems = envfile.app_key_problems({**base, **good})
            self.assertEqual(len(problems), 1)
            self.assertNotIn(body[:20], problems[0])
            key.chmod(0o600)
            self.assertEqual(envfile.app_key_problems({**base, **good}), [])
            self.assertEqual(len(envfile.app_key_problems({**base, **good, "PM_UID": "4242"})), 1)   # not @pm's user
            truncated = base64.encodebytes(b"\x30\x82\x04\xac" + b"\x01" * 1000).decode()
            padded = base64.encodebytes(b"\x30\x83\x00\x04\xac" + b"\x01" * 1196).decode()   # not DER's shortest
            for text in ("not a key", "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----\n",
                         f"-----BEGIN RSA PRIVATE KEY-----\n{truncated}-----END RSA PRIVATE KEY-----\n",
                         f"-----BEGIN RSA PRIVATE KEY-----\n{padded}-----END RSA PRIVATE KEY-----\n",
                         f"-----BEGIN RSA PRIVATE KEY-----\n{body}-----END RSA PRIVATE KEY-----\n" + " " * 17000 + "x"):
                key.write_text(text)
                self.assertIn("isn't a PEM private key", envfile.app_key_problems({**base, **good})[0])
            key.unlink()
            (d / "real.pem").write_text("x")
            key.symlink_to(d / "real.pem")
            self.assertEqual(len(envfile.app_key_problems({**base, **good})), 1)   # a link is never followed
            key.unlink()
            os.mkfifo(key, 0o600)
            self.assertEqual(len(envfile.app_key_problems({**base, **good})), 1)   # a FIFO: refused, never waited on
        compose = (Path(__file__).parent / "docker-compose.yml").read_text()
        self.assertIn("./secrets:/run/hagency-github:ro", compose)               # the key: read-only, never in .env
        self.assertIn("GITHUB_APP_PRIVATE_KEY_PATH: /run/hagency-github/github-app.pem", compose)
        self.assertIn("GITHUB_APP_CLIENT_ID: ${GITHUB_APP_CLIENT_ID:-}", compose)   # Connect GitHub (ADR 0007)
        self.assertIn("secrets/", (Path(__file__).parent / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()


class CaddyPicksUpNewRoutes(unittest.TestCase):
    """Seen live: after a deploy Caddy kept the old Caddyfile (a single-file bind mount keeps the old
    inode when git replaces the file), so /connect/start was 404 until Caddy was restarted."""
    def test_bootstrap_recreates_caddy_when_its_file_differs(self):
        script = (Path(__file__).parent / "bootstrap.sh").read_text()
        self.assertIn("sha256sum Caddyfile", script)
        self.assertIn("sha256sum /etc/caddy/Caddyfile", script)
        self.assertIn("--force-recreate caddy", script)
        self.assertLess(script.index("--force-recreate caddy"), script.index("caddy reload --config"))
