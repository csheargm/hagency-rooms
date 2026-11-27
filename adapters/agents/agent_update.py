"""Versions and one-click updates, the agent's side (docs/adr/0020-versions-and-agent-self-update.md).

At each sign-in the agent tells @pm the AI it runs (kind_report.py) and the code it runs
(code_report.py). When its owner clicks Update in the Hagency panel, @pm sends it one to-device
message, UPDATE_TYPE {"commit": the server's commit}. Only @pm's account is obeyed (Synapse sets the
sender); self_update.py then checks the install and moves it forward along the official master only.
The outcome goes back to @pm (RESULT_TYPE, one word of code_report.RESULTS). After "updated" the agent
finishes what it started, hands off its sync position and starts again on the new code: in place
(exec) on macOS and Linux, so a terminal run restarts too (GPT review); on Windows it exits
EXIT_UPDATED, which the Windows keeper restarts.
"""
import asyncio
import logging
import os
import sys
import uuid

from aiohttp import ClientError
from nio import ToDeviceMessage, UnknownToDeviceEvent

from adapters.agents import self_update
from adapters.agents.agent_github import BridgeGitHub
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
log = logging.getLogger("bridge")
STARTED = version.of(paths.ROOT)              # the code this process loaded, read as it starts (GPT re-check:
                                              # after sign-in, another agent may have updated the install already)


def runs_own(owner: str) -> bool:
    """This process runs its owner's own version: from a live-* copy, or with one chosen (customize.py)."""
    return paths.ROOT.resolve() != customize.ROOT.resolve() or owner in customize.in_use()


class BridgeUpdate(BridgeGitHub):
    """Mixed into bridge.Bridge; with the panel's Sign in to GitHub (agent_github.py, ADR 0021)."""
    updated = False

    async def report_and_listen(self) -> None:
        """After the first sync: the reports to @pm, then @pm's update and GitHub sign-in requests."""
        await kind_report.send(self.client, PM_ID, CREDENTIALS)                  # My agents: the AI I run
        await code_report.send(self.client, PM_ID, code_report.build(STARTED, runs_own(self.owner)))
        await self.report_github()                                               # My agents: signed in to GitHub?
        self.client.add_to_device_callback(self.on_update_request, UnknownToDeviceEvent)
        self.client.add_to_device_callback(self.on_github_request, UnknownToDeviceEvent)

    async def on_update_request(self, event: UnknownToDeviceEvent) -> None:
        if event.type != UPDATE_TYPE or event.sender != PM_ID:
            return                            # only @pm's account; anyone else's message is ignored
        if self.__dict__.get("_updating"):
            return                            # one update at a time; a second click waits for its answer
        self._updating = True
        content = event.source.get("content") or {}
        ask = content.get("id") if isinstance(content.get("id"), str) else ""   # echoed: @pm matches the answer to its click
        job = asyncio.create_task(self.update_to(content.get("commit"), ask[:64]))   # git and pip take a while: never in the sync loop
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def update_to(self, target: object, ask: str = "") -> None:
        try:
            result = ("not_official" if self.buttons_only     # Hermes: its gateways wouldn't restart (ADR 0020)
                      else await asyncio.to_thread(self_update.apply, target, runs_own(self.owner)))
            if result == "current" and self.stale():
                result = "updated"            # another agent of this computer updated the install: restart onto it
            log.info("update to %s: %s", str(target)[:12], result)
            await self.tell_pm(result, ask)
            if result == "updated":
                self.updated = True
                self.sync_point.stop.set()    # serve() hands off and drains, then serve_until_updated exits
        finally:
            self._updating = self.updated

    def stale(self) -> bool:
        """The install on disk is not the code this process loaded."""
        now, then = version.of(paths.ROOT), STARTED
        return bool(now and then and now["commit"] != then["commit"])

    async def tell_pm(self, result: str, ask: str = "") -> None:
        try:
            await asyncio.wait_for(self.client.to_device(ToDeviceMessage(RESULT_TYPE, PM_ID, "*", {"result": result, "id": ask}),
                                                         uuid.uuid4().hex), kind_report.SEND_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, ClientError) as e:
            # Not fatal on purpose: an updated agent restarts and its new code report says it all.
            log.warning("couldn't tell @pm how the update went: %s", e)

    async def serve_until_updated(self) -> None:
        try:
            await lifecycle.serve(self.client, f"{self.owner}-agent", self.sync_point, self.tasks)
        finally:
            await self.stop_github()          # a GitHub sign-in under way: its gh killed, never orphaned by execv
        if not self.updated:
            return
        if os.name == "nt":
            raise SystemExit(EXIT_UPDATED)    # the Windows keeper starts the new code
        log.info("restarting on the new code")
        os.execv(sys.executable, sys.orig_argv)   # in place: a service, supervise.py or a terminal (--foreground) alike
