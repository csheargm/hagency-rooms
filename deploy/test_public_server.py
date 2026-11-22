"""Unit tests for the public server's tools (guard.py, health.py, pm_alert.py, deploy.sh).
Run from deploy/: python3 -m unittest discover -p 'test_*.py'"""
import datetime
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import guard
import health
import pm_alert

ROUTE = ("Iface\tDestination\tGateway\tFlags\n"
         "docker0\t000011AC\t00000000\t0001\nenp0s6\t00000000\t0100000A\t0003\n")
VNSTAT = {"interfaces": [
    {"name": "docker0", "traffic": {"month": [{"date": {"year": 2026, "month": 11}, "rx": 1, "tx": 9 * guard.TB}]}},
    {"name": "enp0s6", "traffic": {"month": [{"date": {"year": 2026, "month": 10}, "rx": 5, "tx": 7 * guard.TB},
                                             {"date": {"year": 2026, "month": 11}, "rx": 5, "tx": 2 * guard.TB}]}}]}
NOV = datetime.date(2026, 11, 20)


class Guard(unittest.TestCase):
    def setUp(self):
        mock.patch("builtins.print").start()          # main() reports to the journal
        self.addCleanup(mock.patch.stopall)

    def test_only_this_months_traffic_out_of_the_internet_facing_interface(self):
        iface = guard.default_iface(ROUTE)
        self.assertEqual(iface, "enp0s6")
        self.assertEqual(guard.month_tx(VNSTAT, iface, NOV), 2 * guard.TB)        # not docker0, not October
        self.assertIsNone(guard.month_tx(VNSTAT, iface, datetime.date(2026, 12, 1)))   # no figure yet: not zero
        with self.assertRaises(SystemExit):
            guard.month_tx({}, iface, NOV)                                           # an interface vnstat doesn't watch

    def test_thresholds(self):
        self.assertEqual(guard.decide(0.5, 0), ([], False))
        warnings, pause = guard.decide(0.81, 0)
        self.assertEqual((len(warnings), pause), (1, False))
        self.assertIn("81%", warnings[0])
        warnings, pause = guard.decide(0.1, 1 * guard.TB + 1)
        self.assertEqual((len(warnings), pause), (1, False))
        warnings, pause = guard.decide(0.9, 5 * guard.TB + 1)
        self.assertEqual((len(warnings), pause), (2, True))
        self.assertIn("paused", warnings[1])

    def run_main(self, disk, tx, alert_rc=0):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(guard, "FLAG", Path(d) / "flags" / "media-paused"), \
                mock.patch.object(guard, "measure", return_value=(disk, tx)), \
                mock.patch.object(guard.subprocess, "run", return_value=mock.Mock(returncode=alert_rc)) as run:
            rc = guard.main()
            return rc, guard.FLAG.exists(), run

    def test_pause_sets_the_flag_caddy_reads_and_a_new_month_clears_it(self):
        rc, flagged, run = self.run_main(0.2, 6 * guard.TB)
        self.assertEqual((rc, flagged), (0, True))
        self.assertIn("paused", run.call_args.kwargs["input"])
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "media-paused"
            flag.touch()
            with mock.patch.object(guard, "FLAG", flag), mock.patch.object(guard, "measure", return_value=(0.2, 0)), \
                    mock.patch.object(guard.subprocess, "run") as run:
                self.assertEqual(guard.main(), 0)
                self.assertFalse(flag.exists())
                run.assert_not_called()                                              # nothing to say

    def test_missing_figures_keep_this_months_pause_but_not_last_months(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "media-paused"
            with mock.patch.object(guard, "FLAG", flag), mock.patch.object(guard, "measure", return_value=(0.2, None)), \
                    mock.patch.object(guard.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                flag.write_text("2026-11")
                guard.main(NOV)
                self.assertTrue(flag.exists())                                       # vnstat reset mid-month: still paused
                self.assertIn("no traffic figure", run.call_args.kwargs["input"])
                flag.write_text("2026-10")
                guard.main(NOV)
                self.assertFalse(flag.exists())                                      # a new month: downloads resume
                flag.write_text("2026-10")
                with mock.patch.object(guard, "measure", side_effect=SystemExit("vnstat broke")), \
                        self.assertRaises(SystemExit):
                    guard.main(NOV)
                self.assertFalse(flag.exists())                                      # even when measuring fails
            with mock.patch.object(guard, "FLAG", flag), mock.patch.object(guard, "measure", return_value=(0.2, 6 * guard.TB)), \
                    mock.patch.object(guard.subprocess, "run", return_value=mock.Mock(returncode=0)):
                guard.main(NOV)
                self.assertEqual(flag.read_text(), "2026-11")

    def test_a_warning_nobody_got_fails_the_run(self):
        self.assertEqual(self.run_main(0.95, 0, alert_rc=1)[0], 1)

    def test_caddy_pauses_exactly_media_downloads(self):
        caddy = (Path(__file__).parent / "Caddyfile").read_text()
        self.assertIn("try_files /media-paused", caddy)
        self.assertIn("./flags:/flags:ro", (Path(__file__).parent / "docker-compose.yml").read_text())


class Health(unittest.TestCase):
    def test_sign_up_must_need_a_token_or_an_email_in_every_flow(self):
        tok = {"stages": ["m.login.registration_token", "m.login.dummy"]}
        mail = {"stages": ["m.login.email.identity"]}                              # open sign-up (signup.py)
        self.assertTrue(health.verified_only(json.dumps({"flows": [tok]})))
        self.assertTrue(health.verified_only(json.dumps({"flows": [mail]})))
        self.assertTrue(health.verified_only(json.dumps({"flows": [tok, {"stages": ["m.login.registration_token", "m.login.email.identity"]}]})))
        self.assertFalse(health.verified_only(json.dumps({"flows": [tok, {"stages": ["m.login.dummy"]}]})))
        self.assertFalse(health.verified_only(json.dumps({"flows": [mail, {"stages": ["m.login.recaptcha"]}]})))
        self.assertFalse(health.verified_only(json.dumps({})))                     # no flows: not proof

    def test_the_configured_mode_must_be_the_live_one(self):
        """GPT review: closing sign-up must be seen to take effect, not just "some verification"."""
        tok = {"flows": [{"stages": ["m.login.registration_token", "m.login.dummy"]},
                         {"stages": ["m.login.registration_token", "m.login.email.identity"]}]}
        mail = {"flows": [{"stages": ["m.login.email.identity"]}]}
        self.assertTrue(health.verified_only(json.dumps(tok), "token"))
        self.assertFalse(health.verified_only(json.dumps(mail), "token"))           # still open: fails
        self.assertTrue(health.verified_only(json.dumps(mail), "open"))
        self.assertFalse(health.verified_only(json.dumps({"flows": tok["flows"][:1]}), "open"))
        both = {"flows": [{"stages": ["m.login.registration_token", "m.login.email.identity"]}]}
        self.assertFalse(health.verified_only(json.dumps(both), "open"))          # open, yet a token needed
        with mock.patch.object(health, "run", return_value=True) as ran:
            self.assertEqual(health.main(["--internal", "m", "m", "token"]), 0)
            self.assertIn("an invite token", ran.call_args[0][0][3][0])
        with mock.patch("builtins.print"):
            self.assertEqual(health.main(["--internal", "m", "m", "wide-open"]), 2)

    def test_failures_and_errors_both_count(self):
        def boom():
            raise OSError("refused")
        with mock.patch("builtins.print"):
            self.assertTrue(health.run([("a", lambda: True)]))
            self.assertFalse(health.run([("a", lambda: True), ("b", lambda: False)]))
            self.assertFalse(health.run([("c", boom)]))
            self.assertFalse(health.run([("d", lambda: "")]))                         # empty is not a pass

    def test_public_checks_include_federation_and_a_closed_admin_api(self):
        names = [n for n, _ in health.checks("https://r", "https://m", "https://r", "m", "m", public=True)]
        self.assertTrue(any("8448" in n for n in names) and any("admin" in n for n in names))
        self.assertEqual(len(health.checks("http://e", "http://s", "http://l", "m", "m", public=False)), 7)


class Alert(unittest.TestCase):
    def test_reuses_the_operators_chat_and_opens_a_new_one_if_they_left(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(pm_alert, "ROOM_FILE", Path(d) / "alerts.json"):
            calls = []

            def api(method, path, body=None, token=None):
                calls.append((method, path))
                if "createRoom" in path:
                    return {"room_id": f"!new{len(calls)}"}
                return {"membership": "leave"}
            with mock.patch.object(pm_alert, "api", side_effect=api):
                first = pm_alert.alert_room("@op:x.org", "t")
                self.assertEqual(json.loads(pm_alert.ROOM_FILE.read_text()), {"@op:x.org": first})
                second = pm_alert.alert_room("@op:x.org", "t")               # they left: a new chat
            self.assertNotEqual(first, second)
            with mock.patch.object(pm_alert, "api", return_value={"membership": "join"}):
                self.assertEqual(pm_alert.alert_room("@op:x.org", "t"), second)

    def test_no_operator_is_an_error_not_silence(self):
        with mock.patch.dict("os.environ", {"HAGENCY_OPERATOR": ""}), mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = "disk full"
            with self.assertRaises(SystemExit) as e:
                pm_alert.main()
        self.assertIn("OPERATOR", str(e.exception))


class Deploy(unittest.TestCase):
    def deploy(self, commit):
        script = Path(__file__).parent / "deploy.sh"
        return subprocess.run([str(script), commit], capture_output=True, text=True, timeout=120,
                              env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HAGENCY_VM": "no-such-host.invalid",
                                   "HOME": tempfile.gettempdir()})

    def test_refuses_a_real_commit_that_is_not_on_master_before_touching_the_vm(self):
        """GPT review 22: an existing commit, not just an unknown id."""
        repo = Path(__file__).resolve().parent.parent
        git = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@example.org", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        tree = subprocess.run(["git", "-C", str(repo), "write-tree"], capture_output=True, text=True, env=git)
        if tree.returncode != 0:
            self.skipTest("no git index here")
        stray = subprocess.run(["git", "-C", str(repo), "commit-tree", tree.stdout.strip(), "-m", "not on master"],
                               capture_output=True, text=True, env=git, check=True).stdout.strip()   # dangling: gc removes it
        out = self.deploy(stray)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertIn("not on GitHub's master", out.stdout)
        self.assertNotIn("Deploying", out.stdout)                                  # stopped before ssh

    def test_refuses_an_unknown_commit(self):
        out = self.deploy("0" * 40)
        self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
        self.assertNotIn("Deploying", out.stdout)

    def test_admin_only_on_the_admin_host_and_only_through_cloudflare(self):
        caddy = (Path(__file__).parent / "Caddyfile").read_text()
        rooms, matrix, admin = (caddy.split("{$ROOMS_DOMAIN} {")[1].split("\n}")[0],
                                caddy.split("{$MATRIX_DOMAIN}, {$MATRIX_DOMAIN}:8448 {")[1].split("\n}")[0],
                                caddy.split("{$ADMIN_DOMAIN} {")[1].split("\n}")[0])
        for block in (rooms, matrix):
            self.assertIn("handle /_synapse/admin* {\n\t\trespond 404", block)
        route = admin.split("route {")[1]
        order = [route.index(x) for x in ("respond @not_cloudflare 403", "respond @no_access_token 403",
                                          "reverse_proxy /_synapse/admin*", "reverse_proxy synapse-admin:8080")]
        self.assertEqual(order, sorted(order))                                     # refusals first, as written
        self.assertIn("not header Cf-Access-Jwt-Assertion *", admin)
        for net in ("173.245.48.0/20", "104.16.0.0/13", "2606:4700::/32", "2c0f:f248::/32"):
            self.assertIn(net, admin)

    def test_bootstrap_cant_swallow_the_rest_of_the_remote_script(self):
        """Live: `docker compose exec` read deploy.sh's stdin (the script), so .deployed was never written."""
        self.assertIn('"$app/deploy/bootstrap.sh" </dev/null', (Path(__file__).parent / "deploy.sh").read_text())

    def test_pm_runs_as_the_recorded_deploy_user_never_root(self):
        here = Path(__file__).parent
        boot = (here / "bootstrap.sh").read_text()
        self.assertIn('[ "$(get PM_UID)" = "$(id -u)" ]', boot)
        self.assertIn("not root", boot)
        self.assertIn("env -u PM_UID docker compose", boot)                       # the shell can't override .env
        self.assertIn('user: "${PM_UID}:${PM_UID}"', (here / "docker-compose.yml").read_text())

    def test_the_site_is_not_served_here_any_more(self):
        here = Path(__file__).parent
        for name in ("Caddyfile", "docker-compose.yml", "bootstrap.sh"):
            text = (here / name).read_text()
            self.assertNotIn("/srv/site", text)
            self.assertNotIn("SITE_DOMAIN", text)


if __name__ == "__main__":
    unittest.main()
