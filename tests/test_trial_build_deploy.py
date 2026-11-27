"""The trial-worker's isolation in deploy/docker-compose.yml (ADR 0027): its own network, only trial.env and
the Trial App's key, read-only code, hardened; @pm and the worker share only the queue folder.
Run: .venv/bin/python -m unittest tests.test_trial_build_deploy"""
import re
import unittest
from pathlib import Path

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def service(name: str) -> str:
    text = (DEPLOY / "docker-compose.yml").read_text()
    m = re.search(rf"^  {name}:[^\n]*\n(.*?)(?=^  [a-z#-]|^networks:)", text, re.S | re.M)
    return m.group(1)


class WorkerIsolation(unittest.TestCase):
    def test_it_is_hardened_and_small(self):
        w = service("trial-worker")
        for line in ("read_only: true", "cap_drop: [ALL]", '"no-new-privileges:true"', "pids_limit: 128",
                     "cpus: 0.5", "mem_limit: 512m", "networks: [trial-out]", "restart: on-failure"):
            self.assertIn(line, w)

    def test_it_never_gets_pms_secrets_or_networks(self):
        w = service("trial-worker")
        for never in ("--env-file", "./.env", "pm-secret", "./secrets:", "../../pm:", "edge", "db]", "synapse"):
            self.assertNotIn(never, w)
        self.assertIn("./trial.env", w)
        self.assertIn("./trial-secrets:/run/hagency-trial:ro", w)
        self.assertIn("../server:/app/server:ro", w)

    def test_pm_gets_the_shared_queue_but_no_trial_secrets(self):
        pm = service("pm")
        self.assertIn("../../trial-shared:/app/data/trial-shared", pm)
        for never in ("trial-secrets", "E2B_API_KEY", "TRIAL_APP_ID", "trial.env"):
            self.assertNotIn(never, pm)

    def test_the_trial_env_and_key_are_never_committed(self):
        ignored = (DEPLOY / ".gitignore").read_text().split()
        self.assertIn("trial.env", ignored)
        self.assertIn("trial-secrets/", ignored)
        example = (DEPLOY / "trial.env.example").read_text()
        self.assertRegex(example, r"(?m)^E2B_API_KEY=$")
        self.assertRegex(example, r"(?m)^OLLAMA_API_KEY=$")

    def test_the_start_script_exits_quietly_when_not_configured(self):
        script = (DEPLOY / "trial_worker_start.sh").read_text()
        self.assertIn('if [ -z "${E2B_API_KEY:-}" ]; then', script)
        self.assertIn("exec /venv/bin/python -m server.trial_worker", script)

    def test_the_e2b_sdk_is_pinned_for_the_worker_only(self):
        root = DEPLOY.parent
        self.assertIn("e2b==2.66.0", (DEPLOY / "requirements-trial.txt").read_text())
        self.assertNotIn("e2b", (root / "requirements.txt").read_text())


if __name__ == "__main__":
    unittest.main()
