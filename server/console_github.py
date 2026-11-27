"""My agents' "Sign in to GitHub": @pm's side (docs/adr/0021-agent-github-signin.md).

Each agent on a person's computer says at its start whether it is signed in to GitHub (agent_github.py;
pm_kind_reports.record_github keeps it as registry.GITHUB). One that isn't gets a Sign in to GitHub
button, for its owner only (console.act "github_signin"). @pm sends that agent github_login.REQUEST_TYPE
with a click id; the agent starts GitHub's device sign-in on its computer and answers with the one-time
code, which @pm shows only to its owner: in the panel's My agents row and in their private chat with @pm.
Then the agent's result (one word of github_login.RESULTS) and its GitHub username. The click is kept
here, in @pm's memory only. An agent whose code has no sign-in (no state report) is offered Update first.
"""
import asyncio
import logging
import time
import uuid

from aiohttp import ClientError
from nio import ToDeviceMessage, ToDeviceResponse

from common import github
from common import github_login
from common import kind_report
from common import registry
from common.bridge_config import SERVER_NAME
from server import console_update

CLICK_GAP_S = 60                  # one click per agent per minute
CODE_WAIT_S = github_login.PM_CODE_WAIT_S   # longer than the agent's worst case for sending gh's code
SIGNIN_S = 16 * 60                # GitHub's codes last 15 minutes; then the agent answers "timeout"
SHOW_S = 600                      # how long an outcome stays under the agent
STARTING = "Starting GitHub's sign-in on its computer…"
ASKED = "Your agent is starting GitHub's sign-in. Its code shows under it in My agents in a moment."
NO_ANSWER = "It didn't answer. Check that it's online, then click Sign in to GitHub again."
OLD_CODE = "Update it to get a Sign in to GitHub button here."   # review: not a must, it may be signed in already
HERMES = "A Hermes agent signs in to GitHub on its own computer, not from here."
NOT_YOURS = "that isn't one of your agents on your own computer"
NOT_SIGNED = "Not signed in to GitHub yet, so it can't open issues or pull requests."
OFFLINE = "Not signed in to GitHub yet. Start it on its computer to get a Sign in to GitHub button here."
SAYS = {
    "signed_in": "Signed in to GitHub.",
    "already": "It was already signed in to GitHub.",
    "failed": "The GitHub sign-in didn't finish on its computer. Click Sign in to GitHub to try again.",
    "timeout": "The code ran out before it was entered. Click Sign in to GitHub for a new one.",
    "no_gh": "Its computer needs the GitHub CLI first (https://cli.github.com). Install it, then click Sign in to GitHub.",
    "too_old_gh": "Its computer's GitHub CLI is too old. Update it (https://cli.github.com), then click Sign in to GitHub.",
}
DM = ("To sign your agent {agent} in to GitHub, open {url} and enter the code **{code}** (it works for 15 minutes). "
      "Sign in there with the GitHub account {agent} should use. Enter it only if you clicked Sign in to GitHub yourself.")
log = logging.getLogger("loop")


def _clicks(loop) -> dict:
    return loop.__dict__.setdefault("_gh_signins", {})   # agent localpart → {"at", "id", "owner", "code", "code_at", "result", "done_at"}


def _phase(loop, agent: str) -> tuple[str, bool, str]:
    """(the line under it, whether a sign-in is under way, the code to show): its latest click."""
    click, now = _clicks(loop).get(agent), time.time()
    if click is None:
        return "", False, ""
    if click["result"] is not None:
        if now - click["done_at"] < SHOW_S:
            return SAYS[click["result"]], False, ""
        _clicks(loop).pop(agent, None)
        return "", False, ""
    if click["code"] is None:
        return (STARTING, True, "") if now - click["at"] < CODE_WAIT_S else (NO_ANSWER, False, "")
    return ("", True, click["code"]) if now - click["code_at"] < SIGNIN_S else (SAYS["timeout"], False, "")


def status(loop, a: dict) -> dict:
    """What My agents shows: "gh" (signed_in, none, no_gh, old, or "" for one the server runs), "gh_login",
    "gh_says" (the line under it), "gh_busy" (no button meanwhile), "gh_code" and "gh_url" (only its owner
    sees My agents), "gh_cli" (the same sign-in as a command, for a terminal on its computer)."""
    if a.get("agent") != registry.REMOTE or console_update.is_hermes(a):   # Hermes: no bridge runs this (review)
        return {"gh": "", "gh_login": "", "gh_says": "", "gh_busy": False, "gh_code": "", "gh_url": "", "gh_cli": ""}
    said = a.get(registry.GITHUB)
    word = "old" if said is None else said["state"]
    says, busy, code = _phase(loop, a["localpart"])
    if not says and word != "signed_in":     # offline: why there's no button (review)
        offline = loop.agent_online(a["localpart"]) is not True
        says = {"old": OLD_CODE, "no_gh": SAYS["no_gh"]}.get(word, OFFLINE if offline else NOT_SIGNED)
    return {"gh": word, "gh_login": (said or {}).get("login") or "", "gh_says": says, "gh_busy": busy, "gh_code": code,
            "gh_url": github_login.DEVICE_PAGE if code else "",
            "gh_cli": "" if word == "signed_in" else github_login.command(a["localpart"])}


