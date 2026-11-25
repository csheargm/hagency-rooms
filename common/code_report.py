"""An agent tells @pm which code it runs, so My agents can say "Up to date" or "Update available"
(docs/adr/0020-versions-and-agent-self-update.md).

Like the AI report (kind_report.py), and next to it: each time the agent adapter signs in, one Matrix
to-device message of type EVENT_TYPE to @pm: {"commit": its 40-hex commit, "time": that commit's
Unix time as text, "own": "yes" when it runs its owner's own version ("Use it myself", customize.py)}.
A message of its own, so a server that predates it simply ignores it. Synapse sets the sender: only
the agent's own sign-in can report for it (server/pm_kind_reports.py records it).
"""
import asyncio
import logging
import uuid

from aiohttp import ClientError
from nio import AsyncClient, LocalProtocolError, ToDeviceMessage, ToDeviceResponse

from common import version

EVENT_TYPE = "xyz.hagencylabs.agent.code"
UPDATE_TYPE = "xyz.hagencylabs.agent.update"          # @pm → agent: {"commit"} (an owner's Update click)
RESULT_TYPE = "xyz.hagencylabs.agent.update.result"   # agent → @pm: {"result": one of RESULTS}
RESULTS = ("updated", "current", "own", "not_official", "edited", "not_newer", "failed")   # self_update.py
KEYS = {"commit", "time", "own"}
SEND_TIMEOUT_S = 15
log = logging.getLogger("bridge")


def build(running: dict | None, own: bool) -> dict | None:
    """The report for the code this process runs (version.of), or None when that isn't known."""
    if running is None:
        return None
    return {"commit": running["commit"], "time": str(running["time"]), **({"own": "yes"} if own else {})}


def valid(content: object) -> dict | None:
    """What @pm keeps: {"commit", "time": int, "own": bool}, or None for anything else (an extra field,
    a short or non-hex commit, a time that isn't whole seconds)."""
    if not isinstance(content, dict) or not set(content) <= KEYS or content.get("own", "yes") != "yes":
        return None
    parsed = version.parse(content.get("commit"), content.get("time")) if isinstance(content.get("time"), str) else None
    return {**parsed, "own": "own" in content} if parsed else None


def stored(entry: object) -> dict | None:
    """A report as agents.json keeps it (valid()'s shape), re-checked: a damaged record is none."""
    if not isinstance(entry, dict) or set(entry) != {"commit", "time", "own"} or not isinstance(entry["own"], bool):
        return None
    parsed = version.parse(entry["commit"], entry["time"]) if type(entry["time"]) is int else None
    return {**parsed, "own": entry["own"]} if parsed else None


async def send(client: AsyncClient, pm_id: str, report: dict | None) -> bool:
    """Tell @pm which code this agent runs; False when there is nothing to say or it didn't go out."""
    if report is None:
        return False
    try:
        resp = await asyncio.wait_for(client.to_device(ToDeviceMessage(EVENT_TYPE, pm_id, "*", report), uuid.uuid4().hex),
                                      SEND_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, ClientError, LocalProtocolError) as e:   # LocalProtocolError: not signed in
        resp = e
    if isinstance(resp, ToDeviceResponse):
        return True
    # Not fatal on purpose: the agent works without it; My agents says "no version reported" until the next start.
    log.warning("couldn't tell @pm which code this agent runs: %s", resp)
    return False
