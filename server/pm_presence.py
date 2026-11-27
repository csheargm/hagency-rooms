"""@pm notices when a person's own agent (bring your own, connect.py) goes offline, and tells them.

Every CHECK_S, @pm asks Synapse for each such agent's presence (a syncing agent adapter is "online"; @pm
shares Welcome with every agent, so it may see it). An agent that was online and has now been away
for OFFLINE_S gets ONE private message to its owner, at most once per ALERT_EVERY_S, with a 🔁
button: the Connect page again with a fresh code (running it again is safe). The console's
"My agent" shows Online / Offline from the same view. "@pm disconnect my agent" (or Disconnect in
the console) gives the agent a new password, so its computer can't sign in until a new code.
Mixed into loop.Loop (client, say, offer_thumb, private_room, choose: pm_welcome.py).
"""
import asyncio
import logging
import re
import time

from aiohttp import ClientError
from nio import MatrixRoom, ReactionEvent

from common import admin_session
from common import matrix_calls
from common import registry
from server import auto_update
from server import remote_agent
from common.bridge_config import ROOT, SERVER_NAME
from server.pm_kind_reports import KindReports
from server.pm_remove import Removal
from server.pm_stop import StopStart
from common.saved_dict import SavedDict
from common.setup_accounts import AccountError

log = logging.getLogger("loop")
CHECK_S, OFFLINE_S, ALERT_EVERY_S = 60, 5 * 60, 24 * 3600
LIVE_WAIT_S = 3                           # online_now: a wording only, never worth holding up an invite (GPT review)
AGAIN = "🔁"
OFFLINE = ("Your agent **{agent}** is offline: its computer is off or asleep, or its connector stopped. To bring "
           "it back, run the connect line again on that computer (running it again is safe). Click 🔁 below "
           "and I'll send you your Connect page with a fresh code.")
DISCONNECT_RE = re.compile(r"^\W*(please\W+)?disconnect my (agent(\s+(?P<name>[a-z0-9-]+))?|(?P<label>[a-z0-9]+) agent)\W*$", re.I)
DISCONNECTED = ("Disconnected: {agent} can't sign in from its computer any more. To connect it again, type "
                "\"bring my own\" here for a new code and Connect page.")
DISCONNECTED_MORE = ("Disconnected: {agent} can't sign in from its computer any more. To connect it again, click "
                     "🔁 below for a new code and its Connect page.")


