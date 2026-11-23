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
PER_CLIENT_PER_HOUR, ALL_PER_HOUR = 30, 300   # asks of both routes, before any homeserver call (GPT review)
CHECK_FILE = github.GIST_CHECK_FILE
GIST_RE = re.compile(r"[0-9a-f]{20,40}")   # fullmatch only
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
DIFFERENT = ("You're connected to GitHub as @{linked}, but your agent signed in as @{login}. To use @{login}: "
             "Disconnect GitHub in the Hagency panel, then Connect GitHub.")
TAKEN = "That GitHub account is already someone else's here, so I didn't link it to you."
_pending: dict[str, tuple[str, str, float]] = {}   # nonce → (agent localpart, owner, issued)
_asked: dict[str, list[float]] = {}           # agent, "client:<address>" or "*" → recent asks


def _allowed(key: str, per_hour: int, now: float) -> bool:
    recent = _asked[key] = [t for t in _asked.get(key, []) if now - t < 3600]
    if len(recent) >= per_hour:
        return False
    recent.append(now)
    return True


class Busy(github.GitHubError):
    """GitHub couldn't answer now: the check stays usable (it isn't the client's fault)."""


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
    if not _allowed(agent, PER_AGENT_PER_HOUR, now):
        return None
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
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):                  # GitHub's limit for reads without a sign-in (60 an hour here)
            raise Busy("GitHub is busy right now; run the connect line again in an hour") from None
        raise github.GitHubError(f"GitHub couldn't show the check gist (HTTP {e.code})") from None
    except (OSError, ValueError) as e:
        raise Busy(f"GitHub couldn't be reached ({type(e).__name__}); try again soon") from None
    if not isinstance(data, dict):
        raise github.GitHubError("GitHub's answer for the check gist wasn't an object")
    return data


def owner_if_proved(data: dict, nonce: str) -> tuple[str, int] | None:
    """(login, id) of the gist's owner when its one, whole file is exactly this nonce and it is no fork."""
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    only = [files.get(CHECK_FILE)] if list(files) == [CHECK_FILE] else []   # the one check file, by name
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    login, gid = owner.get("login"), owner.get("id")
    if len(only) != 1 or not isinstance(only[0], dict) or only[0].get("truncated") or data.get("truncated") \
            or only[0].get("content") != nonce or data.get("fork_of") or not isinstance(login, str) \
            or not github.LOGIN_RE.match(login) or not isinstance(gid, int) or isinstance(gid, bool):
        return None
    return login, gid


async def prove(agent: str, owner: str, nonce: object, gist: object) -> tuple[int, dict]:
    """Check the gist and link its owner's account to the agent's owner. (HTTP status, answer)."""
    pending = _pending.get(nonce) if isinstance(nonce, str) else None
    if not pending or pending[:2] != (agent, owner) or time.time() - pending[2] > NONCE_S:
        return 400, {"error": "that check expired or isn't yours: run the connect line again"}   # not used up
    if not isinstance(gist, str) or not GIST_RE.fullmatch(gist):
        return 400, {"error": "not a gist id"}
    del _pending[nonce]                           # once, from here on
    try:
        found = owner_if_proved(await asyncio.to_thread(read_gist, gist), nonce)
    except Busy as e:
        _pending[nonce] = pending                 # GitHub's fault, not the client's: the same check again
        return 503, {"error": str(e)}
    except github.GitHubError as e:
        return 502, {"error": str(e)}
    if not found:
        return 400, {"error": "the gist didn't hold this check"}
    login, gid = found
    if registry.owner_of(agent) != owner:         # the agent changed hands while GitHub answered (GPT review)
        return 409, {"error": "this agent isn't yours any more"}
    if any(u != owner and isinstance(e, dict) and e.get("id") == gid for u, e in github_link.links().items()):
        return 409, {"error": TAKEN}              # one account, one person (review workflow: a misled agent)
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

    def refused(request):
        """Before any homeserver call: never a page (Origin), and a few asks per address and in all."""
        now = time.time()
        if "Origin" in request.headers:
            return web.json_response({"error": "not an agent's sign-in"}, status=403, headers=NO_STORE)
        if not (_allowed(f"client:{client_of(request)}", PER_CLIENT_PER_HOUR, now) and _allowed("*", ALL_PER_HOUR, now)):
            return web.json_response({"error": "too many checks; try in an hour"}, status=429, headers=NO_STORE)
        return None

    async def ask(request):
        if (no := refused(request)) is not None:
            return no
        who = await asyncio.to_thread(agent_and_owner, bearer(request))
        if not who:                                            # an agent's own client only
            return web.json_response({"error": "not an agent's sign-in"}, status=403, headers=NO_STORE)
        nonce = challenge(*who)
        if nonce is None:
            return web.json_response({"error": "too many checks; try in an hour"}, status=429, headers=NO_STORE)
        return web.json_response({"nonce": nonce}, headers=NO_STORE)

    async def answer(request):
        if (no := refused(request)) is not None:
            return no
        who = await asyncio.to_thread(agent_and_owner, bearer(request))
        if not who:
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
