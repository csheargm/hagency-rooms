"""Who signed in to the side panel, for people from other Matrix servers too.

Element's widget API gives the panel an OpenID token and `matrix_server_name`, the server that
issued it. Our own people's tokens are checked with our homeserver (console.openid_user). Anyone
else's: we find that server (fed_https.py: the spec's discovery, public addresses only, verified
TLS, deadlines and size caps) and ask it, GET /_matrix/federation/v1/openid/userinfo, never
following a redirect. The answer counts only if its `sub` is a user ID ON THAT SERVER: a server
can vouch for its own people, never for ours or a third server's. Any doubt: not verified (the
one-time console link still works).

Each person's panel (by client address) and each server name may try only so often, and only a
few such checks run at once (each holds a thread for up to DEADLINE_S). The token is never logged.
"""
import json
import logging
import threading
import time
import urllib.parse

from server import fed_https
from common import identity

log = logging.getLogger("console")
DEADLINE_S = 10
USERINFO_MAX = 8192
TOKEN_MAX = 512
WINDOW_S = 600
PER_CLIENT, PER_SERVER = 10, 30               # federation checks per WINDOW_S
PER_CLIENT_ANY = 60                           # any sign-in check (ours too: each holds a thread), per WINDOW_S
AT_ONCE = 4
MAX_TRACKED = 5000
USERINFO = "/_matrix/federation/v1/openid/userinfo?access_token="
_tries: dict[str, list[float]] = {}
_tries_lock = threading.Lock()
_running = threading.BoundedSemaphore(AT_ONCE)


def may_try(key: str, limit: int, now: float | None = None) -> bool:
    """One more try for `key` within the window, counted if allowed (pruned so it can't grow without end)."""
    now = time.monotonic() if now is None else now
    with _tries_lock:
        if len(_tries) > MAX_TRACKED:
            for k in [k for k, ts in _tries.items() if not ts or now - ts[-1] > WINDOW_S]:
                del _tries[k]
            if len(_tries) > MAX_TRACKED:
                return False                  # flooded with fresh keys: wait for the window to pass
        recent = [t for t in _tries.get(key, []) if now - t < WINDOW_S]
        if len(recent) >= limit:
            _tries[key] = recent
            return False
        _tries[key] = [*recent, now]
        return True


def vouched_for(sub: object, server_name: str) -> str | None:
    """`sub` if it is a well-formed user ID whose server is exactly `server_name`."""
    if not isinstance(sub, str) or not identity.USER_RE.fullmatch(sub) or not all(33 <= ord(c) <= 126 for c in sub):
        return None
    return sub if identity.split(sub)[1] == server_name else None


def userinfo(access_token: str, server_name: str) -> str | None:
    """Ask `server_name` whose token this is (see the module's docstring); None on any doubt."""
    deadline = time.monotonic() + DEADLINE_S
    try:
        host, port, host_header, tls_name = fed_https.cached_resolve(server_name, deadline)
        status, _, body = fed_https.get(host, port, USERINFO + urllib.parse.quote(access_token, safe=""),
                                        host_header, tls_name, deadline, USERINFO_MAX)
    except fed_https.Refused as e:
        log.info("panel sign-in: %s didn't confirm a token (%s)", server_name, type(e.__cause__ or e).__name__)
        return None
    if status != 200:                         # a redirect included: never followed with the token
        log.info("panel sign-in: %s answered %s", server_name, status)
        return None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):   # deeply nested JSON fits in the cap (GPT review)
        return None
    found = vouched_for(data.get("sub") if isinstance(data, dict) else None, server_name)
    if found is None:
        log.warning("panel sign-in: %s vouched for a user that isn't its own; refused", server_name)
    return found


def user_of(access_token: object, server_name: object, client: str, ours: str, local_check) -> str | None:
    """The Matrix user an OpenID token from the panel belongs to, or None. `ours`: this server's name
    (its tokens go to `local_check`, our homeserver); `client`: who is asking, for the limits."""
    if not isinstance(access_token, str) or not access_token or len(access_token) > TOKEN_MAX \
            or not access_token.isascii() or not access_token.isprintable():
        return None
    if not may_try("any:" + client, PER_CLIENT_ANY):     # GPT review: our own branch was unlimited
        log.info("panel sign-in: too many checks from %s; refused for now", client)
        return None
    if server_name is None or server_name == ours:   # Element sends it; an older one may not
        return local_check(access_token)
    if fed_https.parse_server_name(server_name) is None:
        return None
    if not _running.acquire(blocking=False):
        return None                           # several checks already underway: try again in a moment
    try:
        if not may_try("client:" + client, PER_CLIENT) or not may_try("server:" + server_name.lower(), PER_SERVER):
            log.info("panel sign-in: too many checks from %s or for %s; refused for now", client, server_name)
            return None
        return userinfo(access_token, server_name)
    finally:
        _running.release()
