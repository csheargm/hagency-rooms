"""Stop and Start: an agent's owner pauses it from the Hagency panel (docs/adr/0009-agent-stop-start.md).

The server can't start or stop a program on someone's computer, so Stop doesn't end the agent's
process: it stays signed in and online, and it ignores messages and @pm's loop tasks until Start.
The switch is one state event that only @pm can send (Welcome's power levels), in the Welcome room
every account shares with @pm: type STOP_TYPE, state_key the agent's Matrix ID, {"stopped": bool}.
The agent reads it (with its sender) before it answers or works; pm_stop.py writes it.

Hermes agents answer through Hermes's own gateways, not bridge.py, so Stop doesn't reach them yet.
"""
import asyncio
import logging
import urllib.parse

from aiohttp import ClientError
from nio import MatrixRoom, RoomMessageText

from common import github
from common import matrix_calls
from common.bridge_config import SERVER_NAME
from common.local_signup import WELCOME_ALIAS

STOP_TYPE = "xyz.hagencylabs.agent.stopped"
KEY_PREFIX = "agent:"     # Matrix refuses a state_key starting with "@" unless it is the sender's own ID
REPLY = ("I'm stopped right now. {owner} can start me again in the Hagency panel "
         "(My agents → Start), or by telling Product Manager \"start my agent\".")
PM_ID = f"@{github.PM_BOT}:{SERVER_NAME}"
ANSWERED_KINDS = ("plan", "file_issues")   # pm_plan.py: one request, no resend; an error result tells its asker
TASK_LINE = "{kind} skipped: my owner stopped me (Start in the Hagency panel)"

log = logging.getLogger("bridge")


def switch_key(agent_id: str) -> str:
    """The state_key of an agent's switch (@pm writes it, the agent reads it): never a bare user ID."""
    return KEY_PREFIX + agent_id


def stopped_in(event: dict) -> bool:
    """A state event (format=event) says stopped only if @pm sent it and its content says so."""
    content = event.get("content")
    return event.get("sender") == PM_ID and isinstance(content, dict) and content.get("stopped") is True


async def read_stopped(client, agent_id: str) -> bool:
    """Has this agent's owner stopped it? Asked afresh each time (two small reads), so Start works at once."""
    if not getattr(client, "access_token", None):
        return False                          # not signed in yet: nothing to ask with (and nobody to answer)
    try:
        welcome = await matrix_calls.room_of_alias(client, WELCOME_ALIAS)
        if welcome is None:
            return False
        path = (f"/_matrix/client/v3/rooms/{urllib.parse.quote(welcome)}/state/{STOP_TYPE}/"
                f"{urllib.parse.quote(switch_key(agent_id))}?format=event")
        status, event = await matrix_calls.get_json(client, path)
    except (RuntimeError, OSError, asyncio.TimeoutError, ClientError) as e:
        # Fails open, on purpose: Stop is the owner's own pause, not a security control (only they direct
        # this agent anyway), and a network hiccup must not silence an agent nobody stopped. Logged.
        log.warning("couldn't check whether my owner stopped me: %s", e)
        return False
    if status == 404 and event.get("errcode") == "M_NOT_FOUND":
        return False                          # never stopped (a proxy's 404 page is logged below)
    if status != 200 or "sender" not in event:   # 403: not in Welcome (an old account); not an event envelope
        log.warning("couldn't read my stop switch (HTTP %s): answering as if running", status)
        return False
    return stopped_in(event)


class BridgeStop:
    """Mixed into bridge.Bridge: the stop check before a reply or a loop task."""

    async def is_stopped(self) -> bool:
        return await read_stopped(self.client, self.user_id)

    async def stopped_now(self, room: MatrixRoom, event: RoomMessageText | None, notify: bool = True) -> bool:
        """True while stopped; then it says so once per room (a reply to `event` if one), until started
        again. notify=False: quiet (a message that didn't address me, e.g. a possible follow-up)."""
        told = self.__dict__.setdefault("_told_stopped", set())
        if not await self.is_stopped():
            told.clear()                      # started again: a later Stop says so once more
            return False
        if notify and room.room_id not in told:
            told.add(room.room_id)
            await self.send(room.room_id, REPLY.format(owner=self.owner_name), reply_to=event)
        return True

    async def task_unless_stopped(self, room_id: str, task: dict) -> None:
        """run_task, or (stopped) nothing: as for an offline agent, @pm's loop asks again once after
        its step timeout (a resend Start lets through), then pauses the step for a person. An error
        result instead would pause the issue at once, and Start couldn't bring it back (GPT review)."""
        if not await self.is_stopped():
            await self.run_task(room_id, task)
            return
        log.info("stopped by my owner: not taking %s #%s yet", task.get("kind"), task.get("issue") or task.get("pr"))
        if task.get("kind") in ANSWERED_KINDS:   # no watchdog resends these: say so, or the owner waits (GPT re-check)
            result = {"kind": task.get("kind"), "task_id": task.get("task_id"), "error": "my owner stopped me (Start in the Hagency panel)"}
            await self.send(room_id, TASK_LINE.format(kind=task.get("kind")), extra={github.RESULT_KEY: result})
