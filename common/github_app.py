"""@pm's own read access to GitHub, as the "Hagency Rooms" GitHub App (docs/GITHUB_APP.md).

On the public server there is no `gh` login for @pm, so its reads (issues, PRs, checks, readiness)
use the App instead: a short JWT signed with the App's private key finds the repo's installation,
and that gives a short-lived installation token scoped to that one repo. Tokens are cached until
shortly before they expire. Unconfigured (no GITHUB_APP_ID), @pm keeps using the host's `gh`
(github.run_json), so local dev is unchanged. Agents' own GitHub identities never come here.

Never logged or put in an error: the private key, the JWT, an installation token.
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import jwt   # PyJWT[crypto] (requirements.txt): RS256 with `cryptography`

from common import github
from common.redact import secret_safe

APP_ID = os.environ.get("GITHUB_APP_ID", "").strip()
KEY_PATH = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
SLUG = os.environ.get("GITHUB_APP_SLUG", "").strip() or "hagency-rooms"
API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")   # tests: a local fake
JWT_BACKDATE_S = 60          # GitHub allows for clock drift this way
JWT_TTL_S = 9 * 60           # GitHub's limit is 10 minutes
TOKEN_MARGIN_S = 5 * 60      # a cached installation token is renewed this long before it expires
HTTP_TIMEOUT_S = 20
ERROR_CHARS = 160
REST_ACCEPT = "application/vnd.github+json"
GRAPHQL_ACCEPT = "application/vnd.github.merge-info-preview+json"   # PR mergeStateStatus, as gh asks for it
# Anything shaped like a credential is cut from GitHub's error text before it reaches a room or a log.
_tokens: dict[str, tuple[str, float]] = {}       # repo (lower case) -> (installation token, expires at)


class NotInstalled(github.GitHubError):
    """The App isn't installed on this repo: its owner installs it with one click (install_url)."""


def configured() -> bool:
    return bool(APP_ID)


def install_url() -> str:
    return f"https://github.com/apps/{urllib.parse.quote(SLUG)}/installations/new"


def private_key() -> str:
    """The App's private key, from a file only its owner can read (never logged, never in errors)."""
    path = Path(KEY_PATH)
    try:
        if not KEY_PATH or path.stat().st_mode & 0o077:
            raise github.GitHubError("the GitHub App's private key must be a file only @pm can read "
                                     "(GITHUB_APP_PRIVATE_KEY_PATH, mode 600)")
        return path.read_text()
    except OSError as e:          # the path is the operator's own; the key's text never gets here
        raise github.GitHubError(f"can't read the GitHub App's private key ({type(e).__name__})") from None


def app_jwt(now: float | None = None) -> str:
    """The App's own short-lived identity: only for finding installations and minting tokens."""
    now = int(time.time() if now is None else now)
    claims = {"iat": now - JWT_BACKDATE_S, "exp": now + JWT_TTL_S, "iss": APP_ID}
    try:
        return jwt.encode(claims, private_key(), algorithm="RS256")
    except (ValueError, TypeError, jwt.PyJWTError) as e:   # a malformed key: say so, never show it
        raise github.GitHubError(f"the GitHub App's private key isn't a valid RSA key ({type(e).__name__})") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None               # never carry a token to wherever a redirect points


_opener = urllib.request.build_opener(_NoRedirect)


def scrub(text: object, *secrets: str) -> str:
    """GitHub's message text with the credentials we sent, and anything shaped like one, cut out."""
    return secret_safe(text, *secrets)[:ERROR_CHARS]


