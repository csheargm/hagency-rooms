"""@pm records which AI an agent on a person's computer runs, as the agent itself reports it
(common/kind_report.py: a to-device message each time its adapter signs in; docs/adr/0008-agent-kinds.md).

Only the sender's own entry changes: Synapse sets the sender, so one account can never set another
agent's kind, and a person's own account isn't an agent. It is kept on that agent's entry as
"reported" (registry.REPORTED): My agents shows it over what was picked (pm_kinds.describe), but it
is never offered back to a computer with a new code and never decides what @pm allows (Stop and Start
read the picked kind: GPT review, a sign-in can't talk its way out of them). Mixed into loop.Loop (via
pm_presence.Presence): listen_early registers it before @pm's first sync, so a report sent while
@pm was away isn't lost.
"""
import logging

from nio import InviteMemberEvent, UnknownToDeviceEvent

from common import kind_report
from common import pm_kinds
from common import registry
from common.bridge_config import SERVER_NAME

log = logging.getLogger("loop")


def record(sender: str, content: object) -> bool:
    """Store `sender`'s report on its own entry; False (nothing written) when the sender isn't one of
    this server's agents on a person's computer, the report is refused, or nothing changed."""
    suffix = f":{SERVER_NAME}"
    if not (isinstance(sender, str) and sender.startswith("@") and sender.endswith(suffix)):
        return False
    said = pm_kinds.reported(content)
    if said is None:                          # never logged: a refused report may hold what looked like a key
        log.warning("ignored %s's AI report: it isn't one this server takes", sender)
        return False
    agent = sender[1:-len(suffix)]
    with registry.LOCK:                       # a Remove in between can't make the write miss (GPT re-check)
        owner = registry.owner_of(agent)
        found = registry.find(owner, agent) if owner else None
        if found is None or found["localpart"] != agent or found["agent"] != registry.REMOTE:
            log.info("ignored an AI report from %s: not an agent on a person's computer", sender)
            return False
        if found.get(registry.REPORTED) == said:
            return False                      # every start reports: unchanged, nothing written
        registry.update(owner, None if found["first"] else agent, **{registry.REPORTED: said})   # whole: an old model goes
    log.info("%s runs %s (its own report)", sender, said["agent_kind"])
    return True


class KindReports:
    def listen_early(self) -> None:
        """Callbacks that must see @pm's first sync: pending invites (pm_follow.py) and AI reports."""
        self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_to_device_callback(self.on_kind_report, UnknownToDeviceEvent)

    async def on_kind_report(self, event: UnknownToDeviceEvent) -> None:
        if event.type != kind_report.EVENT_TYPE:
            return
        try:
            record(event.sender, event.source.get("content"))
        except OSError as e:                  # agents.json unwritable: only the badge waits (the next start reports
            log.error("couldn't record %s's AI report: %s", event.sender, e)   # again); never stops @pm's sync
