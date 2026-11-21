"""@pm tells the operator something (guard.py's warnings), in a private chat of its own.

  echo "text" | python deploy/pm_alert.py      # in @pm's container, /app (docker compose exec -T pm ...)

The operator is HAGENCY_OPERATOR (OPERATOR in deploy/.env). The chat's id is kept in
data/alerts.json; if the operator has left it, a new one is opened. Exits non-zero if nobody got it.
"""
import json
import os
import sys
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))      # /app: @pm's own modules
from common import setup_accounts  # noqa: E402
from common.bridge_config import ROOT, SERVER_NAME  # noqa: E402
from common.github import PM_BOT  # noqa: E402
from common.setup_accounts import api  # noqa: E402

ROOM_FILE = ROOT / "data" / "alerts.json"


def operator_in(room: str, operator: str, token: str) -> bool:
    """The operator is still joined or invited (so the warning reaches them)."""
    path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/state/m.room.member/{urllib.parse.quote(operator)}"
    return api("GET", path, token=token).get("membership") in ("join", "invite")


def alert_room(operator: str, token: str) -> str:
    saved = json.loads(ROOM_FILE.read_text()) if ROOM_FILE.exists() else {}
    room = saved.get(operator)
    if room and operator_in(room, operator, token):
        return room
    made = api("POST", "/_matrix/client/v3/createRoom", {"is_direct": True, "preset": "trusted_private_chat",
                                                         "name": "Server warnings", "invite": [operator]}, token=token)
    if "room_id" not in made:
        raise SystemExit(f"✗ couldn't open a chat with {operator}: {made.get('errcode')}")
    ROOM_FILE.parent.mkdir(exist_ok=True)
    ROOM_FILE.write_text(json.dumps({**saved, operator: made["room_id"]}))
    return made["room_id"]


def send(text: str, localpart: str) -> str:
    """The warning, from @pm to the operator; returns the room it went to."""
    operator = f"@{localpart}:{SERVER_NAME}"
    token = setup_accounts.login(PM_BOT, setup_accounts.load_env()[setup_accounts.password_key(PM_BOT)])
    try:
        room = alert_room(operator, token)
        sent = api("PUT", f"/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message/"
                          f"alert-{uuid.uuid4().hex}", {"msgtype": "m.notice", "body": text}, token=token)
        if "event_id" not in sent:
            raise SystemExit(f"✗ couldn't send the warning: {sent.get('errcode')}")
        return room
    finally:
        api("POST", "/_matrix/client/v3/logout", {}, token=token)


def main() -> None:
    localpart = os.environ.get("HAGENCY_OPERATOR", "")
    text = sys.stdin.read().strip()
    if not localpart:
        raise SystemExit("✗ no OPERATOR in deploy/.env: the warning is only in the journal")
    if text:
        print(f"✓ sent to @{localpart} in {send(text, localpart)}")


if __name__ == "__main__":
    main()
