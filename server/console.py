"""The Hagency console: room settings in the side panel (Element widget) or as a standalone page
(phones, other clients) — instead of config files.

Who is looking:
  - in Element: the widget asks Element for an OpenID token (Matrix widget API, one-time "allow"),
    and we verify it with the server that issued it, ours or another over federation (fed_openid.py);
  - anywhere else: "@pm send me my console link" → @pm DMs a one-time link (10 min, single use).
Either way the page gets a short-lived session bound to that Matrix user.

What they can do goes through @pm's own code paths and checks (mode: owner/maintainer; my agent's
follow-me: only my own agent; approve: only what I asked for), and each change is
also announced in the room. Nothing here bypasses the chat's rules.
"""
import asyncio
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request

from aiohttp import web

from common import agent_kinds
from server import console_personal
from server import console_repo
from common import customize
from server import fed_openid
from common import github
from server import github_link
from server import ownership
from server import project_rooms
from common import registry
from common import room_state
from common.bridge_config import ELEMENT_ORIGIN
from server.console_agents import add_agent, can_add, first_pick, fresh_code, my_agents   # My agents: kept apart (file size)
from common.setup_accounts import HS, SERVER_NAME
from server import pm_paused

SESSION_S, MAX_SESSIONS = 12 * 3600, 1000
LINK_S = 600
_sessions: dict[str, tuple[str, float, str | None]] = {}   # session → (user, expires, room scope or None)
_links: dict[str, tuple[str, float, str]] = {}             # one-time code → (user, expires, room)
USER_RE = re.compile(r"^@[a-z0-9._=/+-]+:" + re.escape(SERVER_NAME) + "$")
LOCAL_HOSTS = ("localhost", "127.0.0.1")
NOT_VERIFIED = "couldn't verify you with your server; say \"@pm send me my console link\" in the room to sign in"
PORT = int(os.environ.get("PANEL_PORT", "8650"))
# Also safe inside sh: it goes into the install line and connect.sh (GPT review 21).
URL_RE = re.compile(r"^(https://[A-Za-z0-9.-]+|http://(localhost|127\.0\.0\.1))(:\d{1,5})?$")


def public_url() -> str:
    """Where people reach this console: HAGENCY_PUBLIC_CONSOLE (https on the public server, behind its
    proxy), else this machine. Links, the widget, the install line and the allowed Host all use it."""
    url = os.environ.get("HAGENCY_PUBLIC_CONSOLE", f"http://localhost:{PORT}").rstrip("/")
    if not URL_RE.fullmatch(url):
        raise RuntimeError("this server's HAGENCY_PUBLIC_CONSOLE must be https://host[:port] (http only for localhost)")
    return url


def allowed_hosts() -> tuple[str, ...]:
    """Hosts a request may be addressed to (guards against DNS rebinding): this machine and the public name."""
    return (*LOCAL_HOSTS, urllib.parse.urlsplit(public_url()).hostname)


def allowed_origins() -> set[str]:
    """Exact origins (scheme, host, port) our own page is served from."""
    return {public_url(), *(f"http://{h}:{PORT}" for h in LOCAL_HOSTS)}


def new_session(user_id: str, room: str | None = None) -> str:
    if len(_sessions) > MAX_SESSIONS:         # GPT review: expired ones were kept for good
        for gone in [t for t, (_, until, _) in _sessions.items() if until < time.time()]:
            del _sessions[gone]
    token = secrets.token_urlsafe(32)
    _sessions[token] = (user_id, time.time() + SESSION_S, room)
    return token


