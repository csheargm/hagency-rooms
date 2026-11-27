"""Trial builds' settings (ADR 0027). All of them live in deploy/trial.env, which only the trial-worker
container gets (docker-compose.yml). @pm never sees the E2B key or the Trial App's key: it learns that
builds are on, and the caps, from the worker's heartbeat (trial_jobs.py) in the shared folder.

OFF unless E2B_API_KEY, TRIAL_APP_ID, the Trial App's key file (TRIAL_APP_PRIVATE_KEY_PATH), TRIAL_ORG
and OLLAMA_API_KEY are all set, and TRIAL_BUILD isn't "off" (the kill switch). Off, the worker exits
at once and writes no heartbeat, so @pm offers nothing and nothing changes for anyone.

  python -m server.trial_build_config check    # exit 0: configured (the start script runs the worker)
"""
import os
import re
import sys
from pathlib import Path

from common.paths import ROOT

REQUIRED = ("E2B_API_KEY", "TRIAL_APP_ID", "TRIAL_ORG", "OLLAMA_API_KEY")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")              # an org name
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
DEFAULTS = {
    "TRIAL_BUILDS_PER_DAY": 20,        # the whole server, per UTC day (the demo allowlist bypasses it)
    "TRIAL_BUILD_QUEUE": 5,            # builds waiting; one runs at a time, server-wide
    "TRIAL_BUILD_STEPS": 40,           # tool calls in one build
    "TRIAL_BUILD_S": 600,              # the tool loop's time
    "TRIAL_SANDBOX_TIMEOUT_S": 900,    # E2B kills a sandbox after this, whatever we do
    "TRIAL_BUILD_TOKENS": 150_000,     # model tokens in one build
    "TRIAL_SANDBOX_S_PER_DAY": 7200,   # our soft cap on sandbox time a day (about $0.25)
    "TRIAL_MIN_ACCOUNT_DAYS": 30,      # the GitHub account's age
    "TRIAL_LEASE_DAYS": 7,             # a visitor keeps their demo repo this long
    "TRIAL_TEST_S": 300,               # the test run
}
DEFAULT_TEMPLATE = "hagency-trial-python"
# -P: the repo isn't put first on sys.path, so nothing in it can stand in for pytest; the repo is added by
# pytest itself once it runs (pythonpath=.). The worker adds --junitxml (trial_build.REPORT). GPT review.
DEFAULT_TEST_CMD = "python -P -m pytest -q -p no:cacheprovider -o pythonpath=."
DEFAULT_REPOS = "demo-1,demo-2,demo-3"
SHARED_ENV = "TRIAL_SHARED_DIR"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def number(name: str) -> int:
    """A whole-number setting; missing or malformed: its default; negative: 0."""
    try:
        return max(0, int(env(name)))
    except ValueError:
        return DEFAULTS[name]


def shared_dir() -> Path:
    """The folder @pm and the worker both mount: jobs, the heartbeat, leases, the Ollama slot's lock."""
    return Path(env(SHARED_ENV) or ROOT / "data" / "trial-shared")


def key_path() -> Path:
    return Path(env("TRIAL_APP_PRIVATE_KEY_PATH", "/run/hagency-trial/trial-app.pem"))


def switched_off() -> bool:
    return env("TRIAL_BUILD").lower() in ("off", "0", "no", "false")


def missing() -> list[str]:
    """What isn't configured (names only, never values)."""
    gone = [name for name in REQUIRED if not env(name)]
    if env("TRIAL_ORG") and not NAME_RE.match(env("TRIAL_ORG")):
        gone.append("TRIAL_ORG (not an organisation name)")
    if not key_path().is_file():
        gone.append("TRIAL_APP_PRIVATE_KEY_PATH (no key file)")
    if not repos():
        gone.append("TRIAL_REPOS")
    return gone


def enabled() -> bool:
    return not switched_off() and not missing()


def org() -> str:
    return env("TRIAL_ORG")


def repos() -> list[str]:
    """The demo repos' full names (org/name), from TRIAL_REPOS (names in TRIAL_ORG)."""
    names = [n.strip() for n in env("TRIAL_REPOS", DEFAULT_REPOS).split(",") if n.strip()]
    return [f"{org()}/{n}" for n in names if REPO_NAME_RE.match(n) and n not in (".", "..")] if org() else []


def template() -> str:
    return env("TRIAL_E2B_TEMPLATE", DEFAULT_TEMPLATE)


def test_command() -> str:
    """The template's fixed test command: never one taken from the repo (ADR 0027)."""
    return env("TRIAL_TEST_CMD", DEFAULT_TEST_CMD)


def demo_users() -> list[str]:
    """Matrix IDs that bypass the server's daily cap, so the owner can always demo."""
    return [u.strip() for u in env("TRIAL_BUILD_DEMO_USERS").split(",") if u.strip().startswith("@")]


def caps() -> dict:
    """What @pm needs to gate and count (sent in the worker's heartbeat)."""
    keys = ("TRIAL_BUILDS_PER_DAY", "TRIAL_BUILD_QUEUE", "TRIAL_MIN_ACCOUNT_DAYS")   # a person's own caps: their tier
    return {**{k: number(k) for k in keys}, "demo_users": demo_users()}


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        gone = [] if switched_off() else missing()
        if not enabled():                 # a heartbeat left by an earlier run must not keep the offer up (GPT review)
            (shared_dir() / "worker.json").unlink(missing_ok=True)
        print("trial builds: " + ("switched off (TRIAL_BUILD=off)" if switched_off() else
                                  f"off, not configured: {', '.join(gone)}" if gone else "on"))
        sys.exit(0 if enabled() else 1)
    print(__doc__)
    sys.exit(2)
