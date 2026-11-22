"""@pm records which AI an agent on a person's computer runs, as the agent itself reports it
(common/kind_report.py: a to-device message each time its adapter signs in; docs/adr/0008-agent-kinds.md).

Only the sender's own entry changes: Synapse sets the sender, so one account can never set another
agent's kind, and a person's own account isn't an agent. The report lands in the same agent_kind /
model / base_url fields the picker writes (registry, agent_kinds.OFFER_KEYS): what the computer
really runs wins over what was picked, as connect.py's --agent does. Mixed into loop.Loop (via
pm_presence.Presence): listen_early registers it before @pm's first sync, so a report sent while
@pm was away isn't lost.
"""
import logging

from nio import InviteMemberEvent, UnknownToDeviceEvent

from common import agent_kinds
from common import kind_report
from common import registry
from common.bridge_config import SERVER_NAME

log = logging.getLogger("loop")


def record(sender: str, content: object) -> bool:
    """Store `sender`'s report on its own entry; False (nothing written) when the sender isn't one of
    this server's agents on a person's computer, the report is refused, or nothing changed."""
    suffix = f":{SERVER_NAME}"
    if not (isinstance(sender, str) and sender.startswith("@") and sender.endswith(suffix)):
        return False
    agent = sender[1:-len(suffix)]
    owner = registry.owner_of(agent)
    found = registry.find(owner, agent) if owner else None
    if found is None or found["localpart"] != agent or found["agent"] != registry.REMOTE:
        log.info("ignored an AI report from %s: not an agent on a person's computer", sender)
        return False
    picked = kind_report.parse(content)
    if picked is None:                        # never logged: a refused report may hold what looked like a key
        log.warning("ignored %s's AI report: it isn't one this server takes", sender)
        return False
    fields = {k: picked.get(k, "") for k in agent_kinds.OFFER_KEYS}   # every field: an old model can't linger
    if all((found.get(k) or "") == v for k, v in fields.items()):
        return False
    registry.update(owner, None if found["first"] else agent, **fields)
    log.info("%s runs %s (its own report)", sender, picked["agent_kind"])
    return True


class KindReports:
    def listen_early(self) -> None:
        """Callbacks that must see @pm's first sync: pending invites (pm_follow.py) and AI reports."""
        self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_to_device_callback(self.on_kind_report, UnknownToDeviceEvent)

    async def on_kind_report(self, event: UnknownToDeviceEvent) -> None:
        if event.type == kind_report.EVENT_TYPE:
            record(event.sender, event.source.get("content"))
