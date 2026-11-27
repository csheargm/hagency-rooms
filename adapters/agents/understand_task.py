"""The "understand" task, the agent's side (docs/adr/0022-pm-understands-with-owner-ai.md): @pm asks the
room owner's agent what a message to @pm means, and the agent answers with one action from @pm's fixed
list and a short reply (common/pm_actions.py: the same prompt and parser @pm's own model used).

It comes as a to-device message (pm_actions.TASK_TYPE), never a room message: nothing shows in the chat
and the conversation isn't copied into the room's history. The agent takes it only from @pm's account
(Synapse sets the sender), only for a room it and its owner are in, and only when its owner owns that
room (room_state.owners, read now). One fresh turn with NO tools at all (agents.without_tools) in an
empty folder; @pm checks the answer again and applies its own permission checks. Mixed into bridge.Bridge.
"""
import asyncio
import logging
import time

from nio import UnknownToDeviceEvent

from adapters.agents import agents
from common import github
from common import pm_actions
from common import room_state
from common.bridge_config import ROOT, SERVER_NAME
from common.redact import room_safe, secret_safe

PM_ID = f"@{github.PM_BOT}:{SERVER_NAME}"
MAX_AGE_S = 120               # an ask older than this (I was asleep) is answered by @pm's rules already
RUN_S = 45                    # @pm waits as long (pm_owner_brain.WAIT_S): a slower answer is never read
MAX_AT_ONCE = 2               # quick turns beside the loop's (bridge.MAX_CONCURRENT_TURNS), never queued
log = logging.getLogger("bridge")


class BridgeUnderstand:
    def listen_understand(self) -> None:
        self.client.add_to_device_callback(self.on_understand, UnknownToDeviceEvent)

    async def on_understand(self, event: UnknownToDeviceEvent) -> None:
        if event.type != pm_actions.TASK_TYPE:
            return
        content = event.source.get("content")
        if event.sender != PM_ID or not isinstance(content, dict):
            log.warning("ignored an understand task from %s (only @pm's are taken)", event.sender)
            return
        if not pm_actions.TASK_ID_RE.match(str(content.get("task_id", ""))):
            return
        job = asyncio.create_task(self.understand(content))   # never inside the sync loop
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def why_not_understand(self, content: dict) -> str | None:
        """Why I won't read this one for @pm (said back to it), or None."""
        room = self.client.rooms.get(content.get("room")) if isinstance(content.get("room"), str) else None
        if room is None or PM_ID not in room.users or PM_ID in room.invited_users:
            return "not in that room"
        if not self.owner_here(room):
            return "my owner isn't in that room"
        if self.owner_id not in await room_state.owners(self.client, room):
            return "my owner doesn't own that room"
        sent = content.get("sent_at")
        if not isinstance(sent, (int, float)) or abs(time.time() - sent) > MAX_AGE_S:
            return "too old"
        if await self.is_stopped():                       # agent_stop.py: my owner paused me
            return "my owner stopped me"
        return None

    async def understand(self, content: dict) -> None:
        task_id = content["task_id"]
        if self.__dict__.get("_understanding", 0) >= MAX_AT_ONCE:
            await self.tell_understood({"task_id": task_id, "error": "busy"})
            return
        self._understanding = self.__dict__.get("_understanding", 0) + 1   # counted before any wait
        try:
            why = await self.why_not_understand(content)
            ctx = pm_actions.clean_context(content.get("context")) if why is None else None
            if ctx is None:
                await self.tell_understood({"task_id": task_id, "error": why or "no message to read"})
                return
            await self.tell_understood({"task_id": task_id, "ack": True})   # @pm stops waiting soon without it
            await self.tell_understood({"task_id": task_id, **await self.read_for_pm(ctx)})
        finally:
            self._understanding -= 1

    async def read_for_pm(self, ctx: dict) -> dict:
        """One no-tools turn: {"action", "reply"} or {"error"} (in room-safe words: @pm may log it)."""
        try:
            said = await asyncio.wait_for(agents.run_turn(self.backend, ROOT / "rooms" / self.owner / "understand",
                                                          pm_actions.prompt(ctx), fresh=True, bare=True), RUN_S)
        except (agents.AgentError, asyncio.TimeoutError, OSError) as e:
            log.warning("couldn't read a message for @pm: %s", e)
            return {"error": room_safe(secret_safe(e, self.client.access_token or "")) or "timed out"}
        decision = pm_actions.parse(secret_safe(said, self.client.access_token or ""))
        return {"action": decision[0], "reply": decision[1]} if decision else {"error": "no usable answer"}

    async def tell_understood(self, content: dict) -> None:
        # Not sent (logged by send): @pm stops waiting after its timeout and uses its phrase rules.
        await pm_actions.send(self.client, PM_ID, pm_actions.RESULT_TYPE, content)
