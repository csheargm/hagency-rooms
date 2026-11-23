"""One GitHub sign-in for a person and their agent (ADR 0017): the agent proves which account it holds.

The connect line signs the agent in to GitHub on its owner's computer (client/github_signin.py). This
module then links that account to the owner, as "Connect GitHub" (pm_github.py) does, without any token
reaching the server:

  POST /connect/github/challenge   Bearer <agent's Matrix token>  → {nonce}
  POST /connect/github/proof       Bearer <agent's Matrix token>, {gist}  → {login}

The agent puts the nonce in a secret gist made with its own gh sign-in. @pm reads that gist from GitHub
itself and takes the owner's login and id from GitHub's answer, never from the client: GitHub sets a
gist's owner to the account whose credential made it, so a modified client can't claim an account it
doesn't control. A nonce works once, for this agent only, for NONCE_S.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request

from aiohttp import web

from common import github
from common import registry
from common.bridge_config import SERVER_NAME
from server import github_link
from server import page_session
from server.remote_agent import NO_STORE, client_of

log = logging.getLogger("loop")
NONCE_S, PER_AGENT_PER_HOUR, MAX_PENDING = 600, 5, 1000
GIST_RE = re.compile(r"^[0-9a-f]{20,40}$")
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
DIFFERENT = ("Your GitHub account is @{linked}, but your agent signed in as @{login}. Disconnect GitHub in the "
             "Hagency panel first, or sign your agent in as @{linked}.")
_pending: dict[str, tuple[str, str, float]] = {}   # nonce → (agent localpart, owner, issued)
_asked: dict[str, list[float]] = {}


def agent_and_owner(token: object) -> tuple[str, str] | None:
    """(agent localpart, its owner's full ID) for an agent's own Matrix token, or None (fail closed)."""
    user = page_session.whoami(token)
    local, _, server = (user or "").lstrip("@").partition(":")
    if not user or server != SERVER_NAME or not local.endswith("-agent"):
        return None
    owner = registry.owner_of(local)
    return (local, owner) if owner else None


def challenge(agent: str, owner: str, now: float | None = None) -> str | None:
    """A fresh nonce for this agent, or None when it asked too often."""
    now = time.time() if now is None else now
    recent = _asked[agent] = [t for t in _asked.get(agent, []) if now - t < 3600]
    if len(recent) >= PER_AGENT_PER_HOUR:
        return None
    recent.append(now)
    for key in [k for k, (_, _, at) in _pending.items() if now - at > NONCE_S]:
        del _pending[key]
    while len(_pending) >= MAX_PENDING:
        del _pending[min(_pending, key=lambda k: _pending[k][2])]
    nonce = secrets.token_urlsafe(32)
    _pending[nonce] = (agent, owner, now)
    return nonce


def read_gist(gist: str) -> dict:
    """GitHub's own answer for a gist (no credentials). Raises github.GitHubError when it can't be read."""
    req = urllib.request.Request(f"{API}/gists/{gist}", headers={"Accept": "application/vnd.github+json",
                                                                  "User-Agent": "hagency-rooms"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read(1_000_000))
    except (OSError, ValueError) as e:            # HTTPError is an OSError: not found, rate limit, network
        raise github.GitHubError(f"GitHub couldn't show the check gist ({type(e).__name__})") from None
    if not isinstance(data, dict):
        raise github.GitHubError("GitHub's answer for the check gist wasn't an object")
    return data


def owner_if_proved(data: dict, nonce: str) -> tuple[str, int] | None:
    """(login, id) of the gist's owner when the gist holds exactly this nonce, is no fork and is new enough."""
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    contents = [f.get("content") for f in files.values() if isinstance(f, dict)]
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    login, gid = owner.get("login"), owner.get("id")
    if contents != [nonce] or data.get("fork_of") or not isinstance(login, str) \
            or not github.LOGIN_RE.match(login) or not isinstance(gid, int) or isinstance(gid, bool):
        return None
    return login, gid


async def prove(agent: str, owner: str, nonce: object, gist: object) -> tuple[int, dict]:
    """Check the gist and link its owner's account to the agent's owner. (HTTP status, answer)."""
    pending = _pending.pop(nonce, None) if isinstance(nonce, str) else None   # once, whatever happens next
    if not pending or pending[:2] != (agent, owner) or time.time() - pending[2] > NONCE_S:
        return 400, {"error": "that check expired or isn't yours: run the connect line again"}
    if not isinstance(gist, str) or not GIST_RE.match(gist):
        return 400, {"error": "not a gist id"}
    try:
        found = owner_if_proved(await asyncio.to_thread(read_gist, gist), nonce)
    except github.GitHubError as e:
        return 502, {"error": str(e)}
    if not found:
        return 400, {"error": "the gist didn't hold this check"}
    login, gid = found
    linked = github_link.link_of(owner)
    if linked and linked["id"] != gid:            # never relinked silently: the person decides
        return 409, {"error": DIFFERENT.format(linked=linked["login"], login=login)}
    try:
        github_link.save_link(owner, login, gid)
    except github.GitHubError as e:
        return 500, {"error": str(e)}
    return 200, {"login": login}


def routes() -> list:
    def bearer(request) -> str:
        auth = request.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    async def ask(request):
        who = await asyncio.to_thread(agent_and_owner, bearer(request))
        if not who or "Origin" in request.headers:            # an agent's own client only, never a page
            return web.json_response({"error": "not an agent's sign-in"}, status=403, headers=NO_STORE)
        nonce = challenge(*who)
        if nonce is None:
            return web.json_response({"error": "too many checks; try in an hour"}, status=429, headers=NO_STORE)
        return web.json_response({"nonce": nonce}, headers=NO_STORE)

    async def answer(request):
        who = await asyncio.to_thread(agent_and_owner, bearer(request))
        if not who or "Origin" in request.headers:
            return web.json_response({"error": "not an agent's sign-in"}, status=403, headers=NO_STORE)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        status, out = await prove(*who, body.get("nonce"), body.get("gist"))
        if status == 200:
            log.info("%s proved GitHub @%s for %s (%s)", who[0], out["login"], who[1], client_of(request))
        return web.json_response(out, status=status, headers=NO_STORE)
    return [web.post("/connect/github/challenge", ask), web.post("/connect/github/proof", answer)]
