"""Versions in "My agents", and the Update button: @pm's side (docs/adr/0020-versions-and-agent-self-update.md).

Each agent on a person's computer reports the code it runs (code_report.py; pm_kind_reports.record_code
keeps it). My agents compares it with this server's (SERVER: what deploy.sh deployed) and shows Up to
date, Update available or Your own version. Update (console.act "selfupdate") is for the agent's owner
only: @pm sends that agent UPDATE_TYPE with the server's commit, and the agent moves itself forward
along the official master (self_update.py), then restarts. Its answer (RESULT_TYPE) and the click are
kept here, in @pm's memory only, for the panel's line under the agent. An agent whose code predates
this reports nothing: the panel offers "Update my agent" (its Connect page, console_agents.fresh_code).
"""
import asyncio
import logging
import time
import uuid

from aiohttp import ClientError
from nio import ToDeviceMessage, ToDeviceResponse

from common import code_report
from common import paths
from common import registry
from common import version
from common.bridge_config import SERVER_NAME

SERVER = version.of(paths.ROOT)               # this server's code: the panel's footer and the comparison
CLICK_GAP_S = 60                              # one Update click per agent per minute
WAIT_S = 300                                  # "Updating…" until its new report, or this long
SHOW_S = 600                                  # how long a refusal stays under the agent
SEND_TIMEOUT_S = 15
UPDATING = "Updating: it restarts on the new version in about a minute."
SAYS = {
    "current": "It already runs this version or a newer one.",
    "own": "It runs your own version, which is never replaced. Click Go back to official first, then Update.",
    "not_official": "It can't update itself on that computer. Click Update my agent and run the command it sends there once.",
    "edited": "Its code was changed on that computer, so it wasn't replaced. Click Update my agent to set it up again.",
    "not_newer": "That computer runs a version this server can't update it to.",
    "failed": "The update didn't finish on its computer, so it still runs its old version. Try again in a few minutes.",
}
BY_HAND = ("Product Manager is sending you its Connect page in your private chat with it. Run the command it shows "
           "on the computer where your agent runs. After that, Update takes one click.")   # My agents' Update my agent
FALLBACK = ("not_official", "edited")         # these need the Connect page once (PR #58's "Update my agent")
log = logging.getLogger("loop")


def _clicks(loop) -> dict:
    return loop.__dict__.setdefault("_updates", {})   # agent localpart → {"at", "from", "result"}


def status(loop, a: dict) -> dict:
    """What My agents shows about one agent's code: "code" (current, behind, own, unknown or ""),
    "code_version" (its label), "code_says" (the line under it), "code_updating" (no button
    meanwhile), "code_fallback" (offer the Connect page)."""
    if a.get("agent") != registry.REMOTE:
        return {"code": "", "code_version": "", "code_says": "", "code_updating": False, "code_fallback": False}
    code = a.get(registry.CODE)
    hermes = "hermes" in (a.get("agent_kind"), (a.get(registry.REPORTED) or {}).get("agent_kind"))
    word = ("unknown" if code is None or hermes or SERVER is None else "own" if code["own"]
            else "current" if version.current(code, SERVER) else "behind")
    click = _clicks(loop).get(a["localpart"])
    says = ""
    if click and code and code["commit"] != click["from"]:
        _clicks(loop).pop(a["localpart"], None)   # it restarted on new code: done
    elif click and click["result"] in (None, "updated") and time.time() - click["at"] < WAIT_S:
        says = UPDATING
    elif click and click["result"] in SAYS and time.time() - click["at"] < SHOW_S:
        says = SAYS[click["result"]]
    return {"code": word, "code_version": version.label(code), "code_says": says, "code_updating": says == UPDATING,
            "code_fallback": word == "unknown" or bool(click and click["result"] in FALLBACK)}


async def request(loop, user: str, agent: str | None) -> str:
    """The owner's Update click: "ok" once @pm has asked the agent, else why not (said in the panel)."""
    found = registry.find(user, agent)
    if found is None or not registry.owns(user, found["localpart"]) or found["agent"] != registry.REMOTE:
        return "that isn't one of your agents on your own computer"
    shown = status(loop, found)
    if shown["code"] != "behind":
        return {"own": SAYS["own"], "current": "It's already up to date."}.get(
            shown["code"], "This agent can't update itself yet: click Update my agent.")
    if loop.agent_online(found["localpart"]) is not True:
        return "It's offline: start its computer, then click Update."
    last = _clicks(loop).get(found["localpart"])
    if last and time.time() - last["at"] < CLICK_GAP_S:
        return UPDATING if last["result"] in (None, "updated") else "Wait a minute, then try again."
    if not await send(loop, f"@{found['localpart']}:{SERVER_NAME}"):
        return "I couldn't reach your agent just now; please try again."
    _clicks(loop)[found["localpart"]] = {"at": time.time(), "from": found[registry.CODE]["commit"], "result": None}
    return "ok"


async def send(loop, agent_id: str) -> bool:
    message = ToDeviceMessage(code_report.UPDATE_TYPE, agent_id, "*", {"commit": SERVER["commit"]})
    try:
        resp = await asyncio.wait_for(loop.client.to_device(message, uuid.uuid4().hex), SEND_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, ClientError) as e:
        resp = e
    if not isinstance(resp, ToDeviceResponse):
        log.warning("couldn't ask %s to update: %s", agent_id, resp)   # the owner is told in the panel
        return False
    log.info("asked %s to update to %s", agent_id, SERVER["commit"][:7])
    return True


def answered(loop, agent: str | None, content: object) -> bool:
    """An agent's (localpart: pm_kind_reports.own_agent) answer to an Update click: kept only for a
    click of its owner's still waiting."""
    result = content.get("result") if isinstance(content, dict) else None
    click = _clicks(loop).get(agent) if agent else None
    if click is None or click["result"] is not None or result not in code_report.RESULTS:
        return False                          # nobody asked, already answered, or not a word we show
    click["result"] = result
    log.info("%s: update %s", agent, result)
    return True
