"""An agent tells @pm which AI it runs, so "My agents" can show it (docs/adr/0008-agent-kinds.md).

Each time the agent adapter (bridge.py) signs in on a person's computer, it sends @pm one Matrix
to-device message of type EVENT_TYPE with what its credentials.json says it runs: {"agent_kind",
"model"?, "base_url"?}, agent_kinds' connect-info names. Never an API key: only these fields are read
from the file, and @pm refuses a report that has any other field, a key-like value, or a value
agent_kinds' rules refuse (server/pm_kind_reports.py records it).
Why to-device: Synapse sets the sender, so only the agent's own sign-in can report for it. It is
private (presence's status_msg is seen by everyone sharing a room), and it waits for @pm's fixed
device (PM_BOT) while @pm restarts. No console address is needed (a credentials file has none).
"""
import asyncio
import logging
import uuid

from aiohttp import ClientError
from nio import AsyncClient, ToDeviceMessage, ToDeviceResponse

from common import agent_kinds
from common import pm_kinds

EVENT_TYPE = "xyz.hagencylabs.agent.kind"
log = logging.getLogger("bridge")


def from_credentials(creds: dict) -> dict | None:
    """The report for this computer's credentials.json ("agent", plus its kind's "model" / "base_url"), or
    None when the file names no kind or a value @pm would refuse. Nothing else is ever read from it."""
    kind = creds.get("agent")
    if kind not in agent_kinds.KINDS:
        return None
    report = {"agent_kind": kind}
    if kind in agent_kinds.MODEL_KINDS and creds.get("model"):
        report["model"] = creds["model"]
    if kind == "remote" and creds.get("base_url"):
        report["base_url"] = creds["base_url"]
    return report if parse(report) is not None else None


def parse(content: object) -> dict | None:
    """A report as @pm records it ({agent_kind, model?, base_url?}: pm_kinds.choice), or None: not a
    dict, a field other than agent_kinds.OFFER_KEYS (an api_key), a non-string or key-like value, or a
    kind, model or address agent_kinds refuses."""
    if not isinstance(content, dict) or not set(content) <= set(agent_kinds.OFFER_KEYS):
        return None
    if not all(isinstance(v, str) for v in content.values()) or any(pm_kinds.looks_like_key(v) for v in content.values()):
        return None
    if content.get("agent_kind") not in agent_kinds.KINDS:
        return None
    picked = pm_kinds.choice(content["agent_kind"], content.get("model", ""), content.get("base_url", ""))
    return picked if isinstance(picked, dict) else None


async def send(client: AsyncClient, pm_id: str, creds: dict) -> bool:
    """Tell @pm (pm_id) what this agent runs; False when there is nothing to say or it didn't go out."""
    report = from_credentials(creds)
    if report is None:
        return False
    try:
        resp = await client.to_device(ToDeviceMessage(EVENT_TYPE, pm_id, "*", report), uuid.uuid4().hex)
    except (OSError, asyncio.TimeoutError, ClientError) as e:
        resp = e
    if isinstance(resp, ToDeviceResponse):
        return True
    # Not fatal on purpose: the agent works without it; My agents keeps "not reported yet" until the next start.
    log.warning("couldn't tell @pm which AI this agent runs: %s", resp)
    return False
