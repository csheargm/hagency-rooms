"""admin-bot's one kept sign-in, shared by @pm and the host tools (invite.sh token, bootstrap's exempt-pm).

Overnight live test 2026-11-23: every admin call signed in afresh, and Synapse allows an account only a
few sign-ins before one every ~5.5 min, so invite tokens and agent changes waited minutes ("Synapse limits
admin-bot's sign-ins: waiting 333s"). Now the session is kept in data/ (git-ignored, mode 600, beside the
password in .env) and reused for at most MAX_AGE_S, then replaced: one sign-in an hour, not one per call.
A session Synapse no longer knows (signed out elsewhere) is replaced at once and the call made again.
"""
import json
import os
import time

from common import setup_accounts
from common.bridge_config import ROOT
from common.setup_accounts import api

FILE = ROOT / "data" / ".admin-bot-session.json"
MAX_AGE_S = 3600


def saved() -> str | None:
    """The kept session's token if it is young enough. The old one is signed out when it is replaced."""
    try:
        kept = json.loads(FILE.read_text())
    except (OSError, ValueError):          # none yet, or damaged: a fresh sign-in replaces it
        return None
    if not isinstance(kept, dict) or not isinstance(kept.get("token"), str):
        return None
    if not 0 <= time.time() - float(kept.get("at", 0)) < MAX_AGE_S:
        api("POST", "/_matrix/client/v3/logout", {}, token=kept["token"])   # retired: never left signed in
        return None
    return kept["token"]


def fresh(user: str, password: str) -> str:
    """Sign in and keep the session (written whole, readable by this account only)."""
    token = setup_accounts.login(user, password)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(f".{os.getpid()}.tmp")   # @pm and a host tool may write at once: readers never see half
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "at": time.time()}, f)
    tmp.replace(FILE)
    return token


def caller(user: str, password: str):
    """call(method, path, body) -> dict on the kept session, signing in only when there is none."""
    token = saved() or fresh(user, password)

    def call(method: str, path: str, body: dict | None) -> dict:
        nonlocal token
        r = api(method, path, body, token=token)
        if r.get("errcode") == "M_UNKNOWN_TOKEN":   # signed out elsewhere: one fresh sign-in, the call again
            token = fresh(user, password)
            r = api(method, path, body, token=token)
        return r
    return call
