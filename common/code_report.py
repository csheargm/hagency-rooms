"""An agent tells @pm which code it runs, so My agents can say "Up to date" or "Update available"
(docs/adr/0020-versions-and-agent-self-update.md).

Like the AI report (kind_report.py), and next to it: each time the agent adapter signs in, one Matrix
to-device message of type EVENT_TYPE to @pm: {"commit": its 40-hex commit, "time": that commit's
Unix time as text, "number": its release number when it has one (version.NUMBER_FILE), "own": "yes" when it runs its owner's own version ("Use it myself", customize.py),
"waits": "yes" from code that waits until no task runs before an automatic update (ADR 0024)}.
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
UPDATE_TYPE = "xyz.hagencylabs.agent.update"          # @pm → agent: {"commit", "id", "auto"?} (a click, or automatic)
RESULT_TYPE = "xyz.hagencylabs.agent.update.result"   # agent → @pm: {"result": one of RESULTS}
RESULTS = ("updated", "current", "own", "not_official", "edited", "not_newer", "failed",   # self_update.py
           "busy")                    # automatic only: a task ran the whole wait (agent_update.py, ADR 0024)
WAITS = "waits"                       # its code waits until no task runs before an automatic update (ADR 0024)
KEYS = {"commit", "time", "own", version.NUMBER_KEY, WAITS}
IDLE_WAIT_S = 30 * 60                 # an automatic update waits this long for no task running, then says "busy"
UPDATE_MAX_S = 35 * 60                # self_update.apply at most: its lock wait, pip and git at their timeouts (a test checks)
UNKNOWN_KIND = "unknown task kind"    # an agent's answer to a task its code predates (tasks.py; pm_cards, pm_solo_rules read it)
# A task kind newer than some agents: the commit time from which master's agents run it (live test 2026-11-27:
# an agent on older code got solo_ruleset and posted "unknown task kind"). 1795804541: 0afcfbb, solo review merged.
TASK_SINCE = {"solo_ruleset": 1795804541}
NONE = object()                               # "number" left out (older agents), unlike a present null (GPT review)
log = logging.getLogger("bridge")


def build(running: dict | None, own: bool) -> dict | None:
    """The report for the code this process runs (version.of), or None when that isn't known."""
    if running is None:
        return None
    return {"commit": running["commit"], "time": str(running["time"]), **({"own": "yes"} if own else {}), WAITS: "yes",
            **({version.NUMBER_KEY: running[version.NUMBER_KEY]} if running.get(version.NUMBER_KEY) else {})}


def valid(content: object) -> dict | None:
    """What @pm keeps: {"commit", "time": int, "own": bool}, "number" when given and "waits": True when said,
    or None for anything else (an extra field, a short or non-hex commit, a time that isn't whole seconds, a bad number)."""
    if (not isinstance(content, dict) or not set(content) <= KEYS or content.get("own", "yes") != "yes"
            or content.get(WAITS, "yes") != "yes"):
        return None
    return _checked(content.get("commit"), content.get("time") if isinstance(content.get("time"), str) else None,
                    "own" in content, content.get(version.NUMBER_KEY, NONE), WAITS in content)


def _checked(commit: object, when: object, own: bool, number: object, waits: bool = False) -> dict | None:
    parsed = version.parse(commit, when) if when is not None else None
    if parsed is None or not (number is NONE or isinstance(number, str) and version.NUMBER_RE.fullmatch(number)):
        return None
    return {**parsed, "own": own, **({} if number is NONE else {version.NUMBER_KEY: number}), **({WAITS: True} if waits else {})}


def stored(entry: object) -> dict | None:
    """A report as agents.json keeps it (valid()'s shape), re-checked: a damaged record is none."""
    if (not isinstance(entry, dict) or not {"commit", "time", "own"} <= set(entry) <= KEYS or not isinstance(entry["own"], bool)
            or entry.get(WAITS, True) is not True):
        return None
    return _checked(entry["commit"], entry["time"] if type(entry["time"]) is int else None, entry["own"],
                    entry.get(version.NUMBER_KEY, NONE), WAITS in entry)


def runs_task(code: dict | None, kind: str) -> bool:
    """An agent whose report is `code` (registry.CODE) runs task `kind`: no report predates them all."""
    return kind not in TASK_SINCE or bool(code) and code["time"] >= TASK_SINCE[kind]


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
