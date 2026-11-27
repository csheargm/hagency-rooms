"""@pm offers the server agent (ADR 0025; code name: trial) to everyone who may have an agent here,
with or without their own: a 🧪 button under the welcome offer (next to 🔌 Bring your own), "try the
server agent" in our private chat, and its card in My agents (My Hagency), with today's usage. Only while
it runs (trial_agent.start: only with OLLAMA_API_KEY); otherwise nothing here changes the welcome flow,
and the words fall through as before.
Mixed into pm_first.FirstAgent (say, user_id, may_have_agent, welcomes,
offer_thumb, is_one_to_one, private_room; `trial` is set by loop.run).
"""
import logging
import re

from common import intents
from common.redact import room_safe
from server.pm_address import unaddressed
from server import trial_limits
from server import trial_models

log = logging.getLogger("loop")
TRIAL = "🧪"
# "try the server agent", "server agent" alone; "trial": its old name
TRIAL_RE = re.compile(r"\b(try|use|start|want|give me|get)\b.{0,20}\b(trial|server agent)\b"
                      r"|^\W*(trial( agent)?|server agent)\W*$", re.I)
LINE = ("\n\nOr right away: 🧪 **Server agent (free, limited)**, a shared AI this server runs for everyone, for talking "
        "only (questions, ideas, reading a pull request). Click 🧪 below, or type \"try the server agent\".")
READY = ("🧪 Your server agent is ready: accept its chat invite, **Server agent**, under Invites (top left), and "
         "write to it there. It's free and limited ({n} messages a day), and can only talk: no code, no tools, "
         "no building or merging. To use it in a project room, invite it there and mention it by name (it only "
         "answers when named, so it never gets in your own agent's way). What you write to it goes to Ollama Cloud. "
         "For your own agent, type \"bring my own\".")
# "use kimi-k2.7-code", "switch to the model gpt-oss:120b"; "which models": the list (our private chat only)
USE_RE = re.compile(r"^\W*(?:use|switch to|change to|pick)\s+(?:the\s+)?(?P<kw>model\s+)?(?P<name>[a-z0-9][a-z0-9._:-]{0,80})"
                    r"(?P<kw2>\s+model)?(?P<trial>\s+(?:for|in)\s+(?:my\s+)?(?:the\s+)?(?:trial(?:\s+agent)?|server\s+agent))?\W*$", re.I)
MODELS_RE = re.compile(r"\b(which|what|list)\b.{0,20}\bmodels?\b|\bmodels?\b.{0,20}\b(can i|available)\b", re.I)
MODEL_SET = "🧪 Your server agent now uses {name}. (Every model shares the same daily messages.)"
MODEL_LIST = "Your server agent uses {now}. You can pick: {names}. Type for example \"use {other}\"."
NOT_ALLOWED = "{name} isn't one of the server agent's models. You can pick: {names}."
NOT_ENROLLED = "Start the server agent first: click 🧪 or type \"try the server agent\"."
UNAVAILABLE = "The server agent isn't available just now. You can bring your own: type \"bring my own\"."
FAILED = "⚠️ I couldn't start your server agent: {why}. Type \"try the server agent\" to try again."
NOT_FOR_YOU = "The server agent isn't available to your account."


def wants_trial(text: str) -> bool:
    """Asks for the server agent; never "I don't want …" or "bring my own … server agent" (GPT review: those are
    pm_welcome's, a refusal or their own agent)."""
    return bool(TRIAL_RE.search(text)) and not (intents.NEGATED.search(text) or intents.BRING_OWN_RE.search(text))


