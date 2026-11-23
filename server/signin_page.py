"""What Hagency's sign-in pages share (docs/adr/0014 and 0015): the invite page (/invite), Get your
agent (/connect/new) and Make an invite link (/invite/new).

Each page signs the person in straight with our homeserver (Google, GitHub or a password: static/
signin.js, served here at /auth/signin.js); @pm never sees a password. The invite page then posts an
OpenID token, which verified() checks with our homeserver (console.openid_user: people on this server
only); the two newer pages prove a real session instead (page_session.py).
"""
import asyncio

from aiohttp import web

from common.bridge_config import ELEMENT_ORIGIN
from server import console
from server import fed_openid
from server import remote_agent
from server.connect_page import STATIC

SCRIPT_PATH = "/auth/signin.js"
HS_MARK, ELEMENT_MARK = "__HAGENCY_HOMESERVER__", "__HAGENCY_ELEMENT__"
TOO_MANY = {"state": "busy"}
TRIES_PER_CLIENT, TRIES_ALL = 30, 600    # signed-in asks of the newer pages, per fed_openid.WINDOW_S


def page_csp() -> str:
    """Our own script and style; requests to this origin and to our homeserver only; no frame or form."""
    return ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            f"connect-src 'self' {remote_agent.PUBLIC_HOMESERVER}; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def filled(name: str, **marks: str) -> str:
    """static/<name> with our homeserver's and Element's addresses (and any other marks) filled in."""
    html = (STATIC / name).read_text(encoding="utf-8")
    for mark, value in {HS_MARK: remote_agent.PUBLIC_HOMESERVER, ELEMENT_MARK: ELEMENT_ORIGIN, **marks}.items():
        html = html.replace(mark, attr(value))
    return html


def reply(data, status=200):
    return web.json_response(data, status=status, headers=console.NO_STORE)


def page(name: str):
    """A GET handler for one of these pages: never reads the request (what matters is in the fragment)."""
    async def handler(request):
        return web.Response(text=filled(name), content_type="text/html",
                            headers={**console.NO_STORE, "Content-Security-Policy": page_csp()})
    return handler


def assets(files: dict):
    """A GET handler serving {path: (static file, content type)}."""
    async def handler(request):
        name, kind = files[request.path]
        return web.Response(text=(STATIC / name).read_text(encoding="utf-8"), content_type=kind, headers=console.NO_STORE)
    return handler


async def json_body(request) -> dict | None:
    """The JSON body of a POST from our own page, or None."""
    if not console.allowed(request, True):
        return None
    try:
        body = await request.json()
    except (ValueError, RecursionError):   # deeply nested JSON too: a plain no, not a 500 (GPT re-check)
        return None
    return body if isinstance(body, dict) else None


def may_ask(scope: str, client: str) -> bool:
    """One more signed-in ask of `scope` for this client address, and for everyone together."""
    return fed_openid.may_try(f"{scope}:{client}", TRIES_PER_CLIENT) and fed_openid.may_try(f"{scope}:all", TRIES_ALL)


async def verified(request, may):
    """(body, the signed-in user) for a POST carrying an OpenID token, or the web.Response refusing it.
    may(client address): one more try allowed (counted), as invite_links.may_look."""
    body = await json_body(request)
    if body is None:
        return reply({"error": "refused"}, 403)
    if not may(remote_agent.client_of(request)):
        return reply(TOO_MANY, 429)
    user = await asyncio.to_thread(console.openid_user, body.get("openid"))
    if user is None:
        return reply({"state": "not_signed_in"}, 401)
    return body, user


def routes() -> list:
    return [web.get(SCRIPT_PATH, assets({SCRIPT_PATH: ("signin.js", "application/javascript")}))]
