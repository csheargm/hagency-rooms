"""Versions in "My agents", and the Update button: @pm's side (docs/adr/0020-versions-and-agent-self-update.md).

Each agent on a person's computer reports the code it runs (code_report.py; pm_kind_reports.record_code
keeps it). My agents compares it with this server's (SERVER: what deploy.sh deployed) and shows Up to
date, Update available or Your own version. Update (console.act "selfupdate") is for the agent's owner
only: @pm sends that agent UPDATE_TYPE with the server's commit, and the agent moves itself forward
along the official master (self_update.py), then restarts. Its answer (RESULT_TYPE) and the click are
kept here, in @pm's memory only, for the panel's line under the agent. An agent whose code predates
this reports nothing: the panel offers "Update my agent" (its Connect page, console_agents.fresh_code).
Automatic updates (auto_update.py, ADR 0024) ask through ask() too, with "auto": the agent first waits
until it has no task running, so its answer may take up to code_report.IDLE_WAIT_S longer.
"""
import asyncio
import logging
import time
import uuid

from aiohttp import ClientError
from nio import ToDeviceMessage, ToDeviceResponse

from common import code_report
from common import kind_report
from common import lifecycle
from common import paths
from common import registry
from common import version
from common.bridge_config import SERVER_NAME

SERVER = version.of(paths.ROOT)               # this server's code: the panel's footer and the comparison
CLICK_GAP_S = 60                              # one Update click per agent per minute
WAIT_S = 300                                  # "Updating…" until its answer, or this long
DRAIN_S = lifecycle.DRAIN_TIMEOUT_S + 300     # it said "updated": it may finish turns this long before restarting
SHOW_S = 600                                  # how long a refusal stays under the agent
UPDATING = "Updating: it restarts on the new version once it finishes what it's doing, usually within a minute or two."
NO_ANSWER = "It didn't answer the update. Click Update my agent and run the command it sends on its computer."
OLD_CODE = ("Its code is from before one-click updates. Click Update my agent and run the command it sends on its "
            "computer, once. After that, Update takes one click.")
HERMES = "A Hermes agent is updated with Update my agent: run the command it sends on its computer."
SAYS = {
    "current": "It already runs this version or a newer one.",
    "own": "It runs your own version, which is never replaced. Click Go back to official first, then Update.",
    "not_official": "It can't update itself on that computer. Click Update my agent and run the command it sends there once.",
    "edited": "Its code was changed on that computer, so it wasn't replaced. Click Update my agent to set it up again.",
    "not_newer": "That computer runs a version this server can't update it to.",
    "failed": "The update didn't finish on its computer, so it still runs its old version. Try again in a few minutes.",
    "busy": "It was busy the whole time, so it hasn't updated yet. It updates once it has nothing running.",
}
BY_HAND = ("Product Manager is sending you its Connect page in your private chat with it. Run the command it shows "
           "on the computer where your agent runs. After that, Update takes one click.")   # My agents' Update my agent
FALLBACK = ("not_official", "edited")         # these need the Connect page once (PR #58's "Update my agent")
log = logging.getLogger("loop")


def clicks(loop) -> dict:
    return loop.__dict__.setdefault("_updates", {})   # agent localpart → {"at", "from", "id", "result"}


def is_hermes(a: dict) -> bool:
    """A Hermes agent (named so, or so reported): its own gateways, no self-update, no bridge (also console_github)."""
    return "hermes" in (a.get("agent_kind"), (a.get(registry.REPORTED) or {}).get("agent_kind"))


