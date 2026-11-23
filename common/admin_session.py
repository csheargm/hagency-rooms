"""admin-bot's one kept sign-in, shared by @pm and the host tools (invite.sh token, bootstrap's exempt-pm).

Overnight live test 2026-11-23: every admin call signed in afresh, and Synapse allows an account only a
few sign-ins before one every ~5.5 min, so invite tokens and agent changes waited minutes ("Synapse limits
admin-bot's sign-ins: waiting 333s"). Now the session is kept in data/ (git-ignored, mode 600, kept across
releases like the password in .env) and reused for at most MAX_AGE_S, then signed out and replaced: one
sign-in an hour, not one per call. Refreshes are serialized across processes (a lock file), so two of
them never both sign in and leave a session nobody tracks (GPT review). A session Synapse no longer
knows (401 M_UNKNOWN_TOKEN) is replaced once and the call made again.
"""
import contextlib
import fcntl
import json
import logging
import math
import os
import tempfile
import time

from common import setup_accounts
from common.bridge_config import ROOT
from common.setup_accounts import api

log = logging.getLogger("loop")
FILE = ROOT / "data" / ".admin-bot-session.json"
MAX_AGE_S = 3600


@contextlib.contextmanager
def locked():
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FILE.with_suffix(".lock"), "a") as f:   # @pm's threads and the host tools: one refresh at a time
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def read() -> dict | None:
    """The kept {"token", "at"}, or None when there is none or it is damaged (a fresh sign-in replaces it)."""
    try:
        kept = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return None
    at = kept.get("at") if isinstance(kept, dict) else None
    if not isinstance(kept, dict) or not isinstance(kept.get("token"), str) or not isinstance(at, (int, float)) \
            or isinstance(at, bool) or not math.isfinite(at):
        return None
    return kept


def retire(token: str) -> None:
    r = api("POST", "/_matrix/client/v3/logout", {}, token=token)
    if r.get("_status") not in (None, 401):   # 401: already gone. Else it may stay valid: say so, never hide it
        log.warning("couldn't sign out admin-bot's old session: HTTP %s %s", r.get("_status"), r.get("errcode", ""))


def write(token: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=FILE.parent, prefix=".admin-bot-session.", suffix=".tmp")   # O_EXCL, mode 600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"token": token, "at": time.time()}, f)
        os.replace(tmp, FILE)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        retire(token)                         # not kept: never left signed in untracked
        raise


def current(user: str, password: str, failed: str | None = None) -> str:
    """The kept token if young enough and not the one that just failed; else sign in, under the lock."""
    with locked():
        kept = read()
        if kept and kept["token"] != failed and 0 <= time.time() - kept["at"] < MAX_AGE_S:
            return kept["token"]              # another process may have replaced it meanwhile: use theirs
        if kept and kept["token"] != failed:
            retire(kept["token"])             # an hour old
        token = setup_accounts.login(user, password)
        write(token)
        return token


def caller(user: str, password: str):
    """call(method, path, body) -> dict on the kept session, signing in only when there is none."""
    token = current(user, password)

    def call(method: str, path: str, body: dict | None) -> dict:
        nonlocal token
        r = api(method, path, body, token=token)
        if r.get("_status") == 401 and r.get("errcode") == "M_UNKNOWN_TOKEN":   # signed out elsewhere: once
            token = current(user, password, failed=token)
            r = api(method, path, body, token=token)
        return r
    return call