async def request(loop, user: str, agent: str | None) -> str | dict:
    """The owner's click: "ok" (with ASKED) once @pm has asked the agent, else why not (said in the panel).
    Also when it says it is signed in ("Sign in again"): its token may have been revoked since it said so, and
    the agent answers "already" only if GitHub still takes it (GPT re-check: the owner was locked out)."""
    found = registry.find(user, agent)
    if found is None or not registry.owns(user, found["localpart"]) or found["agent"] != registry.REMOTE:
        return NOT_YOURS
    lp, said = found["localpart"], found.get(registry.GITHUB)
    if console_update.is_hermes(found):
        return HERMES
    if said is None:
        return OLD_CODE
    if loop.agent_online(lp) is not True:
        return "It's offline: start its computer, then click Sign in to GitHub."
    says, busy, _ = _phase(loop, lp)
    last = _clicks(loop).get(lp)
    if busy:
        return says or "Its code is in your My agents row and your chat with Product Manager."
    if last and time.time() - last["at"] < CLICK_GAP_S:
        return "Wait a minute, then try again."
    click = {"at": time.time(), "id": uuid.uuid4().hex, "owner": user, "code": None, "code_at": None, "result": None, "done_at": None}
    _clicks(loop)[lp] = click                 # before sending: a second click meanwhile waits
    if not await send(loop, f"@{lp}:{SERVER_NAME}", click["id"]):
        _clicks(loop).pop(lp, None)
        return "I couldn't reach your agent just now; please try again."
    return {"result": "ok", "says": ASKED}


async def send(loop, agent_id: str, ask: str) -> bool:
    message = ToDeviceMessage(github_login.REQUEST_TYPE, agent_id, "*", {"id": ask})
    try:
        resp = await asyncio.wait_for(loop.client.to_device(message, uuid.uuid4().hex), kind_report.SEND_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, ClientError) as e:
        resp = e
    if not isinstance(resp, ToDeviceResponse):
        log.warning("couldn't ask %s to sign in to GitHub: %s", agent_id, resp)   # the owner is told in the panel
        return False
    return True


def restarted(loop, agent: str | None, content: dict) -> None:
    """Its state report (pm_kind_reports). One from a start ("startup"; or no "id": an agent from before that
    field) ends a click whose code its old process sent: that sign-in died with it (an update, a crash), so
    the dead code goes and the button comes back (review, 2026-11-26), unless the report names that click.
    A click with no code yet stays: the new process may take it on its next sync (GPT re-check: a click sent
    as the agent started lost its code); if not, it ends as "didn't answer". A report sent after a sign-in
    carries that click's "id" and ends nothing: a newer click stays (GPT review)."""
    if content.get("startup") is not True and "id" in content:
        return
    click = _clicks(loop).get(agent) if agent else None
    if click is not None and click["result"] is None and click["code"] is not None and click["id"] != content.get("id"):
        _clicks(loop).pop(agent, None)


def _login_ok(content: dict) -> bool:
    login = content.get("login")
    return login is None or isinstance(login, str) and bool(github.LOGIN_RE.fullmatch(login))


async def answered(loop, agent: str | None, content: object) -> bool:
    """An agent's (localpart: pm_kind_reports.own_agent) answer to its owner's waiting click: its code (shown
    to the owner), then its result. Its state report follows (pm_kind_reports.record_github)."""
    click = _clicks(loop).get(agent) if agent else None
    if not isinstance(content, dict) or click is None or click["result"] is not None or content.get("id") != click["id"]:
        return False                          # nobody asked, already answered, or an older click's answer
    if "code" in content:
        code = content["code"]
        late = time.time() - click["at"] >= CODE_WAIT_S   # already said "didn't answer": never shown after (GPT review)
        if (set(content) == {"id", "code"} and click["code"] is None and not late and isinstance(code, str)
                and github_login.USER_CODE_RE.fullmatch(code)):
            click["code"], click["code_at"] = code, time.time()
            await tell_owner(loop, click["owner"], agent, code)
            return True
        return False
    result = content.get("result")
    if not set(content) <= {"id", "result", "login"} or result not in github_login.RESULTS or not _login_ok(content):
        return False                          # not our words: ignored, and the panel says it didn't answer
    click["result"], click["done_at"] = result, time.time()
    log.info("%s: GitHub sign-in %s", agent, result)
    return True


async def tell_owner(loop, owner: str, agent: str, code: str) -> None:
    """The code in the owner's private chat with @pm too: a phone may not have the panel open."""
    try:
        dm = await loop.private_room(owner)
        if dm:
            await loop.say(dm, DM.format(agent=agent, url=github_login.DEVICE_PAGE, code=code))
            return
        log.warning("no private chat with %s for the GitHub code", owner)   # the panel still shows it
    except (RuntimeError, OSError, ClientError) as e:   # Matrix refused it: the panel still shows the code
        log.warning("couldn't send %s the GitHub code: %s", owner, e)