class TrialOffer:
    trial = None                          # trial_agent.TrialAgent while it runs (loop.run sets it)

    def trial_on(self) -> bool:
        return self.trial is not None and self.trial.running()

    def trial_eligible(self, person: str) -> bool:
        """Anyone who may have an agent here (a person, never an agent, a bot or a refused server), with
        their own agents or without (owner, 2026-11-27): it answers only where it's theirs to call."""
        return self.may_have_agent(person)

    def trial_line(self) -> str:
        """What the welcome offer adds about the server agent: nothing when it's off."""
        return LINE if self.trial_on() else ""

    async def offer_buttons(self, room_id: str, event_id: str, bring: str) -> None:
        await self.offer_thumb(room_id, event_id, bring)
        if self.trial_on():
            await self.offer_thumb(room_id, event_id, TRIAL)

    async def start_trial(self, room_id: str, person: str) -> str:
        """Enrol them and open the server agent's chat with them; what was said (also posted to room_id)."""
        if not self.trial_on():
            said = UNAVAILABLE
        elif not self.trial_eligible(person):
            said = NOT_FOR_YOU
        else:
            try:
                said = READY.format(n=self.trial.usage.per_day(person)) if await self.trial.enroll(person) \
                    else FAILED.format(why="Matrix refused to open the chat")
            except RuntimeError as e:     # its hello couldn't be posted: they're told plainly here, the detail is logged
                log.error("trial chat for %s: %s", person, room_safe(e))
                said = FAILED.format(why="its chat couldn't be opened just now")
        await self.say(room_id, said)
        return said

    async def on_trial_text(self, room, event) -> bool:
        """"try the server agent" in our private chat (True: answered). Off: never mine, as before."""
        in_dm = room.room_id == (self.welcomes().get(event.sender) or {}).get("dm") or self.is_one_to_one(room, event.sender)
        if not in_dm or not trial_limits.enabled():   # no key: never mine (as pm_welcome decides "our private chat")
            return False
        if not self.trial_on():                  # the key is set but it's down or still signing in: say so
            if not wants_trial(unaddressed(event.body, self.user_id)):
                return False
            await self.say(room.room_id, UNAVAILABLE)
            return True
        text = unaddressed(event.body, self.user_id)
        mine = event.sender in self.trial.visitors and self.trial_eligible(event.sender)   # models: only theirs to pick
        m = USE_RE.match(text)                  # first: "use glm-5.3 for the trial" names a model (independent review)
        if m and (m.group("trial") or mine and (trial_models.allowed(m.group("name")) or m.group("kw") or m.group("kw2"))):
            await self.say(room.room_id, self.choose_model(event.sender, m.group("name")))   # NOT_ENROLLED if not theirs
        elif wants_trial(text):
            await self.start_trial(room.room_id, event.sender)
        elif mine and MODELS_RE.search(text):
            await self.say(room.room_id, self.model_list(event.sender))
        else:
            return False                  # not about the trial: the rest of @pm's chat as before
        return True

    def choose_model(self, person: str, name: str) -> str:
        """What to say for "use <name>": only an allowlisted model, only for someone using the server agent."""
        if person not in self.trial.visitors or not self.trial_eligible(person):
            return NOT_ENROLLED
        if not self.trial.set_model(person, name):
            return NOT_ALLOWED.format(name=name, names=", ".join(trial_models.names()))
        return MODEL_SET.format(name=self.trial.model_of(person))

    def model_list(self, person: str) -> str:
        now, names = self.trial.model_of(person), trial_models.names()
        return MODEL_LIST.format(now=now, names=", ".join(names), other=next((n for n in names if n != now), now))

    def counts_once(self, key: str) -> bool:
        """A welcome button is used up by its click, except 🧪 while the trial is down (it says so; it works later)."""
        return key != TRIAL or self.trial_on()

    def trial_view(self, person: str) -> dict | None:
        """The panel's Server agent card (My agents, for everyone), or None (off, or not a person who may use it)."""
        if not self.trial_on() or not self.trial_eligible(person):
            return None
        usage = self.trial.usage
        return {"on": person in self.trial.visitors, "used": usage.used(person), "per_day": usage.per_day(person),
                "tier": self.trial.tiers.tier(person), "model": self.trial.model_of(person), "models": trial_models.names()}

    async def trial_action(self, person: str, action: str = "trial", value: str = "") -> str | dict:
        """The panel's "Start it" / "Open its chat" button (the same as 🧪, answered in our private chat too):
        with the trial chat's room, which the panel opens (GPT review: "again" left them where they were); or its
        model picker."""
        if action == "trial_model":
            said = self.choose_model(person, value) if self.trial_on() else UNAVAILABLE
            return "ok" if said.startswith("🧪") else said
        dm = await self.private_room(person)
        if dm is None:
            return "I couldn't open our private chat; please try again."
        said = await self.start_trial(dm, person)
        chat = self.trial.dm_of(person) if said.startswith("🧪") else None
        return {"result": "ok", "chat": chat} if chat else "ok" if said.startswith("🧪") else said
