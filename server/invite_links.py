"""One-step invite links for a project room (docs/adr/0014-one-step-invite-link.md): the store.

A link is https://<rooms>/invite#<token>. The token is 32 random bytes; only its SHA-256 is kept
(data/invite-links.json), with the room, who made it, when it expires and what happened to it. Each
link also has a short id, which is not the token, so its maker can cancel it ("cancel invite link
<id>"). A link works once: `take` checks and marks it in one synchronous step (no await), so two
tabs racing can't both use it; `give_back` undoes that when the invite itself failed.
No function here logs or returns a token except `create`, which hands it to its caller once.
"""
import hashlib
import secrets
import time

from common.bridge_config import ROOT
from common.saved_dict import SavedDict
from server import fed_openid

DAY_S = 86400
DEFAULT_DAYS, MAX_DAYS = 7, 30
KEEP_S = 30 * DAY_S                   # used, cancelled and expired links are dropped after this
PER_ROOM_OPEN = 20                    # unused links one room may have at once
PER_PERSON_DAY = 10                   # new links one person may make in a day
LOOKUPS_PER_CLIENT, LOOKUPS_ALL = 30, 600   # check + redeem, per fed_openid.WINDOW_S
TOKEN_MAX = 128
OK, USED, EXPIRED, CANCELLED, UNKNOWN = "ok", "used", "expired", "cancelled", "unknown"
_store: SavedDict | None = None


def store() -> SavedDict:
    """hash → {"id", "room", "by", "made", "expires", "state", "who"}; on disk."""
    global _store
    if _store is None:
        _store = SavedDict(ROOT / "data" / "invite-links.json")
    return _store


def hashed(token: object) -> str | None:
    """The key a token is kept under, or None for anything that can't be one of ours."""
    if not isinstance(token, str) or not token or len(token) > TOKEN_MAX or not token.isascii() \
            or not all(c.isalnum() or c in "-_" for c in token):
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def prune(now: float | None = None) -> None:
    now = time.time() if now is None else now
    links = store()
    for key in [k for k, v in links.items() if now - max(v["expires"], v.get("at", 0)) > KEEP_S]:
        links.pop(key)


def refusal(room: str, by: str, now: float | None = None) -> str | None:
    """Why `by` can't make another link for `room` now (too many), else None."""
    now = time.time() if now is None else now
    links = store().values()
    if sum(1 for v in links if v["room"] == room and status(v, now) == OK) >= PER_ROOM_OPEN:
        return (f"This room already has {PER_ROOM_OPEN} invite links nobody has used yet. Cancel some first "
                "(\"@pm cancel invite links\").")
    if sum(1 for v in links if v["by"] == by and now - v["made"] < DAY_S) >= PER_PERSON_DAY:
        return f"You've made {PER_PERSON_DAY} invite links today. Please try again tomorrow."
    return None


def create(room: str, by: str, days: int = DEFAULT_DAYS, now: float | None = None) -> tuple[str, str]:
    """(token, id) for a new link; the caller checked that `by` may invite to `room`."""
    now = time.time() if now is None else now
    prune(now)
    token, link_id = secrets.token_urlsafe(32), secrets.token_hex(4)
    store()[hashed(token)] = {"id": link_id, "room": room, "by": by, "made": now,
                              "expires": now + max(1, min(days, MAX_DAYS)) * DAY_S, "state": OK}
    return token, link_id


def status(link: dict | None, now: float | None = None) -> str:
    if link is None:
        return UNKNOWN
    if link["state"] != OK:
        return link["state"]
    return EXPIRED if (time.time() if now is None else now) >= link["expires"] else OK


def look_up(token: object, now: float | None = None) -> tuple[str, dict | None]:
    """(status, the link when it is still usable). Never changes anything."""
    key = hashed(token)
    link = store().get(key) if key else None
    state = status(link, now)
    return state, (dict(link) if state == OK else None)


def take(token: object, user: str, now: float | None = None) -> tuple[str, dict | None]:
    """Use the link for `user`: (OK, the link) the first time only, else (why not, None). It counts
    as used from here on; `give_back` reopens it only when the server said no to the invite."""
    now = time.time() if now is None else now
    state, link = look_up(token, now)
    if state != OK:
        return state, None
    store()[hashed(token)] = {**link, "state": USED, "who": user, "at": now, "pending": True}   # no await since look_up
    return OK, link


def settle(token: object) -> None:
    """The invite went through: the link stays used (or cancelled, if that came meanwhile)."""
    key = hashed(token)
    link = store().get(key) if key else None
    if link is not None and link.pop("pending", None):
        store()[key] = link


def give_back(token: object) -> None:
    """The server refused the invite: the link works again, unless it was cancelled meanwhile
    (GPT review: a cancel during the invite must win) or has already settled."""
    key = hashed(token)
    link = store().get(key) if key else None
    if link is not None and link["state"] == USED and link.get("pending"):
        store()[key] = {k: v for k, v in link.items() if k not in ("who", "at", "pending")} | {"state": OK}


def cancellable(link: dict, now: float) -> bool:
    """Unused, or being used right now (its invite may still fail and must not reopen it)."""
    return status(link, now) == OK or (link["state"] == USED and bool(link.get("pending")))


def cancel(link_id: str, may_cancel, now: float | None = None) -> bool:
    """Cancel the unused link with this id if `may_cancel(link)` says so; True if one was cancelled."""
    now = time.time() if now is None else now
    for key, link in list(store().items()):
        if link["id"] == link_id and cancellable(link, now) and may_cancel(link):
            store()[key] = {**link, "state": CANCELLED, "at": now}
            return True
    return False


def cancel_room(room: str, now: float | None = None) -> int:
    """Cancel every unused link to `room`; how many."""
    now = time.time() if now is None else now
    done = 0
    for key, link in list(store().items()):
        if link["room"] == room and cancellable(link, now):
            store()[key] = {**link, "state": CANCELLED, "at": now}
            done += 1
    return done


def may_look(client: str) -> bool:
    """One more lookup for this client address (and for everyone together) in the window."""
    return fed_openid.may_try("invite:" + client, LOOKUPS_PER_CLIENT) and fed_openid.may_try("invite:all", LOOKUPS_ALL)
