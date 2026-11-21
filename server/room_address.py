"""A listed or open project room's readable address, #final-project:<server>. Without one, Element's
room directory and links showed the raw room ID (screen test). It is made from the room's name (which
the directory shows anyway), numbered when taken (#final-project-2), and never holds a private repo's
name. It goes when the room goes back to private. @pm records the address it made in its own state
event (ADDRESS_EVENT, read only when @pm wrote it), so it only ever removes its own; an address the
room's owners set themselves is left alone.
"""
import asyncio
import json
import logging
import re
import urllib.parse
from types import SimpleNamespace

from nio import AsyncClient, RoomGetStateResponse

from common import matrix_calls
from common import room_state
from common.bridge_config import SERVER_NAME

log = logging.getLogger("loop")
ADDRESS_EVENT = "xyz.hagencylabs.address"
SLUG_MAX = 40
TRIES = 10                                    # #name, #name-2 … #name-10
FALLBACK = "project"
MADE, HERE, TAKEN = "made", "here", "taken"
RESERVED = ("lobby", "welcome", "pm")         # @pm's own rooms and name: never a look-alike #lobby-2


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:SLUG_MAX].strip("-")


def localpart(name: str, private_repo: str | None = None) -> str:
    """The address's name part: the room's name, unless it holds the private repo's name."""
    made = slug(name)
    hidden = slug(private_repo.split("/")[-1]) if private_repo else ""
    if not made or (hidden and hidden in made):
        return FALLBACK
    return f"{made}-project" if made in RESERVED else made


async def call(client: AsyncClient, method: str, path: str, body: dict | None = None) -> int:
    resp = await client.send(method, path, json.dumps(body) if body is not None else None,
                             headers={"Authorization": f"Bearer {client.access_token}", "Content-Type": "application/json"})
    status = getattr(resp, "status", 0)
    if hasattr(resp, "release"):
        resp.release()
    return status


def alias_path(alias: str) -> str:
    return f"/_matrix/client/v3/directory/room/{urllib.parse.quote(alias)}"


async def addresses(client: AsyncClient, room_id: str) -> tuple[str | None, dict]:
    """(the address I made, the room's canonical alias content). Raises RuntimeError when unreadable."""
    r = await client.room_get_state(room_id)
    if not isinstance(r, RoomGetStateResponse):
        raise RuntimeError(f"couldn't read the room's state: {getattr(r, 'message', r)}")
    mine, canonical = None, {}
    for ev in r.events:
        content = ev.get("content") if isinstance(ev.get("content"), dict) else {}
        if ev.get("state_key") != "":
            continue
        if ev.get("type") == ADDRESS_EVENT and ev.get("sender") == room_state.pm_id(client):
            mine = content.get("alias") if isinstance(content.get("alias"), str) else None
        elif ev.get("type") == "m.room.canonical_alias":
            canonical = content
    return mine, canonical


async def claim(client: AsyncClient, room_id: str, alias: str) -> str | None:
    """MADE: I made `alias` for this room now; HERE: it already points here (an earlier try whose answer
    was lost, or the owners': GPT review), so it is used but never recorded as mine (never removed by
    me); TAKEN: another room has it; None: the server refused."""
    status = await call(client, "PUT", alias_path(alias), {"room_id": room_id})
    if status == 409:
        return HERE if await matrix_calls.room_of_alias(client, alias) == room_id else TAKEN
    if status != 200:
        log.warning("couldn't make %s for %s: HTTP %s", alias, room_id, status)
        return None
    return MADE


async def give(client: AsyncClient, room_id: str, name: str, private_repo: str | None, canonical: dict) -> str | None:
    """Make the room's address and make it the main one (its other addresses kept); None if none."""
    base = localpart(name, private_repo)
    for n in range(1, TRIES + 1):
        alias = f"#{base if n == 1 else f'{base}-{n}'}:{SERVER_NAME}"
        got = await claim(client, room_id, alias)
        if got is None:
            return None
        if got == TAKEN:                      # the next number
            continue
        recorded = await client.room_put_state(room_id, ADDRESS_EVENT, {"alias": alias}) \
            if got == MADE else SimpleNamespace(event_id=None)   # mine: recorded first, removable later
        shown = await client.room_put_state(room_id, "m.room.canonical_alias", {**canonical, "alias": alias}) \
            if hasattr(recorded, "event_id") else recorded
        if hasattr(shown, "event_id"):
            return alias
        log.warning("couldn't make %s the address of %s: %s", alias, room_id, shown)
        if got == MADE:
            await take(client, room_id, alias)
        return None
    return None


async def take(client: AsyncClient, room_id: str, alias: str) -> bool:
    """Remove the address I made: from the directory, as the main one, and from my record. Only while
    it still points here: a record that outlived its address never removes another room's (GPT review)."""
    if await matrix_calls.room_of_alias(client, alias) == room_id \
            and await call(client, "DELETE", alias_path(alias)) not in (200, 404):   # 404: gone already
        return False
    _, canonical = await addresses(client, room_id)
    if canonical.get("alias") == alias:
        kept = {k: v for k, v in canonical.items() if k != "alias"}   # its other addresses stay
        if not hasattr(await client.room_put_state(room_id, "m.room.canonical_alias", kept), "event_id"):
            return False
    return hasattr(await client.room_put_state(room_id, ADDRESS_EVENT, {}), "event_id")


async def keep_secret(client: AsyncClient, room_id: str, name: str, private_repo: str) -> None:
    """Binding a private repo: my address (made before the bind) never holds its name (GPT review)."""
    mine, _ = await addresses(client, room_id)
    if not mine or localpart(mine[1:].split(":")[0], private_repo) != FALLBACK:
        return
    if not await take(client, room_id, mine):
        raise RuntimeError(f"couldn't remove {mine}")   # the caller doesn't bind the repo (GPT re-check)
    _, canonical = await addresses(client, room_id)
    await give(client, room_id, name, private_repo, canonical)


async def follow(client: AsyncClient, room_id: str, listed: bool, name: str, private_repo: str | None) -> str:
    """After a visibility change: a listed or open room gets an address, a private one loses mine.
    Returns a note for the reply ("" when there is nothing to say)."""
    try:
        mine, canonical = await addresses(client, room_id)
        if not listed:
            if mine and not await take(client, room_id, mine):
                return f" I couldn't remove its address {mine}: set it to private again to retry."
            return ""
        if mine or canonical.get("alias"):    # mine already, or one its owners chose
            return ""
        made = await give(client, room_id, name, private_repo, canonical)
    except (RuntimeError, OSError, asyncio.TimeoutError) as e:   # the setting itself is done; said in the reply
        log.warning("address of %s not updated: %s", room_id, e)
        made = None
        if not listed:
            return " I couldn't remove its address just now: set it to private again to retry."
    return f" Its address is {made}." if made else " I couldn't give it a readable address, so it shows by its ID."
