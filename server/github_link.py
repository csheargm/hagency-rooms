"""Connect GitHub: a person proves which GitHub account is theirs (ADR 0007, docs/GITHUB_APP.md).

GitHub's device flow, with the "Hagency Rooms" App's Client ID (public, GITHUB_APP_CLIENT_ID): @pm
asks GitHub for a one-time user code, the person enters it at github.com/login/device and approves,
and @pm's poll gets a user token. That token is used for ONE call (GET /user: who approved), then
dropped: never stored, logged or shown. @pm keeps only {login, id} for the Matrix user. Write access
to a repo is read later as the App (github_app.permission), so no user token is ever needed again.

Pending sign-ins (one per person) live here, in memory: a restart drops them and the person clicks
again. The device code is never shown; only the short user code goes to the person, privately.
"""
import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from common import github
from common import github_app
from common.bridge_config import ROOT
from common.github_login import DEVICE_PAGE, USER_CODE_RE   # one place: an agent's sign-in uses them too
from common.saved_dict import SavedDict

log = logging.getLogger("loop")
CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
WEB = os.environ.get("GITHUB_WEB_URL", "https://github.com").rstrip("/")     # tests: a local fake
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
MAX_FLOW_S = 15 * 60          # GitHub's device codes last 15 minutes; never wait longer
MIN_INTERVAL_S = 5            # GitHub's own minimum between polls
SLOW_DOWN_S = 5               # what GitHub adds to the interval after "slow_down"
WRITE = ("admin", "write")    # GitHub's permission field: maintain counts as write, triage as read
LINKS_FILE = ROOT / "data" / "github-links.json"
FLOWS: dict[str, dict] = {}   # Matrix user → their one pending sign-in (pm_github.py starts and ends them)
_links: SavedDict | None = None


class LinkFailed(github.GitHubError):
    """The sign-in ended without a GitHub account: its text is what the person is told."""


def configured() -> bool:
    """Connect GitHub is on: the App reads GitHub, and its Client ID is set (the public server)."""
    return github_app.configured() and bool(CLIENT_ID)


def links() -> SavedDict:
    """Matrix user (full ID) → {"login", "id"}: the GitHub account they proved is theirs."""
    global _links
    if _links is None:
        _links = SavedDict(LINKS_FILE)
    return _links


def link_of(user: str) -> dict | None:
    entry = links().get(user)
    if isinstance(entry, dict) and github.LOGIN_RE.match(str(entry.get("login", ""))) \
            and isinstance(entry.get("id"), int):
        return entry
    return None                   # nothing (or a damaged entry): not connected, fail closed


def _change(user: str, entry: dict | None) -> bool:
    """Write (or with None, remove) a link: memory and disk agree, or GitHubError and nothing changed."""
    saved = links()
    before = saved.get(user)
    try:
        if entry is not None:
            saved[user] = entry
        elif user in saved:
            saved.pop(user)
    except OSError as e:          # the disk refused: put memory back the way the disk has it
        dict.pop(saved, user, None)
        if before is not None:
            dict.__setitem__(saved, user, before)
        raise github.GitHubError(f"I couldn't save your GitHub link ({type(e).__name__})") from None
    return before is not None


TAKEN = "That GitHub account is already someone else's here, so I didn't link it to you."


def taken(user: str, github_id: int) -> bool:
    """This GitHub account is already linked to someone else here (one account, one person: ADR 0017)."""
    return any(u != user and isinstance(e, dict) and e.get("id") == github_id for u, e in links().items())


def save_link(user: str, login: str, github_id: int) -> None:
    _change(user, {"login": login, "id": github_id, "at": time.time()})


def unlink(user: str) -> bool:
    return _change(user, None)


def forget(user: str) -> None:
    """A deactivated account's link: out of memory at once (every check fails closed from now on),
    then off the disk; GitHubError if the disk refuses (the caller tries again: GPT re-check)."""
    saved = links()
    dict.pop(saved, user, None)
    try:
        saved._save()
    except OSError as e:
        raise github.GitHubError(f"I couldn't save the GitHub links ({type(e).__name__})") from None


def status(user: str) -> dict | None:
    """What the panel shows this person (only them): None when Connect GitHub is off here."""
    if not configured():
        return None
    link, flow = link_of(user), FLOWS.get(user)
    pending = flow if flow and flow["expires_at"] > time.time() else None
    return {"login": link["login"] if link else None,
            "code": pending["user_code"] if pending else None, "url": pending["url"] if pending else None}


