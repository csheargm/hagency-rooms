"""Who can find and join a project room: its visibility, set by its owners and maintainers in the
Hagency panel or by telling @pm, and kept in the room's own state (written by @pm only, like its repo):
  - private (the default): unlisted; joinable by invite, or by asking @pm with the exact repo name;
  - listed: in "Find a project" and the server's room directory; join rule "knock", so Element shows
    "Ask to join" (its feature_ask_to_join, on in deploy/element_home.py), and "find a project" has 🙋
    (pm_find.py); either way @pm asks the room's owners (👍 let in · 👎 decline: pm_joining.py);
  - open: listed, and anyone joins directly (join rule "public").
A room bound to a private GitHub repo can't be open, and a listing never shows a private repo's name.
Listed and open rooms are in this server's room directory, which other Matrix servers can read too.
Joining never gives anyone a say in how the room runs: mode, repo and approvals stay with moderators.
"""
import asyncio
import json
import logging
import urllib.parse

from nio import AsyncClient, RoomGetStateResponse

from common import github
from common import room_state
from common.bridge_config import SERVER_NAME
from common.identity import plain  # noqa: F401 — pm_joining, pm_settings, pm_projects import it from here
from common.local_signup import WELCOME_ALIAS

log = logging.getLogger("loop")
LOBBY_LOCALPART = "lobby"
LOBBY_ALIAS = f"#{LOBBY_LOCALPART}:{SERVER_NAME}"
FRONT_DOORS = (WELCOME_ALIAS, LOBBY_ALIAS)    # @pm's own rooms: never a project room
NOT_A_PROJECT = "The Hagency panel works in project rooms only. Open one of your project rooms to use it."
VISIBILITY_EVENT = "xyz.hagencylabs.visibility"
PRIVATE, LISTED, OPEN = "private", "listed", "open"
VISIBILITIES = (PRIVATE, LISTED, OPEN)
JOIN_RULE = {PRIVATE: "invite", LISTED: "knock", OPEN: "public"}
DIRECTORY = {PRIVATE: "private", LISTED: "public", OPEN: "public"}
WORDS = {PRIVATE: "private: people join by invite, or by asking me with the exact repo name",
         LISTED: ("listed: shown in Find a project and the room directory (its name and topic are public); "
                  "people ask to join and an owner lets them in"),
         OPEN: "open: listed, and anyone can join"}
NOT_OPEN = "This room works on a private repo, so it can't be open to anyone; it can be listed (people ask to join)."
# Set when a private repo is removed from the room (pm_unbind.py): Matrix shows each message under the
# history rule it was sent with, so changing the rule then can't hide the past; open stays refused.
PRIVATE_PAST_EVENT = "xyz.hagencylabs.private_past"
NOT_OPEN_PAST = ("This room's earlier messages are about a private repo, so it can't be open to anyone; it can be "
                 "listed (people ask to join).")


async def had_private_repo(client: AsyncClient, room_id: str) -> bool:
    """This room once worked on a private repo (PRIVATE_PAST_EVENT); unreadable: as if it had."""
    r = await client.room_get_state_event(room_id, PRIVATE_PAST_EVENT, "")
    content = getattr(r, "content", None)
    if isinstance(content, dict):
        return content.get("private") is True
    return getattr(r, "status_code", "") != "M_NOT_FOUND"


FRONT_DOOR_IDS: set[str] = set()              # what FRONT_DOORS point to, looked up at each start (pm_welcome, pm_lobby)


def is_front_door(room) -> bool:
    """The Welcome room or the Lobby: no project settings, no Hagency panel. By the room the aliases
    point to, and by its own main address (GPT review: a main address alone can be changed)."""
    return getattr(room, "room_id", None) in FRONT_DOOR_IDS or getattr(room, "canonical_alias", None) in FRONT_DOORS


async def join_rule(client: AsyncClient, room_id: str) -> str:
    """The room's join rule now, asked of the server; "public" when unreadable (fail closed)."""
    r = await client.room_get_state_event(room_id, "m.room.join_rules", "")
    content = getattr(r, "content", None)
    rule = content.get("join_rule") if isinstance(content, dict) else None
    return rule if isinstance(rule, str) else "public"


def visibility_in(events: list, pm: str) -> str:
    """The room's visibility, only if @pm wrote it (anyone else's is ignored): private by default."""
    for ev in events:
        if ev.get("type") == VISIBILITY_EVENT and ev.get("state_key") == "" and ev.get("sender") == pm:
            value = (ev.get("content") or {}).get("visibility")
            return value if value in VISIBILITIES else PRIVATE
    return PRIVATE


