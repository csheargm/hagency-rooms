"""An agent tells @pm which AI it runs, so "My agents" can show it (docs/adr/0008-agent-kinds.md).

Each time the agent adapter (bridge.py) signs in on a person's computer, it sends @pm one Matrix
to-device message of type EVENT_TYPE with what its credentials.json says it runs: {"agent_kind",
"model"?, "host"?} (pm_kinds.REPORT_KEYS; a remote service's host only, never its whole address).
Never an API key: only these fields are read from the file, and @pm refuses a report that has any
other field, a key-like value, or a value agent_kinds' rules refuse (pm_kinds.reported;
server/pm_kind_reports.py records it).
Why to-device: Synapse sets the sender, so only the agent's own sign-in can report for it. It is
private (presence's status_msg is seen by everyone sharing a room), and it waits for @pm's fixed
device (PM_BOT) while @pm restarts. No console address is needed (a credentials file has none).
"""
import asyncio
import logging
import urllib.parse
import uuid

from aiohttp import ClientError
from nio import AsyncClient, ToDeviceMessage, ToDeviceResponse

from common import agent_kinds
from common import pm_kinds

EVENT_TYPE = "xyz.hagencylabs.agent.kind"
SEND_TIMEOUT_S = 15                 # every to-device send of an agent or @pm (code_report, agent_update, console_update)
log = logging.getLogger("bridge")


def from_credentials(creds: dict) -> dict | None:
    """The report for this computer's credentials.json: its "agent", plus "model" for ollama / remote and
    for remote only the service's host (never its whole address: a path may hold a token; GPT review).
    None when the file names no kind or a value @pm would refuse. Nothing else is ever read from it."""
    kind = creds.get("agent")
    if kind not in agent_kinds.KINDS:
        return None
    report = {"agent_kind": kind}
    if kind in agent_kinds.MODEL_KINDS and creds.get("model"):
        report["model"] = creds["model"]
    if kind == "remote" and agent_kinds.valid_base_url(creds.get("base_url")):
        report["host"] = urllib.parse.urlsplit(creds["base_url"]).hostname.removesuffix(".")   # one root dot: "x.example." is x.example
    return report if pm_kinds.reported(report) is not None else None


async def send(client: AsyncClient, pm_id: str, creds: dict) -> bool:
    """Tell @pm (pm_id) what this agent runs; False when there is nothing to say or it didn't go out."""
    report = from_credentials(creds)
    if report is None:
        return False
    try:
        resp = await asyncio.wait_for(client.to_device(ToDeviceMessage(EVENT_TYPE, pm_id, "*", report), uuid.uuid4().hex),
                                      SEND_TIMEOUT_S)   # nio retries a 429 or timeout without end: never hold up the start
    except (OSError, asyncio.TimeoutError, ClientError) as e:
        resp = e
    if isinstance(resp, ToDeviceResponse):
        return True
    # Not fatal on purpose: the agent works without it; My agents keeps "not reported yet" until the next start.
    log.warning("couldn't tell @pm which AI this agent runs: %s", resp)
    return False
