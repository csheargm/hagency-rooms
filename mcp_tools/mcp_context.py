"""hagency MCP tools for knowing the project (any time, not only during a task): the live status
@pm publishes, the room's recent conversation, and the project's files on the default branch.
Read-only; files are fetched as this agent's own GitHub identity. See room_context.py."""
import base64
import binascii
import re
import time
import urllib.parse

from common import github
from mcp_tools import mcp_core as core
from adapters.agents import room_context
from mcp_tools.mcp_core import AGENT, mcp

MAX_FILE_CHARS = 60_000


SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def endpoint(repo: str, path: str) -> str | None:
    """The contents endpoint for a repo path, or None if the path isn't plainly inside the repo.
    Checked on the raw input (before any normalising), and every segment must be a plain name."""
    if path in ("", "/"):
        return f"repos/{repo}/contents"
    parts = path.strip("/").split("/")
    if path.startswith("/") or any(p in ("", ".", "..") or not SEGMENT_RE.match(p) for p in parts):
        return None
    return f"repos/{repo}/contents/" + "/".join(urllib.parse.quote(p, safe="") for p in parts)


RECENT_S = 15 * 60


def owners_room() -> str | None:
    """The room where the owner just @mentioned this agent (last 15 min). If they did so in more than
    one room, refuse to guess: a turn in one room must never borrow another room's
    conversation (the MCP call itself doesn't say which room it came from)."""
    rooms = []
    for ev in owners_asks():
        if ev.get("room_id") not in rooms:
            rooms.append(ev.get("room_id"))
    return rooms[0] if len(rooms) == 1 else None


def owners_asks() -> list[dict]:
    """The owner's recent messages that @mention this agent, newest first."""
    owner, now_ms, asks = core.owner_id(), time.time() * 1000, []
    for ev in core.recent_events():
        content = ev.get("content", {})
        mentions = content.get("m.mentions") if isinstance(content.get("m.mentions"), dict) else {}
        ids = mentions.get("user_ids") if isinstance(mentions.get("user_ids"), list) else []
        if ev.get("sender") == owner and core.USER_ID in ids and now_ms - ev.get("origin_server_ts", 0) < RECENT_S * 1000:
            asks.append(ev)
    return asks


@mcp.tool()
def project_status() -> dict:
    """The live status (what's done, in progress, next, stopped) and recent conversation of the room
    your owner last spoke to you in. Check this before answering questions about the project: it is
    more current than your memory."""
    room = owners_room()
    if room is None:
        return {"error": "no recent message from your owner to you in a project room"}
    snap = room_context.snapshot(core.token(), room)
    return snap if snap["status"] else {"error": "that room has no project status"}


@mcp.tool()
async def read_project_file(path: str = "") -> dict:
    """Read a file (or list a folder, path "" for the top) on the project's default branch."""
    room = owners_room()
    repo = room and room_context.room_repo(core.token(), room)
    if not repo:
        return {"error": "no recent message from your owner to you in a room with a repo"}
    url = endpoint(repo, path)
    if url is None:
        return {"error": "use a plain path inside the repo, e.g. README.md or docs/guide.md"}
    try:
        data = await github.run_json(["api", url], AGENT)
    except github.GitHubError as e:
        return {"error": f"not found or not readable: {str(e)[:120]}"}
    if isinstance(data, list):
        return {"folder": path or "/", "entries": [f"{e.get('name')}{'/' if e.get('type') == 'dir' else ''}"
                                                   for e in data]}
    try:
        text = base64.b64decode(str(data.get("content", ""))).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return {"error": "couldn't decode that file"}
    return {"path": path, "content": text[:MAX_FILE_CHARS], "truncated": len(text) > MAX_FILE_CHARS}