async def visibility(client: AsyncClient, room_id: str, strict: bool = False) -> str:
    """Private when unreadable; strict: raises room_state.StateUnreadable instead (never a guess)."""
    r = await client.room_get_state(room_id)
    if isinstance(r, RoomGetStateResponse):
        return visibility_in(r.events, room_state.pm_id(client))
    if strict:
        raise room_state.StateUnreadable(f"couldn't read {room_id}'s state: {getattr(r, 'message', r)}")
    return PRIVATE


async def repo_is_private(repo: str | None) -> bool:
    """True unless GitHub says the repo is public (an unreadable repo counts as private)."""
    if repo is None:
        return False
    try:
        info = await github.run_json(["api", f"repos/{repo}"])
    except github.GitHubError as e:
        log.warning("can't tell whether %s is public: %s", repo, str(e)[:160])
        return True
    return not (isinstance(info, dict) and info.get("private") is False)


def refusal(value: str, repo_private: bool) -> str | None:
    """Why `value` isn't allowed for a room (None: it is)."""
    if value not in VISIBILITIES:
        return "Say private, listed or open."
    if value == OPEN and repo_private:
        return NOT_OPEN
    return None


async def publish(client: AsyncClient, room_id: str, public: bool) -> bool:
    """List or unlist the room in this server's room directory (nio has no call for it)."""
    path = f"/_matrix/client/v3/directory/list/room/{urllib.parse.quote(room_id)}"
    resp = await client.send("PUT", path, json.dumps({"visibility": "public" if public else "private"}),
                             headers={"Authorization": f"Bearer {client.access_token}", "Content-Type": "application/json"})
    status = getattr(resp, "status", 0)
    if hasattr(resp, "release"):
        resp.release()                        # the body isn't needed: free the connection
    return status == 200


RANK = {PRIVATE: 0, LISTED: 1, OPEN: 2}


async def apply(client: AsyncClient, room_id: str, value: str, current: str = OPEN) -> str | None:
    """Change who can find and join the room. Closing: the join rule first, so a later failure leaves
    it closed; opening: the join rule last, so a failure leaves it as closed as before (GPT review).
    The record comes after the directory either way. Returns an error line, or None when done."""
    async def rule() -> str | None:
        r = await client.room_put_state(room_id, "m.room.join_rules", {"join_rule": JOIN_RULE[value]})
        return None if hasattr(r, "event_id") else \
            f"⚠️ I couldn't change who can join ({getattr(r, 'status_code', r)}); am I a moderator here?"

    async def directory() -> str | None:
        ok = await publish(client, room_id, DIRECTORY[value] == "public")
        return None if ok else "⚠️ I couldn't update the room directory; please try again."

    closing = RANK[value] <= RANK.get(current, RANK[OPEN])
    for step in ((rule, directory) if closing else (directory, rule)):
        try:
            failed = await step()
        except (OSError, asyncio.TimeoutError) as e:   # the network itself: the same as a refusal
            failed = f"⚠️ I couldn't reach the server ({type(e).__name__}); please try again."
        if failed and not closing and step is rule:   # opening stopped halfway: take the listing back down
            try:
                undone = await publish(client, room_id, DIRECTORY.get(current) == "public")
            except (OSError, asyncio.TimeoutError):
                undone = False
            if not undone:
                failed += " It may still show in the room directory: set it to private again to remove it."
        if failed:
            return failed
    saved = await client.room_put_state(room_id, VISIBILITY_EVENT, {"visibility": value})   # last: what is in force
    return None if hasattr(saved, "event_id") else f"⚠️ I couldn't save the setting: {getattr(saved, 'status_code', saved)}"


async def history_safe(client: AsyncClient, room_id: str) -> str | None:
    """A private repo's room never lets non-members read its history (world_readable → shared)."""
    r = await client.room_get_state_event(room_id, "m.room.history_visibility", "")
    content = getattr(r, "content", None)
    if isinstance(content, dict) and content.get("history_visibility") not in (None, "world_readable"):
        return None
    done = await client.room_put_state(room_id, "m.room.history_visibility", {"history_visibility": "shared"})
    return None if hasattr(done, "event_id") else "⚠️ I couldn't keep this room's history to its members."

