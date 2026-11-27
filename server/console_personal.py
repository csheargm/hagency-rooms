"""My Hagency: the Hagency panel at the top of a person's private chat with @pm (Product Manager), with
only their personal cards: My agents (add with the AI picker, rename, remove, stop / start, disconnect, comes along),
Connect GitHub, and the Server agent card while it runs (pm_trial.py). No room cards there, and only that person can use it: the chat is @pm's own with
them (pm_settings.private_room: only @pm invites; nobody else is in it), checked on every view and
action. The same widget URL as in project rooms, so the same pre-approval (deploy/element_home.py).
"""
ACTIONS = ("follow", "add", "code", "update", "selfupdate", "autoupdate", "github_signin", "github", "rename", "remove", "disconnect", "stop", "start",
           "goback", "trial", "trial_model")   # console.act's personal ones (trial: pm_trial.py, ADR 0025)
ROOM_ONLY = "This is your own Hagency panel; a project room's settings are in that room's panel."
NOT_YOURS = "This is someone else's chat with Product Manager."
TITLE = "My Hagency"


def is_personal_chat(loop, room) -> bool:
    """@pm's own private chat with one person (made by private_room, never one they opened), just the two."""
    from server import pm_settings                        # here: pm_settings imports console, which imports this module
    rid = getattr(room, "room_id", None)
    person = next((p for p, r in (getattr(loop, "dm_rooms", None) or {}).items() if r == rid), None) if rid else None
    return (person is not None and pm_settings.is_safe_chat(room)
            and set(room.users) <= {loop.user_id, person})


NOT_PRIVATE = ("This chat isn't private any more, so its Hagency panel does nothing. Your settings are in a new "
               "chat with Product Manager: say \"hi\" to it.")


def is_dm(loop, room) -> bool:
    """One of @pm's own private chats with a person, safe now or not, and also one it replaced since
    (GPT review: a chat that stopped being private is refused, never treated as a project room)."""
    rid = getattr(room, "room_id", None)
    return rid in (getattr(loop, "dm_rooms", None) or {}).values() or rid in had_panel(loop)


def had_panel(loop) -> dict:
    """room → person: every chat My Hagency was put in (on disk: kept when @pm makes a newer chat)."""
    if "_personal_chats" not in loop.__dict__:
        from common.bridge_config import ROOT
        from common.saved_dict import SavedDict
        loop._personal_chats = SavedDict(ROOT / "data" / "personal-chats.json")
    return loop._personal_chats


def owner_of(loop, room) -> str | None:
    """The person whose My Hagency this room is, or None (not a personal chat)."""
    if not is_personal_chat(loop, room):
        return None
    return next(p for p, r in loop.dm_rooms.items() if r == room.room_id)


def refusal(loop, room, user: str, action: str) -> str | None:
    """Why `user` may not do `action` here (a personal chat), or None (go ahead, or not a personal chat)."""
    if not is_dm(loop, room):
        return None
    owner = owner_of(loop, room)
    if owner is None:
        return NOT_PRIVATE
    if owner != user:
        return NOT_YOURS
    return None if action in ACTIONS else ROOM_ONLY


def view(loop, room, user: str, mine: list, can_add: bool, extra: dict) -> dict:
    """The personal cards' state only (console.view gives `mine`, `can_add` and the shared fields)."""
    owner = owner_of(loop, room)
    if owner != user:
        return {"error": NOT_PRIVATE if owner is None else NOT_YOURS}
    return {"personal": True, "room": TITLE, "user": user.split(":")[0].lstrip("@"), "agents": mine,
            "can_add": can_add, "pm_chat": room.room_id, "repo": None, "moderator": False, "waiting": None,
            "html": "", "trial": loop.trial_view(user) if hasattr(loop, "trial_view") else None, **extra}   # pm_trial.py