def _post(path: str, fields: dict) -> dict:
    """A form POST to github.com's sign-in endpoints; GitHub answers JSON (errors too)."""
    req = urllib.request.Request(f"{WEB}{path}", method="POST", data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Accept": "application/json", "User-Agent": "hagency-rooms-pm",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with github_app._opener.open(req, timeout=github_app.HTTP_TIMEOUT_S) as r:
            data = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}"}            # its body may echo what we sent: never read it
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise github.GitHubError(f"GitHub's sign-in didn't answer ({type(e).__name__})") from None
    return data if isinstance(data, dict) else {"error": "not_json"}


async def post(path: str, fields: dict) -> dict:
    return await asyncio.to_thread(_post, path, fields)


KNOWN_ERRORS = {"authorization_pending", "slow_down", "access_denied", "expired_token", "device_flow_disabled",
                "incorrect_client_credentials", "incorrect_device_code", "unsupported_grant_type"}


def _error(data: dict) -> str:
    """GitHub's documented error code, or "unexpected": its own text never reaches a person or a log."""
    error = data.get("error")
    return "" if error is None else error if isinstance(error, str) and error in KNOWN_ERRORS else "unexpected"


async def start() -> dict:
    """A new device code: {"device_code", "user_code", "url", "expires_at", "interval"}."""
    data = await post("/login/device/code", {"client_id": CLIENT_ID})
    if _error(data) == "device_flow_disabled":
        raise github.GitHubError("the server's owner must tick Enable Device Flow on the GitHub App")
    code, device, ttl, interval = (data.get(k) for k in ("user_code", "device_code", "expires_in", "interval"))
    if not (isinstance(code, str) and USER_CODE_RE.match(code) and isinstance(device, str) and device
            and isinstance(ttl, int) and ttl > 0):
        raise github.GitHubError(f"GitHub didn't start the sign-in ({_error(data) or 'unexpected answer'})")
    return {"device_code": device, "user_code": code, "url": DEVICE_PAGE,   # never a page GitHub's answer names
            "expires_at": time.time() + min(ttl, MAX_FLOW_S),
            "interval": max(interval if isinstance(interval, int) else 0, MIN_INTERVAL_S)}


async def who(user_token: str) -> tuple[str, int]:
    """The GitHub account that approved: (login, id), from GET /user with its token."""
    status_code, data = await github_app.call("GET", "/user", user_token)
    login = data.get("login") if isinstance(data, dict) else None
    gid = data.get("id") if isinstance(data, dict) else None
    if status_code != 200 or not isinstance(login, str) or not github.LOGIN_RE.match(login) or not isinstance(gid, int):
        raise github.GitHubError(f"GitHub didn't say who signed in ({status_code})")
    return login, gid


async def wait_for_login(flow: dict, sleep=asyncio.sleep) -> tuple[str, int]:
    """Poll until the person approves (→ their login and id), declines, or the code expires."""
    fields = {"client_id": CLIENT_ID, "device_code": flow["device_code"], "grant_type": DEVICE_GRANT}
    while True:
        await sleep(flow["interval"])
        if time.time() >= flow["expires_at"]:
            raise LinkFailed("The GitHub code expired before it was entered. Click Connect GitHub to get a new one.")
        data = await post("/login/oauth/access_token", fields)
        token, error = data.get("access_token"), _error(data)
        if isinstance(token, str) and token:
            return await who(token)       # its one use: the token goes no further than this
        if error == "authorization_pending":
            continue
        if error == "slow_down":            # GitHub's new interval when it names one, else 5 s more
            told = data.get("interval")
            flow["interval"] = max(flow["interval"] + SLOW_DOWN_S, told if isinstance(told, int) else 0)
            continue
        if error == "access_denied":
            raise LinkFailed("You declined on GitHub, so nothing was connected.")
        if error == "expired_token":
            raise LinkFailed("The GitHub code expired before it was entered. Click Connect GitHub to get a new one.")
        raise LinkFailed(f"GitHub ended the sign-in ({error or 'no reason given'}). Click Connect GitHub to try again.")


async def may_write(user: str, repo: str) -> bool:
    """`user` connected a GitHub account that can push to `repo` (write, maintain or admin), read as
    the App (NotInstalled when it isn't on `repo` yet). The account GitHub reports must still be the
    one they proved (by id): a renamed and re-registered username never passes for someone else. A
    Disconnect (or a new account) while GitHub answers counts: the link must be unchanged after."""
    link = link_of(user)
    if link is None:
        return False
    try:
        can = await github_app.permission(repo, link["login"], link["id"]) in WRITE
        return can and link_of(user) == link
    except github_app.NotInstalled:
        raise                     # the caller offers the install link (pm_settings.choose_repo)
    except github.GitHubError as e:
        # Fail closed on purpose: the caller's one answer ("can't use this repo") never confirms a
        # repo name, and its text tells them what to check. The reason is kept for the operator.
        log.warning("write check for %s on %s: %s", user, repo, str(e)[:160])
        return False
