"""@pm removes a person's own agent for good, and forgets agents whose accounts are gone.

"@pm remove my agent NAME" (or "remove my NAME agent") asks once, with a ✅ under the question: only
that person's click, in that chat, within CONFIRM_S, removes it. The panel's Remove button asks twice
in the page instead and calls remove_for_good directly (console.act). Disconnect stays the gentle
option: it signs the agent out and keeps its slot (pm_presence.py).

Every PRUNE_EVERY_S, @pm reads Synapse's list of deactivated accounts and forgets the people whose
every account is on it (registry_removal.prune), logging each one; their agents are also skipped by the
presence checks, which Synapse can't answer for a deactivated account.
Mixed into loop.Loop (client, say, offer_thumb, seen: pm_presence.py).
"""
import asyncio
import logging
import re
import time

from nio import MatrixRoom, ReactionEvent

from server import agent_removal
from common import intents
from common import registry
from server import remote_agent
from common.bridge_config import SERVER_NAME
from common.setup_accounts import AccountError

log = logging.getLogger("loop")
CONFIRM_S, PRUNE_EVERY_S, PRUNE_RETRY_S = 10 * 60, 6 * 3600, 10 * 60   # a failed save: again in 10 minutes
REMOVE_RE = re.compile(r"^\W*(please\W+)?remove my (agent(\s+(?P<name>[a-z0-9-]+))?|(?P<label>[a-z0-9]+) agent)\W*$", re.I)
ASK = ("Remove **{agent}** for good? Its account here is closed: it's signed out everywhere, leaves every "
       "room and can never sign in again, and its place among your agents is free. This can't be undone "
       "(to only sign it out, say \"disconnect my agent {label}\"). Click ✅ below to remove it.")
REMOVED = "Removed: {agent} is gone for good, and you can add another agent in its place."
NOT_FORGOTTEN = ("Removed: {agent}'s account is closed for good, but I couldn't update my records just now, so it "
                 "may still be listed. Say \"remove my agent {label}\" again later to free its place.")


class Removal:
    def removals(self) -> dict:
        """event id of my "remove it?" question → {person, agent, dm, until} (in memory: 10 minutes)."""
        return self.__dict__.setdefault("_removals", {})

    def removable(self, person: str, name: str | None) -> list[dict]:
        mine = [a for a in registry.agents_of(person) if a["agent"] == registry.REMOTE and registry.owns(person, a["localpart"])]
        return [a for a in mine if registry.called(a, name)] if name else mine

    async def ask_removal(self, room_id: str, person: str, name: str | None) -> None:
        found = self.removable(person, name)
        if len(found) != 1:
            await self.say(room_id, "Which one? Say \"remove my agent NAME\" with one of: "
                           + ", ".join(a["label"] for a in found) + "." if found else
                           f"You have no agent {'called ' + name + ' ' if name else ''}connected from your own computer here.")
            return
        agent, others = found[0], [a["label"] for a in registry.agents_of(person) if not a["first"]]
        if agent["first"] and others:
            await self.say(room_id, f"Remove your other agents first ({', '.join(others)}); {agent['localpart']} goes last.")
            return
        event_id = await self.say(room_id, ASK.format(agent=agent["localpart"], label=agent["label"]))
        now, asked = time.time(), self.removals()
        for old in [e for e, a in asked.items() if a["until"] < now or (a["person"], a["agent"]) == (person, agent["localpart"])]:
            del asked[old]                    # GPT: expired or replaced questions don't pile up
        asked[event_id] = {"person": person, "agent": agent["localpart"], "dm": room_id,
                                     "until": time.time() + CONFIRM_S}
        await self.offer_thumb(room_id, event_id, "✅")

    async def on_removal_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was a reaction to my "remove it?" question (handled here)."""
        asked = self.removals().get(event.reacts_to)
        if asked is None or asked["person"] != event.sender or asked["dm"] != room.room_id:
            return False
        if not intents.is_confirm_reaction(event.key):
            return True
        self.removals().pop(event.reacts_to)
        if time.time() > asked["until"]:
            await self.say(room.room_id, "That question timed out; say \"remove my agent\" again if you still want to.")
            return True
        await self.remove_for_good(room.room_id, event.sender, asked["agent"])
        return True

    async def remove_for_good(self, room_id: str, person: str, agent: str | None) -> bool:
        """Remove it now (the person already confirmed): codes revoked first, then the account closed
        and its entry freed (agent_removal.py). Says what happened in `room_id`. `agent` is an account
        name (localpart or slot), never a display name: those can change or be taken by another agent
        after the question was asked (GPT review, batch 12-14)."""
        found = [a for a in self.removable(person, None) if agent is None or agent in (a["localpart"], a["label"])]
        if len(found) != 1:
            await self.say(room_id, "That isn't one of your agents on your own computer.")
            return False
        localpart, first = found[0]["localpart"], found[0]["first"]
        said = REMOVED.format(agent=localpart)
        try:
            await asyncio.to_thread(agent_removal.remove, person, localpart)
        except agent_removal.NotForgotten as e:   # closed: its codes go too, and they're told the truth
            log.error("removed %s's agent %s, but its record stays: %s", person, localpart, e)
            said = NOT_FORGOTTEN.format(agent=localpart, label=found[0]["label"])
        except AccountError as e:
            await self.say(room_id, f"⚠️ I couldn't remove it: {e}.")
            return False
        except OSError as e:                   # .env or agents.json unwritable: the log has the detail
            log.error("couldn't remove %s's agent %s: %s", person, localpart, e)
            await self.say(room_id, "⚠️ I couldn't remove it just now; please try again.")
            return False
        try:
            remote_agent.CODES.revoke(person, None if first else localpart)   # only once it's gone (GPT re-check)
        except OSError as e:                   # revoked in memory; the account is closed, so a code is useless anyway
            log.error("removed %s's agent %s, but couldn't save its revoked codes: %s", person, localpart, e)
        self.seen().pop(localpart, None)       # pm_presence.py: no "offline" note for it ever again
        log.info("removed %s's agent %s for good", person, localpart)
        await self.say(room_id, said)
        return True

    async def prune_registry(self, now: float) -> None:
        """At most every PRUNE_EVERY_S (pm_presence.check_agents): forget people whose accounts here
        are all deactivated. Unknown (Synapse's list unreadable): nothing changes."""
        if now - self.__dict__.get("_pruned", 0) < PRUNE_EVERY_S:
            return
        self._pruned = now
        dead = await asyncio.to_thread(agent_removal.deactivated)
        if dead is None:
            return                            # agent_removal logged why; the dead set stays as it was
        self._dead = dead
        if self.forget_github_of(dead):       # pm_github.py: a deactivated account's GitHub link goes too
            self._pruned = now - PRUNE_EVERY_S + PRUNE_RETRY_S   # one couldn't be saved: again soon
        try:
            gone = await asyncio.to_thread(agent_removal.prune, dead)
        except OSError as e:                  # agents.json unwritable: logged, the poll goes on; next time again
            log.error("couldn't save agents.json while forgetting deactivated accounts: %s", e)
            self._pruned = now - PRUNE_EVERY_S + PRUNE_RETRY_S
            return
        for person in gone:
            log.warning("forgot %s in agents.json: every account of theirs here is deactivated", person)

    def is_deactivated(self, localpart: str) -> bool:
        return f"@{localpart}:{SERVER_NAME}" in self.__dict__.get("_dead", ())
