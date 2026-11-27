"""PM_BRAIN=owner (docs/adr/0022-pm-understands-with-owner-ai.md): @pm understands a message with the
room owner's own agent instead of a model on this server, which has none.

@pm sends that agent the same context its own model got (pm_understand.think) as one to-device message
(pm_actions.TASK_TYPE: nothing in the chat); the agent answers "ack" at once, then one action from the
fixed list and a short reply (adapters/agents/understand_task.py). @pm checks the answer again
(pm_actions.checked) and applies it under its usual permission checks. No owner, no owner's agent here,
offline, stopped, Hermes, older code (no "ack"), slow or failed: the answer is a plain reason, and @pm
uses its phrase rules. Mixed into loop.Loop through pm_understand.Understanding.
"""
import asyncio
import logging
import os
import time
import uuid

from nio import MatrixRoom, UnknownToDeviceEvent

from common import pm_actions
from common import room_state
from server.pm_cards import HERMES, STOPPED, cannot, local

WAIT_S = float(os.environ.get("PM_OWNER_TIMEOUT_S", "45"))
ACK_S = 8                     # a running agent with this code says "ack" at once
QUIET_S = 600                 # an agent that didn't is not asked again for this long (asleep, or older code)
SIMPLE = " so I'm using simple phrases. "
NO_AGENT = "The room owner's agent isn't in this room," + SIMPLE
OFFLINE = "The room owner's agent is offline," + SIMPLE
NOT_ANSWERING = "The room owner's agent isn't answering (offline, or its code needs an update)," + SIMPLE
FAILED = "The room owner's agent couldn't read that just now," + SIMPLE
CANNOT = {STOPPED: "The room owner's agent is stopped," + SIMPLE,
          HERMES: "The room owner's agent can't read requests for me yet (Hermes)," + SIMPLE}
BUSY = "I've asked the room owner's agent a lot just now," + SIMPLE
# Every ask is a model turn on the owner's own account: capped per person and per room, on every path
# (named or not), so nobody can run up their bill; over it, the phrase rules answer. (limit, window s)
SENDER_CAPS = ((6, 60), (30, 3600))
ROOM_CAPS = ((15, 60), (100, 3600))
log = logging.getLogger("loop")


class OwnerBrain:
    def brain_waits(self) -> dict:
        """task_id → {"agent", "ack": Event, "answer": Future} while @pm waits for the owner's agent."""
        if "_brain_waits" not in self.__dict__:
            self._brain_waits = {}
            self.client.add_to_device_callback(self.on_understood, UnknownToDeviceEvent)   # once, on first use
        return self._brain_waits

    async def owner_agent(self, room: MatrixRoom) -> tuple[str | None, str]:
        """(the agent to ask, "") or (None, why not): the first owner whose own agent can answer."""
        why, quiet = NO_AGENT, self.__dict__.setdefault("_brain_quiet", {})
        for owner in await room_state.owners(self.client, room):
            agent = self.own_agent(room, owner)
            if agent is None:
                continue
            if reason := cannot(owner, agent):            # Hermes (no to-device task) or stopped
                why = CANNOT.get(reason, why)
                continue
            if self.agent_online(local(agent)) is False:
                why = OFFLINE
            elif time.monotonic() < quiet.get(agent, 0):
                why = NOT_ANSWERING
            else:
                return agent, ""
        return None, why

    async def owner_ready(self, room: MatrixRoom) -> bool:
        return (await self.owner_agent(room))[0] is not None

    def spend(self, budgets: tuple) -> bool:
        """One use of every (key, ((limit, window s), ...)) budget, only if all have one left."""
        now, used = time.monotonic(), self.__dict__.setdefault("_spent", {})
        recent = {key: [t for t in used.get(key, []) if now - t < max(w for _, w in caps)] for key, caps in budgets}
        if any(sum(now - t < w for t in recent[key]) >= limit for key, caps in budgets for limit, w in caps):
            used.update(recent)
            return False
        used.update({key: times + [now] for key, times in recent.items()})
        return True

    async def ask_owner(self, room: MatrixRoom, ctx: dict) -> tuple[str, str] | str:
        """(action, reply) from the room owner's agent, or why not (said when the rules don't catch it)."""
        agent, why = await self.owner_agent(room)
        if agent is None:
            return why
        if not self.spend(((("owner", room.room_id, ctx.get("sender")), SENDER_CAPS),
                           (("owner", room.room_id), ROOM_CAPS))):
            return BUSY
        task_id = uuid.uuid4().hex
        wait = {"agent": agent, "ack": asyncio.Event(), "answer": asyncio.get_running_loop().create_future()}
        self.brain_waits()[task_id] = wait
        try:
            sent = await pm_actions.send(self.client, agent, pm_actions.TASK_TYPE,
                                         {"task_id": task_id, "room": room.room_id, "sent_at": time.time(),
                                          "context": pm_actions.clean_context(ctx)})   # bounded: one to-device message
            answer = await self.owner_answer(wait) if sent else "not sent"
        finally:
            self.brain_waits().pop(task_id, None)
        if answer is None:                    # sent, and no "ack": don't wait on it for every message
            self._brain_quiet[agent] = time.monotonic() + QUIET_S
            return NOT_ANSWERING
        decision = pm_actions.checked(answer.get("action"), answer.get("reply")) if isinstance(answer, dict) else None
        if decision is None:
            log.warning("the room owner's agent %s didn't read it: %s", agent,
                        str(answer.get("error") or f"action {answer.get('action')!r}")[:200] if isinstance(answer, dict) else answer)
        return decision or FAILED

    async def owner_answer(self, wait: dict) -> dict | str | None:
        """The agent's answer; None: no "ack" (not running this code); "too slow": no answer within WAIT_S."""
        try:
            await asyncio.wait_for(wait["ack"].wait(), ACK_S)
        except asyncio.TimeoutError:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(wait["answer"]), WAIT_S)
        except asyncio.TimeoutError:
            return "too slow"

    async def on_understood(self, event: UnknownToDeviceEvent) -> None:
        """The owner's agent answering (only the agent that task went to: Synapse sets the sender)."""
        content = event.source.get("content")
        if event.type != pm_actions.RESULT_TYPE or not isinstance(content, dict):
            return
        task_id = content.get("task_id")      # anyone can send @pm a to-device message: a str key only (GPT review)
        wait = self.brain_waits().get(task_id) if isinstance(task_id, str) else None
        if wait is None or event.sender != wait["agent"]:
            return                            # stale, or not the agent asked
        wait["ack"].set()                     # an answer without its ack still counts
        if content.get("ack") is not True and not wait["answer"].done():
            wait["answer"].set_result(content)
