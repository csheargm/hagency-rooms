"""The owner's Synapse admin account, for Synapse Admin (https://<ADMIN_DOMAIN>). Runs in @pm's container.

  python deploy/admin_user.py ensure owner      # bootstrap.sh: create @owner as a server admin, once
  python deploy/admin_user.py password owner    # the owner, in their own terminal: set the password

`ensure` gives the new account a random password that is kept nowhere and never printed: nobody can
sign in with it. The owner then runs `ssh -t hagency-vm '~/hagency/app/deploy/invite.sh owner-password'`
and types a password of their own (twice, not echoed); it goes straight to Synapse's admin API
(as admin-bot, inside Docker) and every other session of that account is signed out.
"""
import getpass
import re
import secrets
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))      # /app: @pm's own modules
from common import local_signup  # noqa: E402
from common import setup_accounts  # noqa: E402
from common.bridge_config import SERVER_NAME  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9]{2,24}$")
MIN_PASSWORD = 12


def ensure(name: str) -> str:
    """Create @name as a server admin unless it exists; 'created' or 'exists'."""
    r = setup_accounts.register(name, secrets.token_urlsafe(32), setup_accounts.shared_secret(), admin=True)
    if r.get("errcode") == "M_USER_IN_USE":
        user = urllib.parse.quote(f"@{name}:{SERVER_NAME}")
        if local_signup.admin_call("GET", f"/_synapse/admin/v2/users/{user}", None).get("admin") is not True:
            raise SystemExit(f"✗ @{name} exists but isn't a server admin; make it one by hand or pick another name")
        return "exists"
    if "_status" in r:
        raise SystemExit(f"✗ couldn't create @{name}: {r.get('errcode')} {r.get('error')}")
    return "created"


def set_password(name: str, password: str) -> None:
    user = urllib.parse.quote(f"@{name}:{SERVER_NAME}")
    r = local_signup.admin_call("POST", f"/_synapse/admin/v1/reset_password/{user}",
                                {"new_password": password, "logout_devices": True})
    if "_status" in r:
        raise SystemExit(f"✗ Synapse refused the new password: {r.get('errcode')} {r.get('error')}")


def ask_password(ask=getpass.getpass) -> str:
    first = ask(f"New password (at least {MIN_PASSWORD} characters): ")
    if len(first) < MIN_PASSWORD:
        raise SystemExit("✗ too short; nothing changed")
    if ask("Again: ") != first:
        raise SystemExit("✗ the two didn't match; nothing changed")
    return first


def main(argv: list[str]) -> None:
    if len(argv) != 2 or argv[0] not in ("ensure", "password") or not NAME_RE.match(argv[1]):
        raise SystemExit(__doc__)
    if argv[0] == "ensure":
        print(f"✓ admin account @{argv[1]}:{SERVER_NAME}: {ensure(argv[1])}")
        return
    if not sys.stdin.isatty():
        raise SystemExit("✗ run this in a terminal (ssh -t ...): the password is typed, never passed in")
    set_password(argv[1], ask_password())
    print(f"✓ @{argv[1]}:{SERVER_NAME}'s password is set; its other sessions are signed out")


if __name__ == "__main__":
    main(sys.argv[1:])
