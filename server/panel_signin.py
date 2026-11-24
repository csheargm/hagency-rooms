"""The panel popped out of Element (its ↗ button) signs people in itself (owner, 2026-11-23: "it gave me almost a
blank page"). The page signs in with our homeserver (static/signin.js handOver: Google, GitHub or a password,
straight to Synapse) and shows the throwaway session here once; @pm asks our homeserver whose it is
(page_session.whoami: people on this server, no guests) and answers with a panel session, as /auth/openid
does inside Element. Counted per address and in all before the homeserver call, as the other sign-in pages.
"""
import asyncio

from aiohttp import web

from server import page_session
from server import remote_agent
from server import signin_page

PATH, SCOPE = "/auth/page", "panel"
FAILED = "Couldn't sign you in; please try again."


def route(allowed, reply, new_session):
    """console.routes' own checks (allowed: this host, JSON from our page), reply and session maker."""
    async def auth_page(request):
        if not allowed(request, True):
            return reply({"error": "refused"}, 403)
        if not signin_page.may_ask(SCOPE, remote_agent.client_of(request)):
            return reply(signin_page.TOO_MANY, 429)
        try:
            body = await request.json()
        except ValueError:                   # not JSON: refused like a wrong token
            body = {}
        user = await asyncio.to_thread(page_session.whoami, body.get("access_token") if isinstance(body, dict) else None)
        return reply({"session": new_session(user)} if user else {"error": FAILED})
    return web.post(PATH, auth_page)
