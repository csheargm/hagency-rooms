"""Versions and one-click updates, the agent's side (docs/adr/0020-versions-and-agent-self-update.md).

At each sign-in the agent tells @pm the AI it runs (kind_report.py) and the code it runs
(code_report.py). When its owner clicks Update in the Hagency panel, @pm sends it one to-device
message, UPDATE_TYPE {"commit": the server's commit}. Only @pm's account is obeyed (Synapse sets the
sender); self_update.py then checks the install and moves it forward along the official master only.
The outcome goes back to @pm (RESULT_TYPE, one word of code_report.RESULTS). After "updated" the agent
finishes what it started, hands off its sync position and exits EXIT_UPDATED, so its service starts it
again on the new code (launchd, systemd and the Windows keeper restart a failed exit).
"""
import asyncio
import logging
import uuid

from aiohttp import ClientError
from nio import ToDeviceMessage, UnknownToDeviceEvent

from adapters.agents import self_update
from common import code_report
from common import customize
from common import github
from common import kind_report
from common import lifecycle
from common import paths
from common import version
from common.bridge_config import CREDENTIALS, SERVER_NAME

UPDATE_TYPE, RESULT_TYPE = code_report.UPDATE_TYPE, code_report.RESULT_TYPE
EXIT_UPDATED = 75                             # EX_TEMPFAIL: not 0, which the services read as "stay stopped"
PM_ID = f"@{github.PM_BOT}:{SERVER_NAME}"
SEND_TIMEOUT_S = 15
log = logging.getLogger("bridge")


def runs_own(owner: str) -> bool:
    """This process runs its owner's own version: from a live-* copy, or with one chosen (customize.py)."""
    return paths.ROOT.resolve() != customize.ROOT.resolve() or owner in customize.in_use()


class BridgeUpdate:
    """Mixed into bridge.Bridge."""
    updated = False

    async def report_and_listen(self) -> None:
        """After the first sync: both reports to @pm, then @pm's update requests."""
        await kind_report.send(self.client, PM_ID, CREDENTIALS)                  # My agents: the AI I run
        await code_report.send(self.client, PM_ID, code_report.build(version.of(paths.ROOT), runs_own(self.owner)))
        self.client.add_to_device_callback(self.on_update_request, UnknownToDeviceEvent)

    async def on_update_request(self, event: UnknownToDeviceEvent) -> None:
        if event.type != UPDATE_TYPE or event.sender != PM_ID:
            return                            # only @pm's account; anyone else's message is ignored
        if self.__dict__.get("_updating"):
            return                            # one update at a time; a second click waits for its answer
        self._updating = True
        target = (event.source.get("content") or {}).get("commit")
        job = asyncio.create_task(self.update_to(target))   # git and pip take a while: never in the sync loop
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def update_to(self, target: object) -> None:
        try:
            result = ("not_official" if self.buttons_only     # Hermes: its gateways wouldn't restart (ADR 0020)
                      else await asyncio.to_thread(self_update.apply, target, runs_own(self.owner)))
            log.info("update to %s: %s", str(target)[:12], result)
            await self.tell_pm(result)
            if result == "updated":
                self.updated = True
                self.sync_point.stop.set()    # serve() hands off and drains, then serve_until_updated exits
        finally:
            self._updating = self.updated

    async def tell_pm(self, result: str) -> None:
        try:
            await asyncio.wait_for(self.client.to_device(ToDeviceMessage(RESULT_TYPE, PM_ID, "*", {"result": result}),
                                                         uuid.uuid4().hex), SEND_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, ClientError) as e:
            # Not fatal on purpose: an updated agent restarts and its new code report says it all.
            log.warning("couldn't tell @pm how the update went: %s", e)

    async def serve_until_updated(self) -> None:
        await lifecycle.serve(self.client, f"{self.owner}-agent", self.sync_point, self.tasks)
        if self.updated:
            raise SystemExit(EXIT_UPDATED)    # the service starts the new code
