"""Removing an agent for good, and forgetting agents whose accounts are gone (server side, blocking I/O).

remove(): a person's own remote agent (connect.py) has its Matrix account deactivated through the
admin API @pm already uses for Disconnect (local_signup.admin_call): signed out everywhere, out of
every room, never usable again. Then its password leaves .env and its agents.json entry goes, so its
slot is free. Deactivated account names can't be registered again, so a removed further agent's
label is kept ("removed") and a new agent needs a new name.

deactivated(): Synapse's own list of deactivated accounts; registry_removal.prune() forgets only people whose
every account is on it. If the list can't be read in full, nothing is pruned.
"""
import logging
import urllib.parse

from common import identity
from common import local_signup
from common import registry
from server import registry_removal
from common import setup_accounts
from common.bridge_config import SERVER_NAME
from server.remote_agent import WRITING, _signs_in
from common.setup_accounts import AccountError, load_env

log = logging.getLogger("loop")
PAGE = 500
MAX_PAGES = 40                       # 20 000 accounts: far beyond this server; a runaway list is "unreadable"


class NotForgotten(OSError):
    """The account IS closed, but agents.json couldn't be saved: it may still be listed (GPT review,
    batch 5+6). The caller still revokes its codes; removing it again frees the slot."""


def remove(person: str, agent: str) -> bool:
    """Remove this remote agent of `person` (localpart or label). Their first agent goes only when it
    is their last one. True if its account is closed (now, or already: a retry after NotForgotten),
    False when it never existed, so only its entry went. Raises AccountError with a sentence for them."""
    with WRITING:
        person = registry.person_id(person)
        found = registry.find(person, agent)
        if found is None or found["agent"] != registry.REMOTE or not registry.owns(person, found["localpart"]):
            raise AccountError(f"{agent} isn't one of your agents on your own computer")
        others = [a["label"] for a in registry.agents_of(person) if not a["first"]]
        if found["first"] and others:
            raise AccountError(f"remove your other agents first ({', '.join(others)}); this one goes last")
        localpart, env = found["localpart"], load_env()
        if localpart != identity.agent_localpart(person, SERVER_NAME, None if found["first"] else found["label"]):
            raise AccountError(f"{localpart} has an odd record here; ask this server's operator")   # GPT: never a stale name
        key = setup_accounts.password_key(localpart)
        ours = found["ready"] or (key in env and _signs_in(localpart, env[key]))   # never close an account we don't hold
        state = "ours" if ours else _account(f"@{localpart}:{SERVER_NAME}") if key in env else MISSING
        if state == LIVE:                     # a password we can't confirm, on an account that's there
            raise AccountError(f"I couldn't confirm {localpart}'s account just now; please try again")
        if ours:
            _deactivate(f"@{localpart}:{SERVER_NAME}")
        closed = state != MISSING             # closed now, or before (its record couldn't be saved then)
        try:
            registry_removal.forget_agent(person, localpart)
        except OSError as e:
            if closed:                        # closed already: say so, never "try again" as if nothing happened
                raise NotForgotten(f"closed {localpart}, but couldn't save agents.json: {e}") from e
            raise                             # nothing was closed: a plain failure, nothing changed
        if key in env:
            try:
                setup_accounts.save_env({k: v for k, v in load_env().items() if k != key})   # a fresh read: others stay
            except OSError as e:              # the account is closed already: a leftover password is harmless
                log.error("removed %s, but couldn't drop its password from .env: %s", localpart, e)
        return closed


def prune(dead: set[str]) -> list[str]:
    """registry_removal.prune under the same lock as set-ups and removals (GPT: no race with remove())."""
    with WRITING:
        return registry_removal.prune(dead)


MISSING, DEACTIVATED, LIVE = "missing", "deactivated", "live"


def _account(user_id: str) -> str:
    """MISSING only when Synapse says no such account (a set-up whose registration failed);
    DEACTIVATED when it says so (an older record closed before its entry could be saved: GPT
    re-check, batch 5+6); LIVE when it is there or the answer is unclear: nothing on a guess."""
    try:
        r = local_signup.admin_call("GET", f"/_synapse/admin/v2/users/{urllib.parse.quote(user_id)}", None)
    except (SystemExit, OSError, ValueError) as e:   # unclear: treated as live, so nothing is dropped
        log.warning("couldn't ask whether %s exists: %s", user_id, str(e)[:200])
        return LIVE
    if r.get("_status") == 404 and r.get("errcode") == "M_NOT_FOUND":
        return MISSING
    return DEACTIVATED if "_status" not in r and r.get("deactivated") is True else LIVE


def _deactivate(user_id: str) -> None:
    try:
        r = local_signup.admin_call("POST", f"/_synapse/admin/v1/deactivate/{urllib.parse.quote(user_id)}",
                                    {"erase": False})
    except (SystemExit, OSError, ValueError) as e:   # admin-bot can't sign in, Synapse away: as a refusal
        r = {"_status": 0, "errcode": str(e)[:80]}
    if "_status" in r:
        raise AccountError(f"couldn't close {user_id}'s account: {r.get('errcode')}")


def deactivated() -> set[str] | None:
    """Every deactivated account's full ID on this server, or None if Synapse's list can't be read
    in full (an error, a refusal or an odd answer: then nobody may be pruned)."""
    dead, start = set(), "0"
    for _ in range(MAX_PAGES):
        path = f"/_synapse/admin/v2/users?from={urllib.parse.quote(start)}&limit={PAGE}&guests=false&deactivated=true"
        try:
            r = local_signup.admin_call("GET", path, None)
        except (SystemExit, OSError, ValueError) as e:   # admin-bot can't sign in, Synapse away: nothing known
            log.warning("couldn't list deactivated accounts: %s", str(e)[:200])
            return None
        users = r.get("users")
        if "_status" in r or not isinstance(users, list):
            log.warning("couldn't list deactivated accounts: %s", r.get("errcode") or "an odd answer")
            return None
        dead |= {u["name"] for u in users if isinstance(u, dict) and u.get("deactivated") is True
                 and isinstance(u.get("name"), str)}
        start = r.get("next_token")
        if start is None:
            return dead
        start = str(start)
    return None
