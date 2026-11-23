"""Let newcomers sign up on this local server, like they will on the public one:

  python bin/local_signup.py setup         # once: sign-up with a token, new accounts join #welcome
  python bin/local_signup.py token [uses]  # an invite token for Element's sign-up page (default 1 use, 7 days)
  python bin/local_signup.py exempt-pm     # @pm's messages aren't rate limited (deploy/bootstrap.sh, each deploy)

`setup` edits data/homeserver.yaml with the same settings the public server gets
(deploy/configure_synapse.py) and restarts this folder's Synapse only if something changed.
`token` needs a server admin: it creates the account admin-bot once (password in .env, never
printed) through the shared secret, and uses it only for Synapse's registration-token API.
"""
import contextlib
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from common import github
from common import setup_accounts
from common.bridge_config import ELEMENT_ORIGIN, ROOT, SERVER_NAME
from common.setup_accounts import HS, api, load_env

sys.path.append(str(ROOT / "deploy"))           # the public server's settings, shared
from configure_synapse import WELCOME_LOCALPART, module_settings, set_keys, welcome_settings  # noqa: E402

CONFIG = ROOT / "data" / "homeserver.yaml"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "local" / "docker-compose.yml")]   # local/start.sh's stack
ADMIN = "admin-bot"                    # a -bot: never treated as a person (no welcome, no agent)
TOKEN_DAYS = 7
SIGNUP = {"enable_registration": "true", "registration_requires_token": "true", "allow_guest_access": "false"}
WELCOME_ALIAS = f"#{WELCOME_LOCALPART}:{SERVER_NAME}"


def configure(text: str) -> str:
    return set_keys(text, {**SIGNUP, **welcome_settings(SERVER_NAME), **module_settings(SERVER_NAME)})


def setup() -> bool:
    """Apply the settings; restart Synapse when they changed. True if it restarted."""
    old = CONFIG.read_text()
    new = configure(old)
    if new == old:
        return False
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(new)
    tmp.chmod(CONFIG.stat().st_mode & 0o777)
    tmp.replace(CONFIG)
    try:
        # up, not restart: a container made before the module mount (compose) must be recreated with it
        subprocess.run([*COMPOSE, "up", "-d", "synapse"], cwd=ROOT, check=True)   # this folder's compose only
        subprocess.run([*COMPOSE, "restart", "synapse"], cwd=ROOT, check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        CONFIG.write_text(old)            # not applied: the next `setup` tries again (GPT review)
        raise SystemExit(f"couldn't restart Synapse ({e}); nothing changed, run setup again") from e
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{HS}/_matrix/client/versions", timeout=2):
                return True
        except OSError:
            time.sleep(1)                 # still starting
    CONFIG.write_text(old)                # unconfirmed: the next `setup` applies and checks it again
    raise SystemExit("Synapse didn't come back within 60 s: docker compose -f local/docker-compose.yml logs synapse")


def admin_call(method: str, path: str, body: dict) -> dict:
    """One call to Synapse's admin API, signed in as admin-bot only for this call."""
    with admin_session() as call:
        return call(method, path, body)


@contextlib.contextmanager
def admin_session():
    """Several admin API calls on one admin-bot sign-in (each sign-in counts against Synapse's limit:
    GPT review), signed out at the end. Yields call(method, path, body) -> dict."""
    env = load_env()
    try:
        password, _ = setup_accounts.ensure_user(env, ADMIN, setup_accounts.shared_secret(), admin=True)
    except setup_accounts.AccountError as e:
        raise SystemExit(str(e)) from e
    token = setup_accounts.login(ADMIN, password)
    try:
        yield lambda method, path, body: api(method, path, body, token=token)
    finally:
        api("POST", "/_matrix/client/v3/logout", {}, token=token)     # the admin session ends here


def new_token(uses: int = 1, days: int = TOKEN_DAYS) -> str:
    """A registration token from Synapse's admin API."""
    r = admin_call("POST", "/_synapse/admin/v1/registration_tokens/new",
                   {"uses_allowed": uses, "expiry_time": int((time.time() + days * 86400) * 1000)})
    if "token" not in r:
        raise SystemExit(f"Synapse refused the token: {r.get('errcode')} {r.get('error')}")
    return r["token"]


def exempt_pm() -> None:
    """@pm's own messages are never rate limited (Synapse's per-user override; 0 = no limit). Overnight
    live test 2026-11-23: under load @pm got 429s and slept on each, so "@pm help" took two minutes and
    a slow room creation was retried into two rooms with the same name."""
    user = urllib.parse.quote(f"@{github.PM_BOT}:{SERVER_NAME}")
    r = admin_call("POST", f"/_synapse/admin/v1/users/{user}/override_ratelimit",
                   {"messages_per_second": 0, "burst_count": 0})
    if r.get("messages_per_second") != 0:
        raise SystemExit(f"Synapse refused @pm's rate-limit exemption: {r.get('errcode')} {r.get('error')}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        print("Synapse restarted with sign-up by token" if setup() else "already set up")
        print(f"New accounts join {WELCOME_ALIAS}; @pm creates it when it starts.")
    elif cmd == "token":
        uses = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1
        print(json.dumps({"token": new_token(uses), "uses": uses, "days": TOKEN_DAYS,
                          "sign_up": f"{ELEMENT_ORIGIN}/#/register"}))
    elif cmd == "exempt-pm":
        exempt_pm()
        print("@pm isn't rate limited")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