def session_of(request) -> tuple[str, str | None] | None:
    """(user, room scope) for a valid "Bearer <session>" header, else None."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    user, expires, scope = _sessions.get(header[len("Bearer "):].strip(), (None, 0, None))
    return (user, scope) if user and time.time() < expires else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None                           # verification stays on our own homeserver


def openid_user(access_token: str) -> str | None:
    """Ask our homeserver who an OpenID token belongs to (only local users count; fail closed)."""
    if not isinstance(access_token, str) or not access_token or len(access_token) > 512 or not access_token.isascii():
        return None                           # a lone surrogate can't even be quoted (GPT review: a 500, not a 401)
    url = f"{HS}/_matrix/federation/v1/openid/userinfo?access_token={urllib.parse.quote(access_token, safe='')}"
    try:
        with urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({})).open(url, timeout=10) as r:   # no env proxy
            data = json.loads(r.read())
    except (OSError, ValueError):
        return None
    sub = data.get("sub") if isinstance(data, dict) else None
    return sub if isinstance(sub, str) and USER_RE.match(sub) else None


def new_link(user_id: str, room: str) -> str:
    code = secrets.token_urlsafe(24)
    _links[code] = (user_id, time.time() + LINK_S, room)
    return code


def redeem(code: str) -> tuple[str, str] | None:
    """(user, room) for a fresh one-time code; the code is gone after this either way."""
    user, expires, room = _links.pop(code, (None, 0, ""))
    return (user, room) if user and time.time() < expires else None


async def view(loop, room_id: str, user: str) -> dict:
    """What this user may see and change in this room."""
    room = loop.client.rooms.get(room_id)
    if room is None or not loop.is_person(room, user):
        return {"error": "you're not in that room"}
    if project_rooms.is_front_door(room):     # screen test: the panel sat at "Signing in…" in the Lobby
        return {"error": project_rooms.NOT_A_PROJECT}
    local = user.split(":")[0].lstrip("@")
    agent = ownership.agent_id(user) if registry.owns(user, registry.agent_of(user)) else None   # by full ID
    waiting = loop.pending.get(room_id, {})
    mine = my_agents(loop, room, room_id, user)
    if console_personal.is_dm(loop, room):   # My Hagency: their personal cards only (or refused)
        return console_personal.view(loop, room, user, mine, can_add(user, mine),
                                     {"kinds": agent_kinds.kinds(), "github": github_link.status(user), "first_pick": first_pick(loop, user),
                                      "version": (customize.in_use().get(registry.local_name(user) or "") or "")[:7] if agent else None})
    return {"user": local, "agents": mine, "can_add": can_add(user, mine), "first_pick": first_pick(loop, user), "kinds": agent_kinds.kinds(), "room": room.display_name, "repo": await room_state.repo_of(loop.client, room_id),
            "moderator": await room_state.is_moderator(loop.client, room, user),
            "mode": await loop.level(room_id), "agent": agent and agent.split(":")[0][1:],
            "visibility": await project_rooms.visibility(loop.client, room_id), "runs_in": await runs_in(loop, room_id, user),
            "follow": registry.follow(user) if agent else None, "github": github_link.status(user),   # pm_github.py
            "pm_chat": (getattr(loop, "dm_rooms", None) or {}).get(user),   # my private chat with them (their view only): its links open it
            "version": (customize.in_use().get(registry.local_name(user) or "") or "")[:7] if agent else None,
            "remote": bool(agent and registry.load().get(user, {}).get("agent") == registry.REMOTE),   # on their computer
            "online": loop.agent_online(agent.split(":")[0][1:]) if agent else None,
            "waiting": {"kind": waiting.get("kind") or "mode", "mine": waiting.get("by") == user,
                        "id": waiting.get("event_id"), "items": [i["title"] for i in waiting.get("items", [])]}
                       if waiting else None,
            "html": panel_html(loop, room_id) if panel_html else ""}


async def runs_in(loop, room_id: str, user: str) -> dict | None:
    """Where this room's repo is run from, when this room only discusses it (pm_projects.py): that
    room's name and link only for its own members (others may not be allowed to know it)."""
    if not await room_state.discussion_of(loop.client, room_id):
        return None
    other = loop.discussions().get(room_id)
    there = loop.client.rooms.get(other) if other else None
    if there is None or user not in there.users or user in there.invited_users:
        return {"name": "another room", "link": ""}
    return {"name": loop.room_name(other), "link": f"{ELEMENT_ORIGIN}/#/room/{urllib.parse.quote(other)}"}


PANEL_LINK = "Your invite link is ready. Send it to the person you're inviting (it's in our private chat too)."


