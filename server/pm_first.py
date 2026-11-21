"""A person's FIRST agent, and which AI it runs (screen test: the picker was only under "Add an agent",
which shows once an agent has connected, so a newcomer could only get Codex or Claude Code).

🔌 (pm_welcome.py) sets it up as before, the AI found on their computer. They may choose another
AI for it until it has connected: "bring my own using ollama llama3.2" in our chat, or the picker in
My Hagency (console_agents.add_agent). The choice is recorded on their first agent (agents.json, the
same fields as a further agent's: pm_kinds.FIELDS) and so reaches credentials_for and the Connect
page link exactly as an added agent's does. Once it has connected, its AI stays: another AI is
another agent (pm_agents.py).
Mixed into loop.Loop (say, busy, may_have_agent, start_add, send_code, has_connected_agent).
"""
import asyncio
import logging
import re

from common import agent_kinds
from common import pm_kinds
from common import registry
from server import remote_agent
from common.redact import room_safe
from common.setup_accounts import AccountError

log = logging.getLogger("loop")
HOUSE, BRING = "🏠", "🔌"   # 🏠: only on old offers (the server agent, not built yet)
USING_RE = re.compile(r"\bbring (my|your) own\b(\s+agent)?\s+(using|with|running)\s+(?P<how>.+?)\W*$", re.I)
SERVER_REPLY = ("A server agent, one that runs on this server for you, isn't here yet. For now you can bring "
                f"your own: type \"bring my own\" ({agent_kinds.IN_WORDS} on your computer).")
# Under the first agent's Connect message until it connects (pm_agents.send_code).
CHOOSE_HINT = ("\n\n{now} For another AI, type for example \"bring my own using hermes\", \"... using ollama "
               "llama3.2\" or \"... using remote https://openrouter.ai/api/v1 openai/gpt-4o\", or choose it in "
               "My Hagency at the top of this chat.")
CONNECTED = ("Your agent has connected already, so it keeps its AI. For another AI, add another agent, for "
             "example \"add another agent called local using ollama llama3.2\".")


def choose_hint(kind: str | None) -> str:
    """What the first agent's Connect message adds while it has never connected: how to pick its AI."""
    now = (f"It runs {pm_kinds.name(kind)}." if kind in agent_kinds.KINDS
           else "It runs the AI it finds on that computer (Codex or Claude Code).")
    return CHOOSE_HINT.format(now=now)


def picked_from(text: str) -> dict | str | None:
    """"bring my own using ollama llama3.2" → the choice (pm_kinds), or why not; None: no AI named."""
    m = USING_RE.search(text)
    return pm_kinds.from_words(m.group("how")) if m else None


class FirstAgent:
    async def choose(self, dm: str, person: str, key: str, picked: dict | None = None) -> None:
        """🔌 (or "bring my own"): set up their first agent and send its Connect message; picked: the AI
        it runs (pm_kinds.choice; {}: the one found on their computer), only while it has never
        connected; None: keep what it has."""
        if key == HOUSE:
            await self.say(dm, SERVER_REPLY)
            return
        busy = self.__dict__.setdefault("_provisioning", set())
        if person in busy or not self.may_have_agent(person):
            return                            # one at a time per person; never an agent, a bot or a refused server
        if registry.first_removed(person):    # its name can't come back: ask for a new one (pm_agents.py)
            await self.start_add(dm, person, None, picked)
            return
        if picked is not None and self.has_connected_agent(person):
            await self.say(dm, CONNECTED)
            return
        busy.add(person)
        job = asyncio.create_task(self.bring_own(dm, person, picked))   # creating the account takes a moment
        job.add_done_callback(lambda _: busy.discard(person))
        self.busy.add(job)
        job.add_done_callback(self.busy.discard)

    async def bring_own(self, dm: str, person: str, picked: dict | None = None) -> None:
        try:
            await asyncio.to_thread(remote_agent.provision, person, picked)
            await self.send_code(person, None)            # pm_agents.py: re-checks it's just the two of us
        except (AccountError, OSError, RuntimeError) as e:   # homeserver refused, .env unwritable, send failed, bad URL
            log.error("bring-your-own failed for %s: %s", person, e)
            try:
                await self.say(dm, f"⚠️ I couldn't set up your agent: {room_safe(e)}. Type \"bring my own\" to try again.")
            except RuntimeError as e2:        # Matrix refuses too: the log line above is all that's left
                log.error("couldn't tell %s: %s", person, e2)
