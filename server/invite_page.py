"""The invite page (docs/adr/0014-one-step-invite-link.md): GET /invite#<token>, its script and
style, and the two calls its script makes.

  POST /invite/check  {token}          → {state, room?, by?}: the room's name only for a usable link
  POST /invite/redeem {token, openid}  → {state, room?}: who signed in comes from our homeserver
                                         (console.openid_user), never from the page

The token is in the fragment, so it reaches us only in these bodies, which nothing logs. Both calls
count against invite_links.may_look per client address. The page signs in with our homeserver
directly (Google, GitHub or a password): this server never sees a password or a session.
"""
import asyncio

from aiohttp import web

from common import identity
from common.bridge_config import ELEMENT_ORIGIN
from server import console
from server import invite_links
from server import remote_agent
from server.connect_page import STATIC

PAGE_PATH = "/invite"
HS_MARK, ELEMENT_MARK = "__HAGENCY_HOMESERVER__", "__HAGENCY_ELEMENT__"
ASSETS = {f"{PAGE_PATH}/page.js": ("invite.js", "application/javascript"),
          f"{PAGE_PATH}/page.css": ("invite.css", "text/css")}
TOO_MANY = {"state": "busy"}


def page_csp() -> str:
    """Our own script and style; requests to this origin and to our homeserver only; no frame or form."""
    return ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            f"connect-src 'self' {remote_agent.PUBLIC_HOMESERVER}; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def link(token: str) -> str:
    return f"{console.public_url()}{PAGE_PATH}#{token}"


def _attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _page() -> str:
    html = (STATIC / "invite.html").read_text(encoding="utf-8")
    return html.replace(HS_MARK, _attr(remote_agent.PUBLIC_HOMESERVER)).replace(ELEMENT_MARK, _attr(ELEMENT_ORIGIN))


async def _body(request) -> dict | None:
    """The JSON body of a POST from our own page, or None."""
    if not console.allowed(request, True):
        return None
    try:
        body = await request.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def shown(name: str) -> str:
    """A name someone chose, on one line and not too long (the page shows it as text, never HTML)."""
    return " ".join(str(name).split())[:80]


def describe(loop, link_: dict) -> dict:
    """What the page shows for a usable link: the room's name and who invited them."""
    room = loop.client.rooms.get(link_["room"])
    by = link_["by"]
    name = (room.user_name(by) if room is not None else None) or identity.split(by)[0].lstrip("@")
    return {"state": invite_links.OK, "room": shown(room.display_name) if room is not None else "a project room",
            "by": shown(name)}


def routes(loop) -> list:
    def reply(data, status=200):
        return web.json_response(data, status=status, headers=console.NO_STORE)

    async def page(request):
        return web.Response(text=_page(), content_type="text/html",
                            headers={**console.NO_STORE, "Content-Security-Policy": page_csp()})

    async def asset(request):
        name, kind = ASSETS[request.path]
        return web.Response(text=(STATIC / name).read_text(encoding="utf-8"), content_type=kind, headers=console.NO_STORE)

    async def check(request):
        body = await _body(request)
        if body is None:
            return reply({"error": "refused"}, 403)
        if not invite_links.may_look(remote_agent.client_of(request)):
            return reply(TOO_MANY, 429)
        state, found = invite_links.look_up(body.get("token"))
        if found and not await loop.link_stands(loop.client.rooms.get(found["room"]), found):
            return reply({"state": "closed"})          # the room's name only while its maker may still invite
        if found:                                      # looked up again after that await (GPT re-check)
            state, found = invite_links.look_up(body.get("token"))
        return reply(describe(loop, found) if found else {"state": state})

    async def redeem(request):
        body = await _body(request)
        if body is None:
            return reply({"error": "refused"}, 403)
        if not invite_links.may_look(remote_agent.client_of(request)):
            return reply(TOO_MANY, 429)
        user = await asyncio.to_thread(console.openid_user, body.get("openid"))
        if user is None:
            return reply({"state": "not_signed_in"}, 401)
        return reply(await loop.redeem_invite(body.get("token"), user))
    return [web.get(PAGE_PATH, page), *(web.get(p, asset) for p in ASSETS),
            web.post(f"{PAGE_PATH}/check", check), web.post(f"{PAGE_PATH}/redeem", redeem)]
