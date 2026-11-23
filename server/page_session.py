"""Who is asking, for Get your agent and Make an invite link (docs/adr/0015-one-click-home-steps.md).

An OpenID token is not enough here (GPT review): Matrix doesn't tie it to one service, so a widget or
integration a person once let "verify their identity" could replay it within its hour and take a
pairing code (their agent's sign-in) or make invite links as them. Instead the page proves it holds a
real session of that person: it signs in with our homeserver (a new, throwaway session), posts that
session's access token ONCE, and @pm asks our homeserver /whoami with it (people on this server only,
no guests). It is used for that one call, never stored or logged, and the page signs it out right
after. @pm answers with its own short session for this page (SESSION_S, one page's scope only), which
the page keeps in memory for the rest of its calls.
"""
import asyncio
import json
import re
import secrets
import time
import urllib.request

from aiohttp import web

from server import console
from server import remote_agent
from server import signin_page
from server.signin_page import TOO_MANY, json_body, reply

SESSION_S, MAX_SESSIONS = 30 * 60, 1000
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")   # what a homeserver access token looks like; nothing for a header
_sessions: dict[str, tuple[str, str, float]] = {}     # @pm's page session → (user, scope, expires)


def whoami(access_token: object) -> str | None:
    """The person this access token belongs to, asked of our homeserver; None for anything else (fail closed)."""
    if not isinstance(access_token, str) or not TOKEN_RE.match(access_token):
        return None
    req = urllib.request.Request(f"{console.HS}/_matrix/client/v3/account/whoami",
                                 headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.build_opener(console._NoRedirect, urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        return None
    user = data.get("user_id") if isinstance(data, dict) else None
    guest = isinstance(data, dict) and data.get("is_guest") is True
    return user if isinstance(user, str) and console.USER_RE.match(user) and not guest else None


def open_session(user: str, scope: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    for key in [k for k, (_, _, until) in _sessions.items() if until <= now]:
        del _sessions[key]
    while len(_sessions) >= MAX_SESSIONS:             # full: the oldest goes
        del _sessions[min(_sessions, key=lambda k: _sessions[k][2])]
    session = secrets.token_urlsafe(32)
    _sessions[session] = (user, scope, now + SESSION_S)
    return session


def user_of(session: object, scope: str, now: float | None = None) -> str | None:
    found = _sessions.get(session) if isinstance(session, str) else None
    now = time.time() if now is None else now
    return found[0] if found and found[1] == scope and found[2] > now else None


async def signed_in(request, scope: str):
    """(body, user, new page session or None) for a POST from one of these pages, or the web.Response
    refusing it. Counted per client address and for everyone before any homeserver call."""
    body = await json_body(request)
    if body is None:
        return reply({"error": "refused"}, 403)
    if not signin_page.may_ask(scope, remote_agent.client_of(request)):
        return reply(TOO_MANY, 429)
    user = user_of(body.get("session"), scope)
    if user is not None:
        return body, user, None
    user = await asyncio.to_thread(whoami, body.get("access_token"))
    if user is None:
        return reply({"state": "not_signed_in"}, 401)
    return body, user, open_session(user, scope)


def handler(scope: str, answer):
    """A POST handler: answer(body, user) → dict, after signed_in; a new page session goes back with it."""
    async def handle(request):
        found = await signed_in(request, scope)
        if isinstance(found, web.Response):
            return found
        body, user, fresh = found
        return reply({**await answer(body, user), **({"session": fresh} if fresh else {})})
    return handle
