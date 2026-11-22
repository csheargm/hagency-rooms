"""Where this machine's Hagency processes connect, and the agent's password.

On the server everything comes from this folder: Synapse at http://127.0.0.1:8648 and the
passwords in the git-ignored .env. On a person's own computer, connect.py saves the credentials it
was given (homeserver, user_id, password, owner) in a 0600 file under ~/.hagency/ and runs the agent adapter
with HAGENCY_CREDENTIALS pointing at it; then the homeserver and server name come from that file.
HAGENCY_HOMESERVER / HAGENCY_SERVER_NAME override the defaults without a file.
"""
import json
import os
import re
from pathlib import Path

from common import identity
from common import paths

ROOT = paths.ROOT
DEFAULT_HOMESERVER = "http://127.0.0.1:8648"
DEFAULT_SERVER_NAME = "bridge.localhost"
CREDENTIAL_KEYS = ("homeserver", "user_id", "password", "owner")
ELEMENT_ORIGIN = os.environ.get("ELEMENT_ORIGIN", "http://localhost:8649")   # where people open Element
# The Element apps that may show the Hagency panel (frame it, and talk to it over the widget API): ours,
# Element Desktop (vector://vector: seen live, the panel was blank there) and Element's own web apps,
# which people from other servers use. HAGENCY_PANEL_EMBEDDERS (spaces between) replaces the list;
# ours is always in it. Never "*": the panel signs people in with a token from the app framing it.
EMBEDDERS_DEFAULT = "vector://vector https://app.element.io https://develop.element.io https://staging.element.io"
EMBEDDER_RE = re.compile(r"https://[a-z0-9.-]+(:\d{1,5})?|http://localhost(:\d{1,5})?|vector://vector")


def panel_embedders() -> list[str]:
    """ELEMENT_ORIGIN first, then each listed origin that is exactly one (a "*", a path or anything else
    that isn't: dropped, ours too: GPT review, it goes into the CSP and the page's script)."""
    listed = os.environ.get("HAGENCY_PANEL_EMBEDDERS", EMBEDDERS_DEFAULT).split()
    return list(dict.fromkeys(o for o in (ELEMENT_ORIGIN, *listed) if EMBEDDER_RE.fullmatch(o)))


def load_credentials(path: str | None) -> dict:
    """The connect.py credentials file, checked; {} when there is none."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"can't read the Hagency credentials file {path}: {e}") from e
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in CREDENTIAL_KEYS):
        raise SystemExit(f"the Hagency credentials file {path} is incomplete; run connect.py again with a new code")
    if not credentials_match(data["user_id"], data["owner"], data.get("label")):
        raise SystemExit(f"the Hagency credentials file {path} doesn't belong to {data['owner']}'s agent")
    return data


def credentials_match(user_id: str, owner: str, label: str | None = None) -> bool:
    """`user_id` is exactly `owner`'s agent account (owner: a full ID, or a bare name on its server),
    or with a label, exactly that further agent of theirs (identity.py)."""
    server = user_id.partition(":")[2]
    if not server or (label is not None and not (isinstance(label, str) and identity.LABEL_RE.fullmatch(label))):
        return False
    return user_id == f"@{identity.agent_localpart(owner, server, label)}:{server}"


def agent_handle(owner_id: str) -> str:
    """The name this machine runs `owner_id`'s agent under (its localpart without -agent): the one in
    connect.py's credentials file when there is one (a person may have several agents), else the first."""
    if CREDENTIALS:
        if identity.person_id(CREDENTIALS["owner"], SERVER_NAME) != owner_id:
            raise SystemExit(f"the credentials file is {CREDENTIALS['owner']}'s agent, not {owner_id}'s")
        return CREDENTIALS["user_id"].lstrip("@").partition(":")[0].removesuffix(identity.AGENT_SUFFIX)
    return identity.handle(owner_id, SERVER_NAME)


def password_key(user: str) -> str:
    """The .env name of an account's password (localparts here are [a-z0-9-])."""
    return "PW_" + user.upper().replace("-", "_")


CREDENTIALS = load_credentials(os.environ.get("HAGENCY_CREDENTIALS"))
HOMESERVER = CREDENTIALS.get("homeserver") or os.environ.get("HAGENCY_HOMESERVER") or DEFAULT_HOMESERVER
SERVER_NAME = (CREDENTIALS.get("user_id", "").partition(":")[2] or os.environ.get("HAGENCY_SERVER_NAME")
               or DEFAULT_SERVER_NAME)


def read_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v
    return env


def agent_password(agent: str) -> str:
    """The password of agent account `agent` (a localpart): from the credentials file on a person's
    computer, else .env."""
    if CREDENTIALS:
        if CREDENTIALS["user_id"] != f"@{agent}:{SERVER_NAME}":
            raise SystemExit(f"the credentials file is for {CREDENTIALS['user_id']}, not @{agent}")
        return CREDENTIALS["password"]
    return read_env()[password_key(agent)]
