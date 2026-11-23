"""The invite page (docs/adr/0014-one-step-invite-link.md): GET /invite#<token>, its script and
style, and the two calls its script makes.

  POST /invite/check  {token}          → {state, room?, by?}: the room's name only for a usable link
  POST /invite/redeem {token, openid}  → {state, room?}: who signed in comes from our homeserver
                                         (console.openid_user), never from the page

The token is in the fragment, so it reaches us only in these bodies, which nothing logs. Both calls
count against invite_links.may_look per client address. The page signs in with our homeserver
directly (Google, GitHub or a password): this server never sees a password or a session.
"""
from aiohttp import web

from common import identity
from server import console
from server import invite_links
from server import remote_agent
from server import signin_page
from server.signin_page import TOO_MANY, json_body, reply

PAGE_PATH = "/invite"
ASSETS = {f"{PAGE_PATH}/page.js": ("invite.js", "application/javascript"),
          f"{PAGE_PATH}/page.css": ("invite.css", "text/css")}
page_csp = signin_page.page_csp


def link(token: str) -> str:
    return f"{console.public_url()}{PAGE_PATH}#{token}"


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
    async def check(request):
        body = await json_body(request)
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
        found = await signin_page.verified(request, invite_links.may_look)   # counted with check
        if isinstance(found, web.Response):
            return found
        body, user = found
        return reply(await loop.redeem_invite(body.get("token"), user))
    return [web.get(PAGE_PATH, signin_page.page("invite.html")), *(web.get(p, signin_page.assets(ASSETS)) for p in ASSETS),
            web.post(f"{PAGE_PATH}/check", check), web.post(f"{PAGE_PATH}/redeem", redeem)]
