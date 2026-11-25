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
            or isinstance(at, bool):
        return None
    try:
        return kept if math.isfinite(at) else None
    except OverflowError:                     # a huge integer (GPT review): damaged, replaced like any other
        return None


def retire(token: str) -> bool:
    """Sign the session out; True when it's gone (401: already gone). Else it may stay valid: said, never hidden."""
    try:
        r = api("POST", "/_matrix/client/v3/logout", {}, token=token)
    except (OSError, ValueError) as e:        # no answer, or not JSON: still kept, tried again later
        log.warning("couldn't sign out admin-bot's old session: %s", type(e).__name__)
        return False
    if r.get("_status") not in (None, 401):
        log.warning("couldn't sign out admin-bot's old session: HTTP %s %s", r.get("_status"), r.get("errcode", ""))
        return False
    return True


def write(token: str) -> None:
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=FILE.parent, prefix=".admin-bot-session.", suffix=".tmp")   # O_EXCL, mode 600
        with os.fdopen(fd, "w") as f:
            json.dump({"token": token, "at": time.time()}, f)
        os.replace(tmp, FILE)
    except OSError:
        with contextlib.suppress(OSError, TypeError):
            os.unlink(tmp)
        with contextlib.suppress(OSError):    # the write error is the one to report (GPT review)
            retire(token)                         # not kept: never left signed in untracked
        raise


class Limited(Exception):
    """Synapse asked admin-bot's sign-in to wait (try_login's sleep, turned into a way out of the lock)."""


def limited(pause: float) -> None:
    raise Limited(pause)


def current(user: str, password: str, failed: str | None = None) -> str:
    """The kept token if young enough and not the one that just failed; else sign in, under the lock.
    A sign-in Synapse holds back is waited out OUTSIDE the lock (GPT review: other admin calls queued for
    up to LOGIN_WAIT_S), then the file is read again: another process may have signed in meanwhile."""
    waited, retired = 0.0, set()
    while True:
        with locked():
            kept = read()
            if kept and kept["token"] != failed and 0 <= time.time() - kept["at"] < MAX_AGE_S:
                return kept["token"]          # another process may have replaced it meanwhile: use theirs
            if kept and kept["token"] != failed and kept["token"] not in retired:
                retire(kept["token"])         # an hour old: once, even if a held-back sign-in loops
                retired.add(kept["token"])
            try:
                token = setup_accounts.try_login(user, password, sleep=limited)
            except Limited as e:
                pause = e.args[0]
            else:
                if not token:
                    raise SystemExit(f"login failed for {user}")
                write(token)
                return token
        if waited >= setup_accounts.LOGIN_WAIT_S:
            raise setup_accounts.AccountError(f"Synapse is still limiting @{user}'s sign-ins; try again later")
        pause = min(pause, setup_accounts.LOGIN_WAIT_S - waited)   # the budget is the whole wait (GPT review)
        log.warning("Synapse limits %s's sign-ins: waiting %.0fs", user, pause)
        time.sleep(pause)
        waited += pause


def expire() -> None:
    """An hour-old kept session is signed out even if no admin call comes (final review 2026-11-24: an idle
    session lived on); @pm runs this from its minute check (pm_presence.check_agents)."""
    with locked():
        kept = read()
        if kept and not 0 <= time.time() - kept["at"] < MAX_AGE_S and retire(kept["token"]):
            FILE.unlink(missing_ok=True)      # only once it is really signed out (GPT re-check): else retried


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
