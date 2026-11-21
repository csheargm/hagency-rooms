"""A person renames their own agent: only its display name (a Matrix ID never changes). "Rename" on the
agent in the panel's My agents card (console.act), or "@pm call my agent NAME" / "@pm rename my agent
[label] to NAME" (with several agents, the label says which; else I ask). Only the agent's owner
(registry.owns), and never a word about other people's agents. @pm sets it on the server with
Synapse's admin API (local_signup.admin_call: admin-bot, one sign-in per change), so it works while
the agent's computer is off; nothing sets an agent's name back (setup_accounts names only @pm, the
agent adapters and connect never set one), and agents.json keeps it so the panel shows it after a restart.
A few renames per person per hour. Mixed into loop.Loop (say, addressed_text, in_private).
"""
import asyncio
import logging
import re
import threading
import time
import unicodedata
import urllib.parse

from nio import MatrixRoom, RoomMessageText

from common import identity
from common import local_signup
from common import registry
from common.bridge_config import SERVER_NAME
from common.identity import plain
from server.pm_address import unaddressed
from server.remote_agent import WRITING
from common.setup_accounts import AccountError

log = logging.getLogger("loop")
NAME_MAX = 40
RENAMES_PER_HOUR = 5
HOUR_S = 3600
RENAMING = threading.Lock()                   # one rename at a time: Synapse and agents.json in one order
RENAME_RE = re.compile(r"^\W*(please\W+)?(?:call my agent\s+(?P<a>.+?)|rename my agent\s+(?:(?P<label>[a-z0-9]+)\s+)?"
                       r"to\s+(?P<b>.+?)|rename my (?P<label2>[a-z0-9]+) agent to\s+(?P<c>.+?))[\s.!]*$", re.I | re.S)
DONE = "Your agent is now called {name} (its ID {agent} stays the same)."
BAD_NAME = f"An agent's name is plain text, 1 to {NAME_MAX} characters, on one line."


def clean_name(text: str) -> str | None:
    """The name as typed, trimmed (quotes around it too), or None: empty, too long, or holding a line
    break or any control or format character (bidi overrides included)."""
    name = str(text).strip().strip("\"'“”‘’").strip()
    if not 1 <= len(name) <= NAME_MAX or any(unicodedata.category(c)[0] == "C" for c in name):
        return None
    return name


def ours(person: str, agent: dict) -> bool:
    """An account @pm made (or signed in to with its own password) for this person's agent under the
    name it must have: never one someone else registered under a name only reserved here (GPT review),
    as removal checks (agent_removal.remove)."""
    named = identity.agent_localpart(person, SERVER_NAME, None if agent["first"] else agent["label"])
    made_here = agent["agent"] != registry.REMOTE or agent["ready"]   # one this server runs, or a finished set-up
    return made_here and agent["localpart"] == named and registry.owns(person, agent["localpart"])


def set_display_name(person: str, agent: str, name: str) -> bool:
    """Blocking: Synapse's admin API sets @agent's display name (its member events in rooms follow),
    then agents.json. Renames go one at a time (RENAMING), so the two always agree; the set-up lock
    (WRITING) is held only to read and write agents.json, never over a sign-in that may wait (GPT review)."""
    user = f"@{agent}:{SERVER_NAME}"
    path = f"/_synapse/admin/v2/users/{urllib.parse.quote(user)}"
    with RENAMING:
        with WRITING:
            found = registry.find(person, agent)
            if found is None or not ours(registry.person_id(person), found):
                return False
        try:
            with local_signup.admin_session() as call:   # one admin sign-in for both calls
                if "_status" in call("GET", path, None):
                    return False              # missing: a PUT would create it (update only)
                r = call("PUT", path, {"displayname": name})
        except (SystemExit, OSError, ValueError, AccountError) as e:   # admin-bot can't sign in, Synapse away
            log.error("couldn't rename %s: %s", user, str(e)[:120])
            return False
        if "_status" in r:
            log.error("couldn't rename %s: %s", user, r.get("errcode"))
            return False
        with WRITING:
            registry.update(person, agent, display=name)   # the panel shows it
    return True


class Renaming:
    async def on_rename_text(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """True if the message asked to rename one of the sender's agents (answered here)."""
        text = unaddressed(event.body) if self.in_private(room, event.sender) else self.addressed_text(event, room)
        m = RENAME_RE.match(text) if text is not None else None
        if not m:
            return False
        name = m.group("a") or m.group("b") or m.group("c")
        self.in_background(self.rename_agent(room.room_id, event.sender, m.group("label") or m.group("label2"), name))
        return True                           # (GPT review: an admin sign-in can wait; the sync loop never does)

    def renames_left(self, person: str) -> bool:
        times = self.__dict__.setdefault("_renames", {})
        recent = [t for t in times.get(person, []) if time.time() - t < HOUR_S]
        times[person] = recent
        if len(recent) >= RENAMES_PER_HOUR:
            return False
        recent.append(time.time())
        return True

    def which_agent(self, person: str, which: str | None) -> tuple[dict | None, str | None]:
        """(their agent, or why not): only their own agents are ever looked at or named."""
        mine = [a for a in registry.agents_of(person) if ours(registry.person_id(person), a)]
        if which:
            found = [a for a in mine if registry.called(a, which)]
            return (found[0], None) if found else (None, f"You have no agent called {plain(which)}.")
        if len(mine) == 1:
            return mine[0], None
        if not mine:
            return None, "You have no agent here yet."
        return None, ("Which one? Say \"rename my agent LABEL to NAME\" with one of: "
                      + ", ".join(a["label"] for a in mine) + ".")

    async def rename_agent(self, room_id: str, person: str, which: str | None, typed: str) -> str:
        """Rename `which` of the person's agents (None: their only one); says the answer and returns it."""
        agent, answer = self.which_agent(person, which)
        name = clean_name(typed)
        if agent and name is None:
            answer = BAD_NAME
        elif agent and any(registry.called(a, name) for a in registry.agents_of(person) if a["localpart"] != agent["localpart"]):
            answer = f"You already have an agent called {plain(name)}; choose another name."   # one name, one agent
        elif agent and not self.renames_left(person):
            answer = "That's several renames this hour; please try again later."
        elif agent:
            if await asyncio.to_thread(set_display_name, person, agent["localpart"], name):
                answer = DONE.format(name=plain(name), agent=f"@{agent['localpart']}:{SERVER_NAME}")
                log.info("%s renamed @%s", person, agent["localpart"])
            else:
                answer = "⚠️ I couldn't rename it just now; please try again later."
        await self.say(room_id, answer)
        return answer