def _request(method: str, path: str, auth: str, body: dict | None = None) -> tuple[int, object]:
    headers = {"Authorization": f"Bearer {auth}", "User-Agent": "hagency-rooms-pm",
               "Accept": GRAPHQL_ACCEPT if path == "/graphql" else REST_ACCEPT, "X-GitHub-Api-Version": "2022-11-28"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", method=method, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT_S) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("message", "")
        except (ValueError, AttributeError):
            detail = ""           # not GitHub's JSON error: the status code says enough
        return e.code, {"message": scrub(detail, auth)}
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise github.GitHubError(f"GitHub didn't answer {method} {path.split('?')[0]} ({type(e).__name__})") from None


async def call(method: str, path: str, auth: str, body: dict | None = None) -> tuple[int, object]:
    return await asyncio.to_thread(_request, method, path, auth, body)


def _fail(status: int, data: object, what: str) -> github.GitHubError:
    msg = data.get("message", "") if isinstance(data, dict) else ""
    return github.GitHubError(f"GitHub answered {status} for {what}" + (f": {msg}" if msg else ""))


def _check_repo(repo: str) -> str:
    if not isinstance(repo, str) or not github.REPO_RE.match(repo):
        raise github.GitHubError("not a repo name (owner/name)")
    return repo


async def installation_id(repo: str) -> int:
    """The App's installation on `repo`; NotInstalled if its owner hasn't installed it there."""
    status, data = await call("GET", f"/repos/{_check_repo(repo)}/installation", app_jwt())
    if status == 404:
        raise NotInstalled(f"Hagency Rooms isn't installed on {repo}: {install_url()}")
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise _fail(status, data, f"the App's installation on {repo}")
    return data["id"]


def _expiry(data: dict) -> float:
    try:
        return datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return time.time() + TOKEN_MARGIN_S + 60      # GitHub's tokens last an hour; assume a short one


async def token(repo: str) -> str:
    """An installation token for `repo` alone (cached until TOKEN_MARGIN_S before it expires)."""
    key = _check_repo(repo).lower()
    cached = _tokens.get(key)
    if cached and cached[1] - TOKEN_MARGIN_S > time.time():
        return cached[0]
    inst = await installation_id(repo)
    status, data = await call("POST", f"/app/installations/{inst}/access_tokens", app_jwt(),
                              {"repositories": [repo.split("/", 1)[1]]})
    if status != 201 or not isinstance(data, dict) or not isinstance(data.get("token"), str):
        raise _fail(status, data, f"a token for {repo}")
    _tokens[key] = (data["token"], _expiry(data))
    return data["token"]


async def as_app(repo: str, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """One call with `repo`'s installation token; a token GitHub refuses (401) is dropped and the
    call made once more with a new one (an uninstalled-then-reinstalled App, a revoked token)."""
    status, data = await call(method, path, await token(repo), body)
    if status == 401:
        _tokens.pop(repo.lower(), None)
        status, data = await call(method, path, await token(repo), body)
        if status == 401:
            _tokens.pop(repo.lower(), None)       # refused twice: never reuse either token
    return status, data


async def get(repo: str, path: str) -> object:
    """GET a GitHub REST path (starting with /) as the App on `repo`."""
    status, data = await as_app(repo, "GET", path)
    if status != 200:
        raise _fail(status, data, path.split("?")[0])
    return data


async def graphql(repo: str, query: str, variables: dict) -> dict:
    status, data = await as_app(repo, "POST", "/graphql", {"query": query, "variables": variables})
    if status != 200 or not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
        errors = data.get("errors") if isinstance(data, dict) else None
        first = errors[0].get("message", "") if isinstance(errors, list) and errors and isinstance(errors[0], dict) else ""
        raise _fail(status, {"message": scrub(first, *(t for t, _ in _tokens.values()))}, "a GraphQL read")
    return data["data"]


async def installed(repo: str) -> bool:
    """@pm can read `repo`: True when the App is installed there now (looked up afresh, never from
    the token cache) and gives a token for it (not suspended, the repo selected), or when @pm uses
    the host's gh."""
    if not configured():
        return True
    _tokens.pop(repo.lower(), None)
    try:
        await token(repo)
    except NotInstalled:
        return False
    return True


async def permission(repo: str, login: str, github_id: int | None = None) -> str:
    """A GitHub user's access to `repo` (admin / write / read / none), read as the App. With
    `github_id` (github_link.py: the account a person proved is theirs), the user GitHub reports must
    have that id, else "none": a username renamed and taken by someone else never passes."""
    if not github.LOGIN_RE.match(login or ""):
        raise github.GitHubError("not a GitHub username")
    status, data = await as_app(_check_repo(repo), "GET", f"/repos/{repo}/collaborators/{login}/permission")
    if status == 404:
        return "none"
    if status != 200 or not isinstance(data, dict):
        raise _fail(status, data, f"{login}'s access to {repo}")
    user = data.get("user")
    if github_id is not None and (not isinstance(user, dict) or user.get("id") != github_id):
        return "none"
    return str(data.get("permission") or "none")


async def full_name(repo: str) -> str:
    """GitHub's own spelling of `repo` (owner/Name as created), so one repo is one key in every record
    when no operator list spells it (GPT review, batch 12–14); `repo` itself when GitHub can't say."""
    try:
        data = await get(repo, f"/repos/{repo}")
    except github.GitHubError:
        return repo
    name = data.get("full_name") if isinstance(data, dict) else None
    return name if isinstance(name, str) and github.REPO_RE.match(name) and name.lower() == repo.lower() else repo