def status(loop, a: dict) -> dict:
    """What My agents shows about one agent's code: "code" (current, behind, own, unknown or ""),
    "code_version" (its label, in UTC), "code_name" and "code_time" (the label's parts, the time for the
    browser to show in its own zone), "code_says" (the line under it), "code_updating" (no button
    meanwhile), "code_fallback" (offer the Connect page)."""
    if a.get("agent") != registry.REMOTE:
        return {"code": "", "code_version": "", "code_name": "", "code_time": "", "code_says": "", "code_updating": False, "code_fallback": False}
    code = a.get(registry.CODE)
    hermes = is_hermes(a)
    word = ("unknown" if code is None or hermes or SERVER is None else "own" if code["own"]
            else "current" if version.current(code, SERVER) else "behind")
    click = clicks(loop).get(a["localpart"])
    wait = WAIT_S + (code_report.IDLE_WAIT_S if click and click.get("auto") else 0)
    says = ""
    if click and code and code["commit"] != click["from"]:
        clicks(loop).pop(a["localpart"], None)   # it restarted on new code: done
    elif click and time.time() - click["at"] < {None: wait, "updated": DRAIN_S}.get(click["result"], 0):
        says = UPDATING
    elif click and time.time() - click["at"] < SHOW_S + DRAIN_S:
        says = SAYS.get(click["result"]) or NO_ANSWER   # no answer: a click lost while it started, say
    fallback = word == "unknown" or bool(says and (click["result"] in FALLBACK or says == NO_ANSWER))
    if not says and word == "unknown":
        says = HERMES if hermes else OLD_CODE if code is None else ""
    return {"code": word, "code_version": version.label(code), "code_name": version.name(code),
            "code_time": version.iso(code), "code_says": says, "code_updating": says == UPDATING,
            "code_fallback": fallback}


async def request(loop, user: str, agent: str | None, auto: bool = False) -> str:
    """The owner's Update click: "ok" once @pm has asked the agent, else why not (said in the panel).
    `auto`: it waits until it has no task running first (Retry update with Auto-update on: auto_update.py)."""
    found = registry.find(user, agent)
    if found is None or not registry.owns(user, found["localpart"]) or found["agent"] != registry.REMOTE:
        return "that isn't one of your agents on your own computer"
    shown = status(loop, found)
    if shown["code"] != "behind":
        return {"own": SAYS["own"], "current": "It's already up to date."}.get(
            shown["code"], "This agent can't update itself yet: click Update my agent.")
    if loop.agent_online(found["localpart"]) is not True:
        return "It's offline: start its computer, then click Update."
    last = clicks(loop).get(found["localpart"])
    if last and time.time() - last["at"] < CLICK_GAP_S:
        return UPDATING if last["result"] in (None, "updated") else "Wait a minute, then try again."
    return "ok" if await ask(loop, found, auto) else "I couldn't reach your agent just now; please try again."


async def ask(loop, found: dict, auto: bool = False) -> bool:
    """Ask this agent (registry.find's entry, checked by the caller) to update to the server's commit;
    False when it couldn't be reached. The click is kept first: a second one meanwhile waits (GPT review)."""
    click = {"at": time.time(), "from": found[registry.CODE]["commit"], "id": uuid.uuid4().hex, "result": None, "auto": auto}
    clicks(loop)[found["localpart"]] = click
    if await send(loop, f"@{found['localpart']}:{SERVER_NAME}", click["id"], auto):
        return True
    clicks(loop).pop(found["localpart"], None)
    return False


async def send(loop, agent_id: str, ask_id: str, auto: bool = False) -> bool:
    message = ToDeviceMessage(code_report.UPDATE_TYPE, agent_id, "*",
                              {"commit": SERVER["commit"], "id": ask_id, **({"auto": True} if auto else {})})
    try:
        resp = await asyncio.wait_for(loop.client.to_device(message, uuid.uuid4().hex), kind_report.SEND_TIMEOUT_S)
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
    click = clicks(loop).get(agent) if agent else None
    if click is None or click["result"] is not None or result not in code_report.RESULTS or content.get("id") != click["id"]:
        return False                          # nobody asked, already answered, an older click's answer, or not our word
    click["result"] = result
    log.info("%s: update %s", agent, result)
    return True


async def act(loop, user: str, action: str, value: str, agent: str | None) -> str:
    """console.act's "selfupdate" (Update, Retry update) and "autoupdate" (the switch): console_auto_update.py."""
    from server import console_auto_update    # here: it builds on this module
    if action == "autoupdate":
        return await console_auto_update.toggle(loop, user, value)
    return await console_auto_update.request(loop, user, agent)