class Presence(Removal, StopStart, KindReports):   # pm_remove.py: Remove, forgetting deactivated accounts; pm_stop.py; pm_kind_reports.py
    def seen(self) -> dict:
        """agent localpart → {"online", "since", "ever", "alerted", "offer", "owner"}, on disk."""
        if "_seen" not in self.__dict__:
            self._seen = SavedDict(ROOT / "data" / "agent-presence.json")
        return self._seen

    def agent_online(self, agent: str) -> bool | None:
        """For the console: True / False once checked, None if never seen."""
        entry = self.seen().get(agent)
        return entry.get("online") if isinstance(entry, dict) else None

    def ever_connected(self, agent: str) -> bool:
        """This agent (localpart) has been online at least once: its computer really connected."""
        entry = self.seen().get(agent)
        return isinstance(entry, dict) and bool(entry.get("ever") or entry.get("online"))

    async def online_now(self, agent: str) -> bool:
        """Online, asking Synapse now when my last check (every CHECK_S) hasn't seen it: live test 2026-11-23,
        "it joins once it's connected" for an agent that connected seconds before and joined a second later."""
        if self.agent_online(agent):
            return True
        offline = self.__dict__.setdefault("_offline_asked", {})   # agent → when it was last asked and wasn't online:
        if time.monotonic() - offline.get(agent, -CHECK_S) < CHECK_S:   # one ask a minute (GPT review); "online" is
            return False                                              # never kept: the minute check takes over
        try:
            if await asyncio.wait_for(matrix_calls.presence(self.client, f"@{agent}:{SERVER_NAME}"), LIVE_WAIT_S):
                return True
        except (OSError, asyncio.TimeoutError, ClientError, RuntimeError) as e:   # only the wording depends on it
            log.warning("couldn't ask for %s's presence: %s", agent, e)
        offline[agent] = time.monotonic()
        return False

    def has_connected_agent(self, person: str) -> bool:
        """At least one of this person's agents has ever connected (pm_agents.py: ➕ only then)."""
        return any(self.ever_connected(a["localpart"]) for a in registry.agents_of(person))

    async def check_agents(self, now: float | None = None) -> None:
        """At most every CHECK_S (called from each tick)."""
        now = time.time() if now is None else now
        if now - self.__dict__.get("_checked", 0) < CHECK_S:
            return
        self._checked = now
        try:
            await asyncio.to_thread(admin_session.expire)   # admin-bot's kept sign-in never outlives its hour
        except (OSError, ValueError) as e:    # the lock, file or answer: tried again at the next check
            log.warning("couldn't retire admin-bot's old session: %s", e)
        await self.prune_registry(now)        # pm_remove.py: Synapse can't say a deactivated account's presence
        people = registry.load()
        for person in people:                 # each of a person's agents on their own computers
            for found in registry.agents_of(person, people):
                if found["agent"] == registry.REMOTE and not self.is_deactivated(found["localpart"]):
                    await self.check_agent(person, found["localpart"], now)
        await auto_update.sweep(self, now)    # ADR 0024: agents on older code update themselves (also after a deploy)

    async def check_agent(self, person: str, agent: str, now: float) -> None:
        try:
            online = await matrix_calls.presence(self.client, f"@{agent}:{SERVER_NAME}")
        except (OSError, asyncio.TimeoutError, ClientError, RuntimeError) as e:   # never stops @pm's tick
            log.warning("couldn't ask for %s's presence: %s", agent, e)
            return
        if online is None:
            return                            # no such account or a deactivated one (expected): no news, no alert
        old = self.seen().get(agent) or {}
        entry = {**old, "owner": person, "online": online, "ever": old.get("ever") or online,
                 "since": old.get("since", now) if old.get("online") == online else now}
        self.seen()[agent] = entry
        alerted = entry.get("alerted")
        if (online or not entry["ever"] or now - entry["since"] < OFFLINE_S
                or (alerted is not None and (alerted >= entry["since"] or now - alerted < ALERT_EVERY_S))):
            return                            # online, never connected, just left, or told already
        dm = await self.private_room(person)
        if dm is None:
            return
        try:
            offer = await self.say(dm, OFFLINE.format(agent=agent))
            await self.offer_thumb(dm, offer, AGAIN)
        except RuntimeError as e:             # loop.SendError: tried again at the next check
            log.error("couldn't tell %s their agent is offline: %s", person, e)
            return
        self.seen()[agent] = {**entry, "alerted": now, "offer": offer, "dm": dm}

    async def on_offline_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was 🔁 on my "offline" message, by that agent's owner (handled here)."""
        agent = next((a for a, e in self.seen().items() if isinstance(e, dict) and e.get("offer") == event.reacts_to), None)
        mine = self.seen().get(agent) if agent else None
        if mine is None or mine.get("owner") != event.sender or mine.get("dm") != room.room_id:
            return False
        if event.key.replace("️", "") != AGAIN:
            return True
        found = registry.find(event.sender, agent)
        if found is None or found["first"]:
            await self.choose(room.room_id, event.sender, "🔌")    # pm_welcome.py: a fresh code, its own checks
        elif registry.owns(event.sender, agent):
            try:
                await self.send_code(event.sender, agent)          # pm_agents.py: that agent's Connect page
            except RuntimeError as e:         # no private chat with just them: they can click again
                log.error("couldn't resend %s's code: %s", agent, e)
        return True

    async def disconnect(self, room_id: str, person: str, name: str | None = None) -> bool:
        """This remote agent of the person's (by name or label; None: their only one) gets a new
        password: its computer is signed out for good."""
        found = await self.pick_remote(room_id, person, name, "disconnect")   # pm_stop.py
        if found is None:
            return False
        agent, first = found["localpart"], found["first"]
        remote_agent.CODES.revoke(person, None if first else agent)   # first, here on my own thread: no code can pick up the new password
        try:
            await asyncio.to_thread(remote_agent.disconnect, person, agent)
        except (AccountError, OSError) as e:   # Synapse refused or can't be asked, .env unwritable
            log.error("couldn't disconnect %s's agent: %s", person, e)
            await self.say(room_id, "⚠️ I couldn't disconnect your agent just now; please try again.")
            return False
        if first:
            await self.say(room_id, DISCONNECTED.format(agent=agent))
            return True
        offer = await self.say(room_id, DISCONNECTED_MORE.format(agent=agent))
        await self.offer_thumb(room_id, offer, AGAIN)
        self.seen()[agent] = {**(self.seen().get(agent) or {}), "owner": person, "offer": offer, "dm": room_id}
        return True
