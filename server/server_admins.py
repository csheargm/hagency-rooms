"""Is a person on this server one of its admins (e.g. @owner)? Admins run the server; @pm doesn't
welcome them or offer them an agent in Welcome (they can still say "@pm I want an agent").

Asked of Synapse's admin API (as admin-bot, local_signup.admin_call) once per person and
remembered in data/admins.json for a day: each question costs admin-bot a sign-in.
"""
import logging
import time
import urllib.parse

from common import local_signup
from common.bridge_config import ROOT
from common.saved_dict import SavedDict

log = logging.getLogger("loop")
_known: dict = {}
REMEMBER_S = 24 * 3600                        # someone promoted or demoted is seen within a day


def known() -> dict:
    if "file" not in _known:
        _known["file"] = SavedDict(ROOT / "data" / "admins.json")
    return _known["file"]


def is_admin(user_id: str) -> bool:
    """Blocking. False when Synapse can't say (a greeting too many beats a newcomer left unwelcomed)."""
    seen = known().get(user_id)
    if isinstance(seen, dict) and time.time() - seen.get("at", 0) < REMEMBER_S:
        return seen.get("admin") is True
    try:
        info = local_signup.admin_call("GET", f"/_synapse/admin/v2/users/{urllib.parse.quote(user_id)}", None)
    except (SystemExit, OSError, ValueError, AttributeError) as e:   # admin-bot can't sign in, Synapse away: ask again later
        log.warning("couldn't ask whether %s is an admin: %s", user_id, e)
        return False
    if "_status" in info:                     # an error answer: not remembered either
        return False
    known()[user_id] = {"admin": info.get("admin") is True, "at": time.time()}
    return info.get("admin") is True
