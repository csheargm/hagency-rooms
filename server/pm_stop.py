"""@pm's side of Stop and Start (agent_stop.py, docs/adr/0009-agent-stop-start.md): an owner pauses
their own agent with the Hagency panel's Stop button or "@pm stop my agent [NAME]", and resumes it
with Start or "start my agent [NAME]". @pm writes the switch in the Welcome room (only @pm may) and
records it in agents.json for the panel. Also pick_remote: which of a person's own-computer agents a
command means (Disconnect uses it too). Mixed into loop.Loop through pm_presence.Presence.
"""
import asyncio
import logging
import re

from aiohttp import ClientError
from nio import RoomPutStateResponse

from common import matrix_calls
from common import registry
from adapters.agents.agent_stop import STOP_TYPE, switch_key
from common.bridge_config import SERVER_NAME
from common.local_signup import WELCOME_ALIAS

log = logging.getLogger("loop")
STOP_RE = re.compile(r"^\W*(please\W+)?(?P<verb>stop|start|pause|resume) my "
                     r"(agent(\s+(?P<name>[a-z0-9-]+))?|(?P<label>[a-z0-9]+) agent)\W*$", re.I)
STOPPED = ("Stopped: **{agent}** stays connected, but it ignores messages and tasks until you click **Start** in "
           "the Hagency panel (or say \"start my agent\"). Project work given to it meanwhile waits: I ask it again "
           "once later. If it still answers, its computer runs older code: run its connect line there once more to update it.")
STARTED = "Started: **{agent}** answers again."
HERMES = ("**{agent}** runs Hermes, which can't be stopped from here yet. On its computer, run "
          "`~/.hagency/app/.venv/bin/python ~/.hagency/app/connect.py --stop` (and the connect line to start it again).")
FAILED = "⚠️ I couldn't {verb} your agent just now; please try again."


def wants_stop(verb: str) -> bool:
    return verb.lower() in ("stop", "pause")


class StopStart:
    async def pick_remote(self, room_id: str, person: str, name: str | None, verb: str) -> dict | None:
        """The one agent of the person's own computers that `name` (name or label; None: their only one)
        means; otherwise None, after telling them why."""
        remote = [a for a in registry.agents_of(person) if a["agent"] == registry.REMOTE and registry.owns(person, a["localpart"])]
        if name:
            remote = [a for a in remote if registry.called(a, name)]
        if len(remote) > 1:
            await self.say(room_id, f"Which one? Say \"{verb} my agent NAME\" with one of: "
                                    + ", ".join(a["label"] for a in remote) + ". (Or use the Hagency panel.)")
            return None
        if not remote:
            await self.say(room_id, f"You have no agent {'called ' + name + ' ' if name else ''}connected from your own computer here.")
            return None
        return remote[0]

    async def set_stopped(self, room_id: str, person: str, name: str | None, stop: bool) -> bool:
        """Stop (stop=True) or Start this agent of the person's; True once the switch is written."""
        verb = "stop" if stop else "start"
        found = await self.pick_remote(room_id, person, name, verb)
        if found is None:
            return False
        agent = found["localpart"]
        if found.get("agent_kind") == "hermes":
            await self.say(room_id, HERMES.format(agent=agent))
            return False
        async with self.__dict__.setdefault("_stop_lock", asyncio.Lock()):   # the switch and its copy, in one order
            if not await self.write_stopped(agent, stop):
                await self.say(room_id, FAILED.format(verb=verb))
                return False
            registry.update(person, None if found["first"] else agent, stopped=stop)
        await self.say(room_id, (STOPPED if stop else STARTED).format(agent=agent))
        return True

    async def write_stopped(self, agent: str, stop: bool) -> bool:
        """The switch the agent reads (agent_stop.read_stopped), in Welcome; False (logged) if it can't be written."""
        try:
            welcome = await matrix_calls.room_of_alias(self.client, WELCOME_ALIAS)
            resp = None if welcome is None else await self.client.room_put_state(
                welcome, STOP_TYPE, {"stopped": stop}, state_key=switch_key(f"@{agent}:{SERVER_NAME}"))
        except (RuntimeError, OSError, asyncio.TimeoutError, ClientError) as e:   # the caller tells the owner
            log.error("couldn't write %s's stop switch: %s", agent, e)
            return False
        if not isinstance(resp, RoomPutStateResponse):
            log.error("couldn't write %s's stop switch: %s", agent, resp or "no Welcome room")
            return False
        return True
