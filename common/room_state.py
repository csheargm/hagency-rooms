"""A room's settings live in its own Matrix state, not in config files: its repo and loop level
(github.LEVEL_EVENT). @pm writes them after its own checks (pm_settings.py), and they count only
if @pm wrote them (binding_in).
"""
from nio import AsyncClient, MatrixRoom, RoomGetStateResponse

from common import github

MODERATOR_LEVEL = 50


def pm_id(client: AsyncClient) -> str:
    """@pm on this client's server (the agent adapter's agents read the binding too, as themselves)."""
    return f"@{github.PM_BOT}:{client.user_id.split(':', 1)[1]}"


def binding_in(events: list, pm: str) -> dict:
    """The room's LEVEL_EVENT content, only if @pm wrote it: @pm writes it after its own checks
    (moderator, allowed repo, the requester's agent can write there). A room's creator controls its
    power levels, so the server's own check alone would let anyone they choose write it by hand."""
    for ev in events:
        if ev.get("type") == github.LEVEL_EVENT and ev.get("state_key") == "" and ev.get("sender") == pm:
            return ev.get("content") if isinstance(ev.get("content"), dict) else {}
    return {}


class StateUnreadable(RuntimeError):
    """The server didn't give a room's state: a check that must not fail open says so instead."""


async def read_binding(client: AsyncClient, room_id: str, strict: bool = False) -> dict:
    r = await client.room_get_state(room_id)
    if not isinstance(r, RoomGetStateResponse):
        if strict:
            raise StateUnreadable(f"couldn't read {room_id}'s state: {r}")
        return {}
    return binding_in(r.events, pm_id(client))


def repo_in(content: dict) -> str | None:
    """The room's repo from its LEVEL_EVENT. Stored as a list (one repo per room today; several
    later), with the older single "repo" field still understood."""
    if not isinstance(content, dict):
        return None
    if "repos" in content:               # the list wins; the single "repo" is only for older rooms
        repos = content["repos"]
        repo = repos[0] if isinstance(repos, list) and len(repos) == 1 else None
    else:
        repo = content.get("repo")
    return github.bound_repo(repo)       # only an owner/name, and only listed ones once an operator lists them


def is_discussion(content: dict) -> bool:
    """This room only discusses its repo, which another room runs (one project room per repo: a
    separate room may talk about the repo but never runs its build loop). Only a flag: which room runs
    it is kept by @pm (pm_projects.discussions), never in this room's state, which its members read."""
    return isinstance(content, dict) and content.get("discussion") is True


def level_of(content: dict) -> str:
    """The loop level in a LEVEL_EVENT's content; a room without a repo, or one that only discusses
    a repo another room runs, is off."""
    lvl = content.get("level") if repo_in(content) and not is_discussion(content) else None
    return lvl if lvl in github.LEVELS else "off"


def content(level: str, repo: str, discussion: bool = False) -> dict:
    return {"level": level, "repos": [repo], "repo": repo, **({"discussion": True} if discussion else {})}


def unbound() -> dict:
    """A room whose repo was removed (pm_unbind.py): no repo, so its mode is off (level_of)."""
    return {"level": "off", "repos": []}


async def repo_of(client: AsyncClient, room_id: str) -> str | None:
    """The repo this room works on (one room, one repo), or None if nobody has set one."""
    return repo_in(await read_binding(client, room_id))


async def binding_of(client: AsyncClient, room_id: str, strict: bool = False) -> tuple[str | None, bool]:
    """(repo, whether this room only discusses it), from ONE read of the binding; strict: an
    unreadable room raises StateUnreadable instead of looking unbound."""
    content = await read_binding(client, room_id, strict)
    return repo_in(content), is_discussion(content)


def creators_in(events: list) -> set:
    """Room v12+: the creators, who outrank everyone and aren't in power_levels.users."""
    for ev in events:
        if ev.get("type") == "m.room.create":
            content = ev.get("content", {})
            version = str(content.get("room_version", "1"))
            if version.isdigit() and int(version) >= 12:
                return {ev.get("sender"), *content.get("additional_creators", [])}
    return set()


async def standing(client: AsyncClient, room_id: str, user: str) -> dict:
    """A user's membership (and its event id) and moderator role in a room NOW, from one read of the
    server's state, never a cached view (GPT review). Raises StateUnreadable when it can't be read."""
    r = await client.room_get_state(room_id)
    if not isinstance(r, RoomGetStateResponse):
        raise StateUnreadable(f"couldn't read {room_id}'s state: {r}")
    found = {"membership": None, "event_id": None, "moderator": user in creators_in(r.events)}
    for ev in r.events:
        if ev.get("type") == "m.room.member" and ev.get("state_key") == user:
            found.update(membership=(ev.get("content") or {}).get("membership"), event_id=ev.get("event_id"))
        elif ev.get("type") == "m.room.power_levels" and ev.get("state_key") == "":
            levels = ev.get("content") or {}
            users = levels.get("users") if isinstance(levels.get("users"), dict) else {}
            level = users.get(user, levels.get("users_default", 0))
            found["moderator"] = found["moderator"] or (isinstance(level, int) and level >= MODERATOR_LEVEL)
    return found


async def joined_moderator(client: AsyncClient, room_id: str, user: str) -> bool:
    """Joined AND a moderator, read now (standing): a departed person's power-level entry, or a
    creator who left, doesn't count. Raises StateUnreadable."""
    now = await standing(client, room_id, user)
    return now["membership"] == "join" and now["moderator"]


async def discussion_of(client: AsyncClient, room_id: str) -> bool:
    """This room only discusses its repo (another room runs it)."""
    return is_discussion(await read_binding(client, room_id))


async def bound(client: AsyncClient, room_id: str) -> bool:
    """A moderator has given this room a repo. Only then does @pm show issues, status and the panel
    there: a room anyone can create must not become a window into a repo."""
    return await repo_of(client, room_id) is not None


async def level(client: AsyncClient, room_id: str) -> str:
    return level_of(await read_binding(client, room_id))


async def moderators(client: AsyncClient, room: MatrixRoom) -> list[str]:
    """The room's owners and maintainers who are people (joined, never me, an agent or a bot)."""
    people = [u for u in room.users if u not in room.invited_users and u != client.user_id
              and not u.split(":")[0].endswith(("-agent", "-bot"))]
    return [u for u in sorted(people) if await is_moderator(client, room, u)]


async def is_moderator(client: AsyncClient, room: MatrixRoom, sender: str) -> bool:
    if room.power_levels.get_user_level(sender) >= MODERATOR_LEVEL:
        return True
    # Room v12+: creators outrank everyone but are NOT listed in power_levels.users, and
    # matrix-nio doesn't model that, so read the create event ourselves.
    state = await client.room_get_state(room.room_id)
    if not isinstance(state, RoomGetStateResponse):
        return False
    for ev in state.events:
        if ev.get("type") == "m.room.create":
            content = ev.get("content", {})
            version = str(content.get("room_version", "1"))
            creators = {ev.get("sender"), *content.get("additional_creators", [])}
            return version.isdigit() and int(version) >= 12 and sender in creators
    return False
