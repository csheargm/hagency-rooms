"""Change your agent's own code, for agents that use the hagency MCP server (Hermes). The Codex /
Claude Code bridge does the same in bridge_custom.py; both share customize.py and the same buttons.

The agent edits its owner's private copy with read_my_code / write_my_code (contained to that copy,
like the build tools), then finish_code_change posts the offer in the owner's private room with the
👍 / 📤 / 🗑️ buttons. The clicks are handled by the agent's button helper (bridge.py --buttons-only,
always the official code): only the owner's, only there, each once. What a Hermes owner can change is
Hagency's part (these tools, the room rules), not Hermes itself.
"""
from pathlib import Path

from adapters.agents import bridge_custom
from common import customize
from mcp_tools import mcp_core as core
from common import registry
from mcp_tools.mcp_core import mcp
from common.saved_dict import SavedDict

MAX_READ = 200_000
MAX_WRITE = 400_000
_change: dict = {}                       # the change being worked on: {"request": ...}


def owner() -> str:
    person = registry.owner_of(core.AGENT)
    if not person or not registry.local_name(person):     # custom code runs here: only for people here
        raise customize.CustomError(f"{core.AGENT} isn't the registered agent of anyone on this server")
    return registry.local_name(person)


def private_room(person: str) -> str | None:
    """The room where it's just the owner and me, invitation only."""
    wanted = {registry.person_id(person), core.USER_ID}
    for room in core.rooms():
        joined = core.api("GET", core.room_path(room, "/joined_members"), token=core.token()).get("joined", {})
        if set(joined) == wanted:
            rules = core.api("GET", core.room_path(room, "/state/m.room.join_rules/"), token=core.token())
            if rules.get("join_rule") == "invite":
                return room
    return None


def code_path(path: str) -> Path:
    """A file of the owner's private copy; nothing outside it, nothing git or Python acts on by itself."""
    _check_name(Path(path), path)
    work = customize.work_dir(owner()).resolve()
    full = (work / path).resolve()
    if not full.is_relative_to(work):
        raise ValueError(f"path {path!r} leaves the code")
    _check_name(full.relative_to(work), path)     # again where a symlink in the copy really leads
    return full


def _check_name(rel: Path, path: str) -> None:
    """Lower-cased: this Mac's disk ignores case, so .GIT is .git (GPT review 20)."""
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0].lower() in (".git", "custom"):
        raise ValueError(f"path {path!r} must be relative to the code, without '..', .git or custom/")
    if rel.name.lower() == ".gitattributes" or rel.suffix.lower() == ".pth":
        raise ValueError(f"{rel.name} files aren't accepted")


@mcp.tool()
async def start_code_change(request: str) -> dict:
    """Your owner asked you to change your own code (Hagency Rooms): open their private copy."""
    if not request.strip():
        return {"error": "empty request", "next": "Ask your owner what they want changed."}
    try:
        person = owner()
        if not private_room(person):
            return {"error": "no private room with your owner", "next": "Tell your owner to open a private room "
                    "(direct message) with you and ask there."}
        work = await customize.prepare(person)
    except customize.CustomError as e:
        return {"error": str(e)}
    _change.clear()
    _change["request"] = request
    files = sorted(str(p.relative_to(work)) for p in work.iterdir() if p.is_file() and not p.name.startswith("."))
    return {"files": files, "instructions": bridge_custom.edit_prompt(request),
            "next": "Read files with read_my_code(path), change them with write_my_code({path: full new content}), "
                    "then call finish_code_change(summary) with 2-4 short lines on what you changed."}


@mcp.tool()
def read_my_code(path: str) -> dict:
    """Read one file of your owner's private copy of your code."""
    try:
        with code_path(path).open(errors="replace") as f:      # never more than MAX_READ into memory
            return {"path": path, "content": f.read(MAX_READ)}
    except (OSError, ValueError, customize.CustomError) as e:
        return {"error": str(e)}


@mcp.tool()
def write_my_code(files: dict[str, str]) -> dict:
    """Create or replace files in your owner's private copy: {relative path: full new content}."""
    if not _change:
        return {"error": "no change started", "next": "Call start_code_change(request) first."}
    try:
        targets = {path: code_path(path) for path in files}        # check every path before writing any
        if sum(len(c.encode()) for c in files.values()) > MAX_WRITE:
            raise ValueError(f"more than {MAX_WRITE} bytes in one call")
        for path, full in targets.items():
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(files[path])
    except (OSError, ValueError, customize.CustomError) as e:
        return {"error": str(e), "next": "Fix the paths and call write_my_code again."}
    return {"written": sorted(files), "next": "Call finish_code_change(summary) when every change is made."}


@mcp.tool()
async def finish_code_change(summary: str) -> dict:
    """Save the change and show it to your owner in your private room, with buttons to choose."""
    if not _change:
        return {"error": "no change started", "next": "Call start_code_change(request) first."}
    try:
        person = owner()
        room = private_room(person)
        if not room:
            return {"error": "no private room with your owner"}
        sha = await customize.commit(person, _change["request"])
        if not sha:
            return {"error": "nothing changed", "next": "Tell your owner in one line that nothing needed changing."}
        files = await customize.changed_files(person)
    except (OSError, customize.CustomError, core.github.GitHubError) as e:
        return {"error": core.room_safe(e)}
    body, keys, record = bridge_custom.offer_message(person, room, sha, _change["request"], summary[:2000], files)
    event_id = send(room, body)
    SavedDict(customize.ROOT / "data" / f"custom-offers-{person}.json")[event_id] = record
    for key in keys:
        core.api("PUT", core.room_path(room, f"/send/m.reaction/c{core.time.time_ns()}"),
                 {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}}, token=core.token())
    _change.clear()
    return {"offered": sha[:7], "next": "Your owner chooses with the buttons on that message; you're done. "
                                        "If you're in another room, say in one line that it's in your private room."}


@mcp.tool()
def go_back_to_official() -> dict:
    """Your owner asked to go back to the official version of your code."""
    try:
        changed = customize.go_back(owner())
    except customize.CustomError as e:
        return {"error": str(e)}
    return {"done": changed, "next": "Tell your owner in one line: " + (
        "going back to the official version, back in a moment." if changed else "already on the official version.")}


def send(room: str, body: str) -> str:
    content = {"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
               "formatted_body": core.markdown.markdown(body)}
    r = core.api("PUT", core.room_path(room, f"/send/m.room.message/c{core.time.time_ns()}"), content, token=core.token())
    if "event_id" not in r:
        raise RuntimeError(f"Matrix refused the message: {r.get('errcode')}")
    return r["event_id"]
