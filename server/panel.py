"""The Hagency side panel and console: a page @pm serves and adds to each project room as an
Element widget (and at /console for phones and other clients). It shows the live Project status,
what's waiting, the plan and whose agent is whose, and — once it knows who you are (console.py) —
lets you change the room's mode, your agent's follow-me setting, and approve what you asked
for, through @pm's own checks. Refreshed every few seconds; listens on 127.0.0.1 only (PANEL_HOST: the public server's container).
"""
import html
import json
import logging
import os
import urllib.parse

import markdown
from aiohttp import web

from server import console
from common.bridge_config import ELEMENT_HOME, ELEMENT_ORIGIN, panel_embedders
from server import github_proof
from server import remote_agent
from common import github
from common import identity
from server import ownership
from common import registry
from common import paths

PORT = console.PORT
HOST = os.environ.get("PANEL_HOST", "127.0.0.1")     # the public server's container: 0.0.0.0, behind Caddy
WIDGET_ID = "hagency"
WIDGET_KIND, LAYOUT_KIND = "im.vector.modular.widgets", "io.element.widgets.layout"   # the room state Element reads
TOP = {"container": "top", "index": 0, "width": 100, "height": 40}   # pinned above the chat, in every project room
STATIC = paths.ROOT.resolve() / "static"
# The panel's card scripts, by the URL the page loads them from. The one list: the page's <script>
# tags and the routes come from it, and deploy/Caddyfile's @pm route must name every one (test_panel).
SCRIPTS = {"panel-agents.js": STATIC / "panel_agents.js",      # "My agents"
           "panel-project.js": STATIC / "panel_project.js",    # "Project repo", "Who can join", "Run from another room"
           "panel-github.js": STATIC / "panel_github.js"}      # "Connect GitHub" (pm_github.py)
REFRESH_MS = 3000
SIGNIN_MS = 20000                 # no answer from Element by then: the panel says so instead of "Signing in…"
EMBEDDERS = panel_embedders()      # who may frame the panel (no X-Frame-Options: it can't name several)
HEADERS = {"Content-Security-Policy": f"frame-ancestors 'self' {' '.join(EMBEDDERS)}; default-src 'self'; "
                                      "script-src 'unsafe-inline' 'self'; style-src 'unsafe-inline'",
           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}


def browser_note(room_id: str) -> str:
    """For a room's first message: Element Desktop and other servers' Element apps never read our
    config.json, so they ask once before showing the panel; and the same panel opens in a browser."""
    url = f"{console.public_url()}/console?room={urllib.parse.quote(room_id)}"
    return (" (In Element Desktop or another server's Element, click Continue or Allow on the panel once. "
            f"Or [open the Hagency panel in your browser]({url}).)")


def widget_url() -> str:
    """The panel as Element loads it: this console's public URL ($matrix_room_id: Element fills it in)."""
    return f"{console.public_url()}/panel?room=$matrix_room_id"


def local_host(request) -> bool:
    """Only requests addressed to this machine or its public name (guards against DNS rebinding)."""
    return request.host.split(":")[0] in console.allowed_hosts()
log = logging.getLogger("loop")

PAGE = (STATIC / "panel.html").read_text(encoding="utf-8").replace("__REFRESH_MS__", str(REFRESH_MS))
PAGE = PAGE.replace("__SIGNIN_MS__", str(SIGNIN_MS))
PAGE = PAGE.replace("__ELEMENT_HOME__", html.escape(ELEMENT_HOME, quote=True))
PAGE = PAGE.replace("__SCRIPTS__", "".join(f'<script src="{name}"></script>' for name in SCRIPTS))


