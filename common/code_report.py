"""An agent tells @pm which code it runs, so My agents can say "Up to date" or "Update available"
(docs/adr/0020-versions-and-agent-self-update.md).

Like the AI report (kind_report.py), and next to it: each time the agent adapter signs in, one Matrix
to-device message of type EVENT_TYPE to @pm: {"commit": its 40-hex commit, "time": that commit's
Unix time as text, "number": its release number when it has one (version.NUMBER_FILE), "own": "yes" when it runs its owner's own version ("Use it myself", customize.py)}.
A message of its own, so a server that predates it simply ignores it. Synapse sets the sender: only
the agent's own sign-in can report for it (server/pm_kind_reports.py records it).
"""
import asyncio
import logging
import uuid

from aiohttp import ClientError
from nio import AsyncClient, LocalProtocolError, ToDeviceMessage, ToDeviceResponse

from common import kind_report
from common import version

EVENT_TYPE = "xyz.hagencylabs.agent.code"
UPDATE_TYPE = "xyz.hagencylabs.agent.update"          # @pm → agent: {"commit"} (an owner's Update click)
RESULT_TYPE = "xyz.hagencylabs.agent.update.result"   # agent → @pm: {"result": one of RESULTS}
RESULTS = ("updated", "current", "own", "not_official", "edited", "not_newer", "failed")   # self_update.py
KEYS = {"commit", "time", "own", "number"}
log = logging.getLogger("bridge")


def build(running: dict | None, own: bool) -> dict | None:
    """The report for the code this process runs (version.of), or None when that isn't known."""
    if running is None:
        return None
    return {"commit": running["commit"], "time": str(running["time"]), **({"own": "yes"} if own else {}),
            **({"number": running["number"]} if running.get("number") else {})}


def valid(content: object) -> dict | None:
    """What @pm keeps: {"commit", "time": int, "own": bool} and "number" when given, or None for anything
    else (an extra field, a short or non-hex commit, a time that isn't whole seconds, a bad number)."""
    if not isinstance(content, dict) or not set(content) <= KEYS or content.get("own", "yes") != "yes":
        return None
    return _checked(content.get("commit"), content.get("time") if isinstance(content.get("time"), str) else None,
                    "own" in content, content.get("number", NONE))


NONE = object()                               # "number" left out (older agents), unlike a present null (GPT review)


def _checked(commit: object, when: object, own: bool, number: object) -> dict | None:
    parsed = version.parse(commit, when) if when is not None else None
    if parsed is None or not (number is NONE or isinstance(number, str) and version.NUMBER_RE.fullmatch(number)):
        return None
    return {**parsed, "own": own, **({} if number is NONE else {"number": number})}


def stored(entry: object) -> dict | None:
    """A report as agents.json keeps it (valid()'s shape), re-checked: a damaged record is none."""
    if not isinstance(entry, dict) or not {"commit", "time", "own"} <= set(entry) <= KEYS or not isinstance(entry["own"], bool):
        return None
    return _checked(entry["commit"], entry["time"] if type(entry["time"]) is int else None, entry["own"], entry.get("number", NONE))


async def send(client: AsyncClient, pm_id: str, report: dict | None) -> bool:
    """Tell @pm which code this agent runs; False when there is nothing to say or it didn't go out."""
    if report is None:
        return False
    try:
        resp = await asyncio.wait_for(client.to_device(ToDeviceMessage(EVENT_TYPE, pm_id, "*", report), uuid.uuid4().hex),
                                      kind_report.SEND_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError, ClientError, LocalProtocolError) as e:   # LocalProtocolError: not signed in
        resp = e
    if isinstance(resp, ToDeviceResponse):
        return True
    # Not fatal on purpose: the agent works without it; My agents says "no version reported" until the next start.
    log.warning("couldn't tell @pm which code this agent runs: %s", resp)
    return False