async def act(loop, room_id: str, user: str, action: str, value: str, agent: str = "") -> str | dict:
    """Do one change as `user`, through @pm's own checks (each also posts its usual chat line);
    `agent`: which of their agents (empty: their first)."""
    room = loop.client.rooms.get(room_id)
    if room is None or not loop.is_person(room, user):     # a joined human (never an agent or a bot)
        return "you're not in that room"
    if refused := (project_rooms.NOT_A_PROJECT if project_rooms.is_front_door(room)   # GPT review: no changes there
                   else console_personal.refusal(loop, room, user, action)):        # My Hagency: theirs, personal only
        return refused
    if action == "mode" and value in github.LEVELS:
        if value != "off" and not await room_state.is_moderator(loop.client, room, user):
            return "only the room owner or a maintainer can change the mode"
        await loop.say(room_id, await loop.change_level(room_id, value) or
                       f"Mode changed to {value} by {user.split(':')[0][1:]} (from the console)."
                       + await pm_paused.after_switch(loop, room_id, value))
        return "ok"
    if action == "follow" and value in registry.FOLLOW:
        done = await loop.set_follow(room, user, value, agent or None)
        return "ok" if done is not False else "that isn't one of your agents"
    if action == "add":                   # pm_agents.py: the Connect page goes to our private chat
        return await add_agent(loop, user, value)
    if action == "code":                  # a never-connected agent's fresh Connect page (console_agents)
        return await fresh_code(loop, user, agent or None)
    if action == "github":                # pm_github.py: Connect / Disconnect GitHub, for themselves only
        return await loop.github_action(user, value)
    if action in console_repo.ACTIONS:    # the room's repo: choose, remove, check readiness
        return await console_repo.act(loop, room, user, action, value)
    if action == "invite_link":           # pm_invite.py: shown here to copy, and sent to our private chat too
        said, url = await loop.invite_link_and_url(room, user)
        return {"result": PANEL_LINK, "link": url} if url else said
    if action == "visibility":            # pm_projects.py: who can find and join this room
        answer = await loop.set_visibility(room, user, value)
        await loop.say(room_id, answer)
        return answer
    if action == "goback":                # customize.py: official code, so it works when theirs is broken
        local, _, server = user.lstrip("@").partition(":")
        if server != loop.user_id.partition(":")[2]:   # a person from another server never maps to ours
            return "only agents this server runs can go back to the official version"
        customize.go_back(local)
        return "ok"
    if action == "remove" and value == "sure":   # pm_remove.py: for good; the page asked twice first
        done = await loop.remove_for_good(await loop.private_room(user) or room_id, user, agent or None)
        return "ok" if done else "couldn't remove it (see my message)"
    if action == "rename":                # pm_rename.py: their own agent's display name, answered in our chat
        return await loop.rename_agent(await loop.private_room(user) or room_id, user, agent or None, value)
    if action == "disconnect":            # pm_presence.py: their own computer's agent signs out for good
        done = await loop.disconnect(await loop.private_room(user) or room_id, user, agent or None)
        return "ok" if done else "couldn't disconnect it (see my message)"
    if action in ("stop", "start"):       # pm_stop.py: paused or resumed, still signed in on its computer
        done = await loop.set_stopped(await loop.private_room(user) or room_id, user, agent or None, action == "stop")
        return "ok" if done else f"couldn't {action} it (see my message)"
    if action == "approve":
        if loop.pending.get(room_id, {}).get("event_id") != value:
            return "that's no longer what's waiting; look again"   # approve exactly what you saw
        return "ok" if await loop.confirm(room, user) else "nothing of yours is waiting for approval"
    return "unknown action"


def allowed(request, post: bool) -> bool:
    """Only requests addressed to this machine; POSTs must be JSON from our own page."""
    hosts = allowed_hosts()
    if request.host.split(":")[0] not in hosts:
        return False
    if post:
        origin = request.headers.get("Origin")
        if request.content_type != "application/json" or (origin is not None and origin not in allowed_origins()):
            return False
    return True


NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def routes(loop, panel_html_fn) -> list:
    from server import agent_page, connect_page, invite_new_page, invite_page, signin_page   # here: they use this module (a cycle)
    global panel_html
    panel_html = panel_html_fn

    def reply(data, status=200):
        return web.json_response(data, status=status, headers=NO_STORE)

    async def auth_openid(request):
        if not allowed(request, True):
            return reply({"error": "refused"}, 403)
        from server import remote_agent                   # here: it imports this module; client_of: the proxy-aware address
        body = await request.json()
        body = body if isinstance(body, dict) else {}   # client_of may resolve the proxy's name: in the thread
        user = await asyncio.to_thread(lambda: fed_openid.user_of(body.get("access_token"), body.get("matrix_server_name"),
                                                                  remote_agent.client_of(request), SERVER_NAME, openid_user))
        return reply({"session": new_session(user)} if user else {"error": NOT_VERIFIED})

    async def auth_link(request):
        if not allowed(request, True):
            return reply({"error": "refused"}, 403)
        found = redeem(str((await request.json()).get("code", "")))
        return reply({"session": new_session(*found)} if found else {"error": "that link expired or was used"})

    def scoped(request, room):
        found = session_of(request)
        if not found or (found[1] and found[1] != room):
            return None                       # a link's session works only for its own room
        return found[0]

    async def state(request):
        user = scoped(request, request.query.get("room", "")) if allowed(request, False) else None
        if not user:
            return reply({"error": "sign in"}, 401)
        return reply(await view(loop, request.query.get("room", ""), user))

    async def action(request):
        body = await request.json() if allowed(request, True) else {}
        user = scoped(request, str(body.get("room", ""))) if body else None
        if not user:
            return reply({"error": "sign in"}, 401)
        done = await act(loop, str(body.get("room", "")), user, str(body.get("action", "")),
                         str(body.get("value", "")), str(body.get("agent", ""))[:120])
        return reply(done if isinstance(done, dict) else {"result": done})   # a dict: the result plus the invite link
    return [web.post("/auth/openid", auth_openid), web.post("/auth/link", auth_link),
            web.get("/console/state", state), web.post("/console/action", action),
            *connect_page.routes(), *invite_page.routes(loop),   # the Connect page and installer; invite links
            *signin_page.routes(), *agent_page.routes(loop), *invite_new_page.routes(loop)]   # ADR 0015: home page steps


panel_html = None                             # set by routes(): panel.render, kept out of an import cycle
