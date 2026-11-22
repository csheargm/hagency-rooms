"""The Connect page: the one link @pm sends instead of a terminal line, and the Windows installer.

GET /connect/start#CODE (or #CODE&kind=ollama, page_link) shows numbered steps for the visitor's
computer (macOS, Windows or Linux, detected in the browser, with tabs to switch), one big "Copy
command" button for the exact line, a "Before you start" checklist for the chosen kind
(agent_kinds.kinds(), embedded as data) and a live status. The code travels in the URL fragment,
which browsers never send to a server, so it never reaches this server's or the proxy's logs. The
page's own script sends it only in the body of POST /connect/status (connect_status.py), which
answers waiting / connected / expired and nothing else. The Content-Security-Policy allows only
that: our own script and style, requests to this origin only, no inline script, frame or form.
GET /connect.ps1 serves the Windows installer, as /connect.sh (remote_agent.py) serves the other.
"""
import json
from pathlib import Path

from aiohttp import web

from common import agent_kinds
from server import connect_status
from server import console
from common import github
from common.bridge_config import ELEMENT_ORIGIN, SERVER_NAME
from server import remote_agent
from server.remote_agent import CONSOLE_MARK         # the placeholder connect.sh uses, filled in the same way
from common import paths

ROOT = paths.ROOT.resolve()
STATIC = ROOT / "static"
WINDOWS_INSTALLER = ROOT / "client" / "connect.ps1"
PAGE_PATH = "/connect/start"
STATUS_PATH = "/connect/status"
KINDS_MARK = "__HAGENCY_KINDS__"
PM_MARK = "__HAGENCY_PM_LINK__"
NO_STORE = console.NO_STORE
# Our own script and style; requests only back to this origin (the status); no beacon elsewhere,
# form or frame.
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")
ASSETS = {f"{PAGE_PATH}.js": ("connect.js", "application/javascript"),
          f"{PAGE_PATH}.css": ("connect.css", "text/css")}


def page_link(code: str, kind: str | None = None) -> str:
    """What @pm links to: the Connect page with this one-time code (and the agent's kind, so the
    page shows its checklist and the line names it) in the fragment."""
    extra = f"&kind={kind}" if kind in agent_kinds.KINDS else ""
    return f"{console.public_url()}{PAGE_PATH}#{code}{extra}"


def pm_link() -> str:
    """Element's page for @pm, where the person's chat with it is (the same for everyone)."""
    return f"{ELEMENT_ORIGIN}/#/user/@{github.PM_BOT}:{SERVER_NAME}"


def _kinds_json() -> str:
    """The kinds as JSON for a <script type="application/json"> (data, never run): no "<" in it."""
    return json.dumps(agent_kinds.kinds()).replace("<", "\\u003c").replace("&", "\\u0026")


def _with_console(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace(CONSOLE_MARK, console.public_url())


def _page() -> str:
    link = pm_link().replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")
    return _with_console(STATIC / "connect.html").replace(KINDS_MARK, _kinds_json()).replace(PM_MARK, link)


async def _status(request):
    """waiting / connected / expired for the code in the body (connect_status.py)."""
    client = remote_agent.client_of(request)
    if request.content_type != "application/json":
        return web.json_response({"error": "send JSON"}, status=415, headers=NO_STORE)
    if connect_status.LIMIT.blocked(client):      # already over: no body read (not counted; GPT re-check)
        return web.json_response({"error": "too many; wait a minute"}, status=429, headers=NO_STORE)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    # The check, the state and the wrong-code charge with no await in between: concurrent asks of one
    # client can't all pass the check before any is charged (GPT review, batch 12-14).
    if connect_status.too_many(client):
        return web.json_response({"error": "too many; wait a minute"}, status=429, headers=NO_STORE)
    state = connect_status.state(remote_agent.CODES, body.get("code") if isinstance(body, dict) else None)
    if state == "expired":
        connect_status.LIMIT.failed(client)
    return web.json_response({"state": state}, headers=NO_STORE)


def routes() -> list:
    async def page(request):
        # The request itself is never read: the code is in the fragment, which never arrives here.
        return web.Response(text=_page(), content_type="text/html",
                            headers={**NO_STORE, "Content-Security-Policy": PAGE_CSP})

    async def asset(request):
        name, kind = ASSETS[request.path]
        return web.Response(text=(STATIC / name).read_text(encoding="utf-8"), content_type=kind, headers=NO_STORE)

    async def windows_installer(request):
        return web.Response(text=_with_console(WINDOWS_INSTALLER), content_type="text/plain", headers=NO_STORE)
    return [web.get(PAGE_PATH, page), *(web.get(p, asset) for p in ASSETS),
            web.get("/connect.ps1", windows_installer), web.post(STATUS_PATH, _status)]
