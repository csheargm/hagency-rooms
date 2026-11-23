"""Get your agent (docs/adr/0015-one-click-home-steps.md): step ① on Element's home page, without
a detour through the chat with @pm.

GET /connect/new signs the person in (signin_page.py, static/signin.js), then its script asks:
  POST /connect/new/me   {openid}        → {state: "ok", agent, connected, pick, picked, runs}, or {state, error}
  POST /connect/new/code {openid, pick}  → {state: "ok", code, kind?}, or {state, error}
and opens the Connect page (/connect/start#CODE&kind=KIND) with that code, exactly the page @pm's
message links to. The code is made as 🔌 Bring your own makes it (pm_first.bring_own): their first
agent is set up (remote_agent.provision) and remote_agent.CODES.issue binds a code to the person the
homeserver vouched for; their older codes stop working; it works once, for pairing.CODE_S. The code
goes only into that reply's body, never a log line. pick is the panel's picker JSON (pm_kinds.from_panel),
taken only while their first agent has never connected, as in My Hagency (console_agents.first_pick).
"""
import asyncio
import json
import logging

from aiohttp import web

from common import agent_kinds
from common import pm_kinds
from common import registry
from common.redact import room_safe
from common.setup_accounts import AccountError
from server import connect_page
from server import console
from server import console_agents
from server import pm_first
from server import remote_agent
from server import remote_people
from server import signin_page
from server.signin_page import reply

log = logging.getLogger("loop")
PAGE_PATH = "/connect/new"
ASSETS = {f"{PAGE_PATH}/page.js": ("agent.js", "application/javascript")}
ASKS = signin_page.asks("agent")
REMOVED = ("You removed your first agent, so a new one needs a new name. Add it under My agents, at the top "
           "of your chat with the Product Manager.")
BUSY = "I'm already setting up an agent for you; one moment, then try again."


def status_of(loop, user: str) -> dict:
    """What the page shows once they're signed in: their first agent, and whether they may choose its AI."""
    if not loop.may_have_agent(user):
        return {"state": "refused", "error": f"Sorry, {remote_people.OFF}."}
    if registry.first_removed(user):
        return {"state": "refused", "error": REMOVED}
    first = registry.find(user, None)
    return {"state": "ok", "agent": first["localpart"] if first else registry.agent_of(user),
            "connected": bool(first) and loop.ever_connected(first["localpart"]),
            "pick": console_agents.first_pick(loop, user), "runs": console_agents.badge(first) if first else "",
            "picked": {k: first.get(k, "") for k in agent_kinds.OFFER_KEYS} if first else {}}


def chosen(now: dict, pick: object) -> dict | str | None:
    """The AI to record ({}: the one on their computer; None: keep what it has), or why not."""
    _, picked = pm_kinds.from_panel(pick) if isinstance(pick, str) and pick.strip() else ("", None)
    if isinstance(picked, str):
        return picked
    if not now["pick"]:                       # it has connected: it keeps its AI (pm_first.CONNECTED)
        return pm_first.CONNECTED if picked else None
    return picked or {}                       # the picker's "the one on my computer" clears an earlier choice


async def issue(loop, user: str, pick: object) -> dict:
    """Set up their first agent if need be and give a fresh one-time code for it."""
    now = status_of(loop, user)
    if now["state"] != "ok":
        return now
    picked = chosen(now, pick)
    if isinstance(picked, str):
        return {"state": "bad_pick", "error": picked}
    busy = loop.__dict__.setdefault("_provisioning", set())   # shared with 🔌 and Add an agent: one at a time
    if user in busy:
        return {"state": "busy", "error": BUSY}
    busy.add(user)
    try:
        await asyncio.to_thread(remote_agent.provision, user, picked)
    except (AccountError, OSError, RuntimeError) as e:   # refused, cap reached, .env unwritable (as bring_own)
        log.error("get-your-agent page: setting up failed for %s: %s", user, e)
        return {"state": "failed", "error": f"I couldn't set up your agent: {room_safe(e)}."}
    finally:
        busy.discard(user)
    code = remote_agent.CODES.issue(user)
    kind = (registry.find(user, None) or {}).get("agent_kind")
    log.info("get-your-agent page: a code for %s's first agent", user)   # never the code
    return {"state": "ok", "code": code, **({"kind": kind} if kind in agent_kinds.KINDS else {})}


def routes(loop) -> list:
    async def page(request):
        html = signin_page.filled("agent.html").replace(connect_page.KINDS_MARK, connect_page.kinds_json())
        return web.Response(text=html, content_type="text/html",
                            headers={**console.NO_STORE, "Content-Security-Policy": signin_page.page_csp()})

    async def me(request):
        found = await signin_page.verified(request, ASKS)
        return found if isinstance(found, web.Response) else reply(status_of(loop, found[1]))

    async def code(request):
        found = await signin_page.verified(request, ASKS)
        if isinstance(found, web.Response):
            return found
        body, user = found
        pick = body.get("pick")
        return reply(await issue(loop, user, json.dumps(pick) if isinstance(pick, dict) else pick))
    return [web.get(PAGE_PATH, page), *(web.get(p, signin_page.assets(ASSETS)) for p in ASSETS),
            web.post(f"{PAGE_PATH}/me", me), web.post(f"{PAGE_PATH}/code", code)]
