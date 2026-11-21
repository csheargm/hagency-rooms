"""Matrix calls whose expected "no" isn't an error.

nio checks each answer against the success schema and logs "Error validating response: …" at
WARNING for every one that doesn't match, expected or not: the Lobby's alias before the Lobby exists
("'room_id' is a required property"), or a deactivated account's presence ("'presence' is a
required property"). These two calls go through AsyncClient.send instead, so the status is read
here: the expected answers are quiet, and anything else raises RuntimeError for the caller to log.
"""
import urllib.parse

from nio import AsyncClient

TIMEOUT_S = 30


async def get_json(client: AsyncClient, path: str) -> tuple[int, dict]:
    """(HTTP status, JSON object or {}) for one GET as @pm."""
    resp = await client.send("GET", path, headers={"Authorization": f"Bearer {client.access_token}"}, timeout=TIMEOUT_S)
    try:
        try:
            body = await resp.json(content_type=None)
        except ValueError:                    # not JSON (a proxy's error page): the status still says enough
            body = {}
        return resp.status, body if isinstance(body, dict) else {}
    finally:
        resp.release()


async def room_of_alias(client: AsyncClient, alias: str) -> str | None:
    """The room `alias` points to; None if no room has it (404 M_NOT_FOUND: never a proxy's 404 page,
    which would make @pm create a second room). Anything else raises RuntimeError."""
    status, body = await get_json(client, f"/_matrix/client/v3/directory/room/{urllib.parse.quote(alias)}")
    if status == 404 and body.get("errcode") == "M_NOT_FOUND":
        return None
    if status == 200 and isinstance(body.get("room_id"), str):
        return body["room_id"]
    raise RuntimeError(f"couldn't look up {alias}: HTTP {status} {body.get('errcode', '')}".strip())


async def presence(client: AsyncClient, user_id: str) -> bool | None:
    """Whether `user_id` is online now; None when the server can't say: no such account, a
    deactivated one, or presence it won't share (403/404, or an answer without "presence"). Anything
    else raises RuntimeError."""
    status, body = await get_json(client, f"/_matrix/client/v3/presence/{urllib.parse.quote(user_id)}/status")
    if status in (403, 404) or (status == 200 and not isinstance(body.get("presence"), str)):
        return None
    if status != 200:
        raise RuntimeError(f"couldn't ask for {user_id}'s presence: HTTP {status} {body.get('errcode', '')}".strip())
    return body["presence"] == "online" or body.get("currently_active") is True


async def in_directory(client: AsyncClient, room_id: str) -> bool:
    """Whether the room is published in this server's room directory. Raises RuntimeError when unknown."""
    status, body = await get_json(client, f"/_matrix/client/v3/directory/list/room/{urllib.parse.quote(room_id)}")
    if status == 200 and body.get("visibility") in ("public", "private"):
        return body["visibility"] == "public"
    raise RuntimeError(f"couldn't read {room_id}'s directory listing: HTTP {status} {body.get('errcode', '')}".strip())