def no_html(text: str) -> str:
    """Board text is markdown only: any raw HTML in it (from a stored board) shows as text."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


def render(loop, room_id: str) -> str:
    """The panel's HTML for one room (all text escaped except @pm's own board markdown)."""
    room = loop.client.rooms.get(room_id)
    if room is None:
        return "<p class=muted>@pm isn't in this room.</p>"
    board = loop.boards.get(room_id, {}).get("text") or "No status yet."
    if board.startswith("📋"):                # its title line repeats the page header (repo, mode)
        board = board.split("\n", 1)[1].strip() if "\n" in board else "No status yet."
    waiting = loop.pending.get(room_id)
    parts = [f"<section class=card><h2>Project status</h2><div class=board>{markdown.markdown(github.linkify(no_html(board), loop.room_repos.get(room_id)))}</div></section>"]
    if waiting:
        who = html.escape(str(waiting.get("by", "")).split(":")[0].lstrip("@"))
        what = {"plan": "a plan", "plan_request": "turning the discussion into work", "bring": "bringing an agent",
                "mode": f"switching to {html.escape(str(waiting.get('level')))}"}.get(waiting.get("kind"), "a change")
        parts.append(f"<section class=card><h2>Waiting for approval</h2><div>{what} · {who or 'the owner'} clicks 👍 in the chat</div></section>")
    plan = loop.last_plans.get(room_id, {}).get("items", [])
    if plan:
        items = "".join(f"<li><b>{html.escape(i['title'])}</b></li>" for i in plan)
        parts.append(f"<section class=card><h2>Current plan</h2><ol>{items}</ol></section>")
    agents = []
    for user in sorted(room.users):
        owner = ownership.owner_id(user)
        if owner:
            here = "" if ownership.joined(room, owner) else " <span class=muted>(owner not here: idle)</span>"
            if not ownership.joined(room, user):          # screen test: an invite read as if it had joined
                online = getattr(loop, "agent_online", lambda agent: None)(user.split(":")[0][1:])   # pm_presence.py
                here += f" <span class=chip>invited — {'joining' if online else 'not connected yet'}</span>"
            follow = registry.follow(owner, user.split(":")[0][1:])
            agents.append(f"<li><div><b>{html.escape(user.split(':')[0][1:])}</b> <span class=muted>· "
                          f"{html.escape(identity.shown_name(room, owner))}'s agent{here}</span></div>"
                          f"<div><span class=chip>comes along: {html.escape(follow)}</span></div></li>")
    parts.append("<section class=card><h2>Agents in this room</h2><ul class=agents>"
                 + ("".join(agents) or "<li class=muted>None yet</li>") + "</ul></section>")
    return "".join(parts)


async def start(loop) -> web.AppRunner | None:
    """Serve the panel on 127.0.0.1:PORT; never fatal (the chat works without it)."""
    async def page(request):
        if not local_host(request):
            return web.Response(status=403)
        return web.Response(text=PAGE.replace("__EMBEDDERS__", json.dumps(EMBEDDERS)),
                            content_type="text/html", headers=HEADERS)

    def script(path):                     # the card scripts (SCRIPTS)
        async def serve(request):
            if not local_host(request):
                return web.Response(status=403)
            return web.Response(text=path.read_text(encoding="utf-8"), content_type="application/javascript", headers=HEADERS)
        return serve
    app = web.Application()
    app.add_routes([web.get("/panel", page), web.get("/console", page),
                    *(web.get(f"/{name}", script(path)) for name, path in SCRIPTS.items()),
                    *console.routes(loop, render),
                    *remote_agent.routes(),      # bring-your-own pairing (remote_agent.py)
                    *github_proof.routes()])     # one GitHub sign-in for a person and their agent (ADR 0017)
    runner = web.AppRunner(app, access_log=None)        # the widget polls every 3 s: don't flood the log
    await runner.setup()
    try:
        await web.TCPSite(runner, HOST, PORT).start()
    except OSError as e:
        log.warning("side panel not started (port %s): %s", PORT, e)
        return None
    return runner


async def add_to_room(client, room_id: str) -> bool:
    """Add the panel to the room as an Element widget, pinned at the top above the chat."""
    resp = await client.room_put_state(room_id, WIDGET_KIND,
                                       {"type": "m.custom", "url": widget_url(), "name": "Hagency",
                                        "id": WIDGET_ID, "data": {}}, state_key=WIDGET_ID)
    if not hasattr(resp, "event_id"):
        return False
    await client.room_put_state(room_id, LAYOUT_KIND, {"widgets": {WIDGET_ID: TOP}})   # pinned above the chat
    return True


async def lift_to_top(client, room_id: str) -> bool:
    """Older rooms have the panel in Element's right-side Extensions list ({"container": "right"}),
    where owners took it for missing (seen live): put it on top as in new rooms. Only a room with my
    widget, only when it isn't on top already; other widgets keep their places. True when moved.
    Raises RuntimeError when the room's state can't be read or written (the caller goes on)."""
    widget = await client.room_get_state_event(room_id, WIDGET_KIND, WIDGET_ID)
    if not isinstance(getattr(widget, "content", None), dict) or widget.content.get("url") != widget_url():
        if getattr(widget, "status_code", None) not in (None, "M_NOT_FOUND"):
            raise RuntimeError(f"couldn't read the panel widget: {widget}")
        return False                          # not my panel (GPT review: never another page put under its key)
    found = await client.room_get_state_event(room_id, LAYOUT_KIND, "")
    content = getattr(found, "content", None)
    if not isinstance(content, dict):
        if getattr(found, "status_code", None) != "M_NOT_FOUND":
            raise RuntimeError(f"couldn't read the widget layout: {found}")
        content = {}
    places = content.get("widgets") if isinstance(content.get("widgets"), dict) else {}
    mine = places.get(WIDGET_ID)
    if isinstance(mine, dict) and mine.get("container") == "top":
        return False
    new = {**content, "widgets": {**places, WIDGET_ID: TOP}}
    if not hasattr(await client.room_put_state(room_id, LAYOUT_KIND, new), "event_id"):
        raise RuntimeError("couldn't save the widget layout")
    return True


async def remove_from_room(client, room_id: str) -> bool:
    """Take the panel out of a room that isn't a project room (the Lobby, Welcome: screen test, where it
    sat at "Signing in…"). True once the widget and its layout place are both gone (False: retried);
    nothing is written for what isn't there."""
    for kind, key in ((WIDGET_KIND, WIDGET_ID), (LAYOUT_KIND, "")):
        found = await client.room_get_state_event(room_id, kind, key)
        content = getattr(found, "content", None)
        if not isinstance(content, dict):
            if getattr(found, "status_code", None) != "M_NOT_FOUND":
                return False                  # unreadable: never taken for "not there"
            continue
        places = content.get("widgets") if kind == LAYOUT_KIND else None
        if kind == WIDGET_KIND and content.get("url"):
            new = {}
        elif isinstance(places, dict) and WIDGET_ID in places:     # GPT review: other widgets keep their places
            new = {**content, "widgets": {k: v for k, v in places.items() if k != WIDGET_ID}}
        else:
            continue                          # removed already
        if not hasattr(await client.room_put_state(room_id, kind, new, state_key=key), "event_id"):
            return False
    return True
