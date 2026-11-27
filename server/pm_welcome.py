"""@pm welcomes newcomers. Synapse puts every new account in the private Welcome room (#welcome,
created and owned by @pm: deploy/configure_synapse.py, local_signup.py). When a person without an
agent joins it, @pm opens a private chat with them, once, and offers one button:
  🔌 Bring your own: an AI on their own computer (agent_kinds.IN_WORDS; remote_agent.py, connect.py).
The server agent (one run by this server) isn't built yet, so it is not offered; anyone who types
"server agent" (or its old name, "house agent") is told so (old offers still carry a 🏠 button: its
click gets that answer too).
Only that person's click on that message counts, each button once. Typing "bring my own" in that
chat works too, and "@pm I want an agent" anywhere offers it again. People from
other servers (remote_people.py) start by opening a private chat with @pm, or with that sentence.
Mixed into loop.Loop (client, user_id, say, offer_thumb, private_room, addressed_text, busy, started_ms).
"""
import asyncio
import logging
import re
import time

from aiohttp import ClientError
from nio import InviteMemberEvent, JoinResponse, MatrixRoom, ReactionEvent, RoomMemberEvent, RoomMessageText, RoomPreset

from common import test_limits
from common import agent_kinds
from common import identity
from common import matrix_calls
from server import project_rooms
from common import registry
from server import remote_people
from server import pm_signups
from server import server_admins
from common.bridge_config import ROOT, SERVER_NAME
from common.lifecycle import is_stale
from common.local_signup import WELCOME_ALIAS, WELCOME_LOCALPART
from common import intents
from server.pm_address import unaddressed
from server.pm_first import BRING, HOUSE, FirstAgent, picked_from
from server.pm_github import GitHubLink
from server.pm_invite import InviteLinks
from server.pm_lobby import WELCOME_NAME, WELCOME_TOPIC, Lobby
from server.pm_presence import DISCONNECT_RE
from server.pm_stop import STOP_RE, wants_stop
from server.pm_remove import REMOVE_RE
from common.saved_dict import SavedDict

log = logging.getLogger("loop")
SERVER_RE = re.compile(r"\b(server|house) agent\b", re.I)   # "house agent": its old name
BRING_RE = re.compile(r"\bbring (my|your) own\b", re.I)
# The whole message, so "@pm I need an agent to review #7" stays a project request (GPT review 21).
WANT_RE = re.compile(r"^\W*(please\W+)?(i (want|need|would like)|i'?d like|get me|give me|can i (have|get)|how (do|can) i get)"
                     r"\s+(an?|my)\s+(own\s+)?agent(\s+please)?\W*$", re.I)
# "resend me the code", "new code", "connect my agent": the Connect message again, with a fresh code
RESEND_RE = re.compile(r"\b(re-?send|send( me)?|give me|get me|need)\b.{0,20}\b(code|connect\w*( page)?)\b|\bnew code\b"
                       r"|\bconnect my agent\b|\bcode again\b", re.I)
CODES_PER_HOUR = test_limits.pick(5, 20)
WELCOME = ("Welcome to Hagency Rooms, {name}: chat rooms where people and their AI agents build software "
           f"together.\n\nGet your agent: 🔌 **Bring your own**, on your own computer: {agent_kinds.IN_WORDS}.\n\n"
           "Click 🔌 below, or type \"bring my own\" (to choose the AI, for example \"bring my own using ollama "
           "llama3.2\").")
POINTER = ("Welcome, {person}! I've sent you a private message: it's open on the left as **Product Manager**. "
           "Click 🔌 there to get your agent.")
# Other servers don't open my chat for them (deploy/synapse_modules/hagency_auto_accept.py is ours only).
POINTER_REMOTE = ("Welcome, {person}! I've sent you a private message: accept it under Invites (top left), then "
                  "click 🔌 there to get your agent.")


def pointer(person: str) -> str:
    """Where my offer is, in the words that fit their server."""
    return (POINTER if identity.split(person)[1] == SERVER_NAME else POINTER_REMOTE).format(person=person)


class Welcome(Lobby, GitHubLink, FirstAgent, InviteLinks):   # pm_github.py: Connect GitHub, answered first; pm_first.py: 🔌; pm_invite.py
    def welcomes(self) -> dict:
        """person → {"dm", "offer" (event id), "done" (buttons used)}, on disk: welcomed once."""
        if "_welcomes" not in self.__dict__:
            self._welcomes = SavedDict(ROOT / "data" / "welcome.json")
        return self._welcomes

    def is_welcome_room(self, room: MatrixRoom) -> bool:
        return getattr(room, "canonical_alias", None) == WELCOME_ALIAS

    def may_have_agent(self, user_id: str) -> bool:
        """A person (never me, an agent or a bot) who may get an agent here: anyone on this server, and
        people from other servers unless this server turned that off. Always by their full ID."""
        local, server = identity.split(user_id)
        return (bool(identity.USER_RE.match(user_id)) and user_id != self.user_id and not local.endswith(("-agent", "-bot"))
                and (server == SERVER_NAME or remote_people.allowed()))

    def in_my_chat(self, person: str) -> bool:
        """They have joined my private chat with my offer in it (GPT review: a stored welcome isn't enough;
        auto-join can fail, or they may have left it)."""
        dm = (self.welcomes().get(person) or {}).get("dm")
        room = self.client.rooms.get(dm or "")
        if room is None:
            return False          # unknown: maybe gone (GPT review: a stale record must never hide every way in)
        # Invited counts for one of ours: hagency_auto_accept.py joins them a moment later (final round
        # 2026-11-23: checked before that, the public pointer still went out). Left: they're in neither.
        return person in room.users or person in getattr(room, "invited_users", {})

    def is_newcomer(self, user_id: str) -> bool:
        """A person who may have an agent here and has none of their own yet."""
        return self.may_have_agent(user_id) and not registry.owns(user_id, registry.agent_of(user_id))

    async def ensure_welcome_room(self) -> None:
        """The Welcome room exists and is mine (Synapse only creates it for a server's very first user),
        and anyone who joined while I was down is welcomed now."""
        try:
            welcome = await matrix_calls.room_of_alias(self.client, WELCOME_ALIAS)   # a 404 is no error
        except (RuntimeError, OSError, asyncio.TimeoutError, ClientError) as e:   # unknown: never a second one
            log.warning("couldn't check for the Welcome room: %s", e)
            return
        if welcome is None:
            made = await self.client.room_create(alias=WELCOME_LOCALPART, name=WELCOME_NAME, preset=RoomPreset.private_chat,
                                                 topic=WELCOME_TOPIC)
            project_rooms.FRONT_DOOR_IDS.update([made.room_id] if hasattr(made, "room_id") else [])   # GPT re-check
            if not hasattr(made, "room_id"):
                log.warning("couldn't create %s: %s (newcomers won't be greeted)", WELCOME_ALIAS, made)
            await self.ensure_lobby()         # pm_lobby.py: the public front door
            return
        project_rooms.FRONT_DOOR_IDS.add(welcome)
        await self.ensure_welcome_topic(welcome)
        await self.ensure_lobby()
        room = self.client.rooms.get(welcome)
        joined = [u for u in getattr(room, "users", {}) if u not in getattr(room, "invited_users", {})]
        for person in joined:
            if self.is_newcomer(person):
                await self.greet_from_welcome(welcome, person)

    async def on_welcome_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        if is_stale(event, self.started_ms) or event.prev_membership == event.membership:
            return
        if event.membership == "knock":
            await self.on_knock(room, event.state_key, event.event_id)   # pm_joining.py: "Ask to join"
        elif event.membership != "join":
            return
        elif not self.is_welcome_room(room):
            await self.on_room_join(room, event.state_key)      # pm_lobby.py: one line, once, with 👍
        elif self.is_newcomer(event.state_key):
            new = event.state_key not in self.welcomes()   # a live join only, never the start-up scan (GPT review)
            await self.greet_from_welcome(room.room_id, event.state_key)
            if new and event.state_key in self.welcomes():
                pm_signups.notify(self, event.state_key)   # the operator hears of each new sign-up, once

    async def greet_from_welcome(self, welcome_id: str, person: str) -> None:
        """The private offer, plus ONE line in Welcome naming them: Element lists my private chat as
        an invite to accept, which newcomers miss, so the buttons would go unseen."""
        if identity.split(person)[1] == SERVER_NAME and await asyncio.to_thread(server_admins.is_admin, person):
            return                            # the server's admins (@owner): no "Welcome, owner!"
        made_now = person not in self.welcomes()
        if made_now and await self.greet(person) is None:
            return                            # greeted once; a failure is retried at the next join or start
        if identity.split(person)[1] == SERVER_NAME and (made_now or self.in_my_chat(person)):
            return   # made_now: my chat isn't synced yet. It opens by itself (hagency_auto_accept.py); a public
            # "Welcome, @…!" for every newcomer was noise everyone saw (screen tests 2026-11-23)
        entry, pointing = self.welcomes()[person], self.__dict__.setdefault("_pointing", set())
        if entry.get("pointed") or entry.get("done") or person in pointing:
            return                            # said already, or they found my chat (they clicked a button)
        pointing.add(person)
        try:
            await self.say(welcome_id, pointer(person), mention=person)
            self.welcomes()[person] = {**self.welcomes()[person], "pointed": True}
        except RuntimeError as e:             # loop.SendError: retried at their next join or my next start
            log.error("couldn't point %s to my private message: %s", person, e)
        finally:
            pointing.discard(person)

    async def greet(self, person: str) -> str | None:
        """My private chat with them, with the 🔌 button; returns that chat's room id. Remembered only
        once it worked (a failure is retried at my next start, or with "I want an agent")."""
        greeting = self.__dict__.setdefault("_greeting", set())
        if person in greeting:                # a second join while the first greeting is underway
            return None
        greeting.add(person)
        try:
            dm = await self.private_room(person)
            if dm is None:
                return None
            offer = await self.say(dm, WELCOME.format(name=person.split(":")[0].lstrip("@")))
        except RuntimeError as e:             # loop.SendError
            log.error("couldn't welcome %s: %s", person, e)
            return None
        finally:
            greeting.discard(person)
        pointed = {"pointed": True} if self.welcomes().get(person, {}).get("pointed") else {}   # said once, ever
        self.welcomes()[person] = {"dm": dm, "offer": offer, "done": [], **pointed}
        await self.offer_thumb(dm, offer, BRING)   # no 🏠 until the server agent exists (R1)
        return dm

    async def on_welcome_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was a click on my welcome (handled here, used or not), or on "offline" (pm_presence.py)."""
        if (await self.on_offline_reaction(room, event) or await self.on_add_reaction(room, event)   # pm_agents.py
                or await self.on_removal_reaction(room, event)                                       # pm_remove.py
                or await self.on_lobby_reaction(room, event) or await self.on_project_reaction(room, event)):
            return True
        entry = self.welcomes().get(event.sender)
        if not entry or room.room_id != entry.get("dm") or event.reacts_to != entry.get("offer"):
            return False
        key = event.key.replace("️", "")
        if key in (HOUSE, BRING) and key not in entry.get("done", []):
            self.welcomes()[event.sender] = {**entry, "done": [*entry.get("done", []), key]}   # each button once
            await self.choose(room.room_id, event.sender, key)
        return True

    async def on_welcome_text(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """True if the message was about getting an agent (answered here)."""
        if await self.on_invite_text(room, event) or await self.on_github_text(room, event) or await self.on_add_text(room, event) \
                or await self.on_project_text(room, event):   # pm_invite, pm_github, pm_agents, pm_projects
            return True
        entry = self.welcomes().get(event.sender) or {}
        in_dm = room.room_id == entry.get("dm") or self.is_one_to_one(room, event.sender)   # pm_address.py
        text = unaddressed(event.body, self.user_id) if in_dm else self.addressed_text(event, room)
        if text is None:
            return False
        if RESEND_RE.search(text) and not intents.NEGATED.search(text):
            await self.resend_code(room, event.sender)
            return True
        if in_dm and intents.NEGATED.search(text) and (SERVER_RE.search(text) or BRING_RE.search(text)):
            return False                      # "I don't want to bring my own": not a choice
        if in_dm and BRING_RE.search(text):           # "... using ollama llama3.2": the AI it runs (pm_first.py);
            picked = picked_from(text)                # first, so "... on my server agent box" still brings one
            await (self.say(room.room_id, picked) if isinstance(picked, str) else self.choose(room.room_id, event.sender, BRING, picked))
        elif in_dm and SERVER_RE.search(text):
            await self.choose(room.room_id, event.sender, HOUSE)
        elif m := REMOVE_RE.search(text):
            await self.ask_removal(room.room_id, event.sender, m.group("name") or m.group("label"))   # pm_remove.py
        elif m := DISCONNECT_RE.search(text):
            await self.disconnect(room.room_id, event.sender, m.group("name") or m.group("label"))   # pm_presence.py
        elif m := STOP_RE.search(text):
            await self.set_stopped(room.room_id, event.sender, m.group("name") or m.group("label"), wants_stop(m.group("verb")))
        elif WANT_RE.search(text):
            await self.offer_again(room, event.sender)
        else:
            return False
        return True

    async def resend_code(self, room: MatrixRoom, person: str) -> None:
        """A fresh Connect message and one-time code for their agent (what 🔌 sends; a second click on
        🔌 only takes the reaction away, so typing must work too). No agent yet: set one up."""
        recent = [t for t in self.__dict__.setdefault("_codes_sent", {}).get(person, []) if time.time() - t < 3600]
        if len(recent) >= CODES_PER_HOUR:
            await self.say(room.room_id, "That's several codes this hour; the newest one still works. Try again later.")
            return
        self._codes_sent[person] = [*recent, time.time()]
        info = registry.load().get(person, {})
        if self.is_newcomer(person) or info.get("agent") == registry.REMOTE:
            dm = await self.private_room(person)
            if dm is None:
                await self.say(room.room_id, "I couldn't open a private chat with you; please try again.")
                return
            await self.choose(dm, person, BRING)         # bring_own: (re)uses their agent, then send_code
            if dm != room.room_id:
                await self.say(room.room_id, pointer(person), mention=person)
        else:
            await self.say(room.room_id, "Your agent runs on this server: there's no code to connect it.")

    async def offer_again(self, room: MatrixRoom, person: str) -> bool:
        """Answered (True), or my offer couldn't be sent (False: a button may be clicked again)."""
        info = registry.load().get(person, {})   # by full ID: @newbie1:elsewhere is not our newbie1
        if not self.may_have_agent(person):
            await self.say(room.room_id, f"Sorry, {remote_people.OFF}.")
            return True
        if not self.is_newcomer(person) and info.get("agent") != registry.REMOTE:   # pm_agents.py: with ➕
            await self.say_have_agents(room.room_id, person)
            return True
        dm = await self.greet(person)
        if dm and dm != room.room_id:
            await self.say(room.room_id, pointer(person), mention=person)
        return dm is not None

    async def point_to_offer(self, room_id: str, person: str) -> None:
        await self.say(room_id, pointer(person), mention=person)   # pm_lobby.py: my offer went out already

    async def on_remote_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Someone from another server opened a private chat with me: their way in (no Welcome for
        them). I join it, answer as to "I want an agent" and leave: the offer goes to a chat I make
        (theirs may be encrypted, which I can't read, and they could add people to it later)."""
        if (event.state_key != self.user_id or event.membership != "invite" or not event.content.get("is_direct")
                or not self.may_have_agent(event.sender) or identity.split(event.sender)[1] == SERVER_NAME):
            return
        if not self.__dict__.setdefault("_dm_joins", remote_people.DmJoins()).take(event.sender):
            log.warning("not joining %s from %s: too many chats from other servers this hour", room.room_id, event.sender)
            return
        joined = await self.client.join(room.room_id)
        if not isinstance(joined, JoinResponse):
            log.warning("couldn't join %s from %s: %s", room.room_id, event.sender, joined)
            return
        try:
            if await self.only_us(room.room_id, event.sender):   # "is_direct" is only the sender's word
                await self.offer_again(room, event.sender)
        except RuntimeError as e:             # loop.SendError: they can open another chat with me
            log.error("couldn't answer %s in %s: %s", event.sender, room.room_id, e)
        finally:
            left = await self.client.room_leave(room.room_id)
            if not hasattr(left, "room_id"):   # nothing more to do: I never answer there again
                log.warning("couldn't leave %s from %s: %s", room.room_id, event.sender, left)

    async def only_us(self, room_id: str, person: str) -> bool:
        """Asked of the server now, not my cached view: nobody but the two of us joined or invited."""
        resp = await self.client.room_get_state(room_id)
        members = {e.get("state_key") for e in getattr(resp, "events", None) or []
                   if e.get("type") == "m.room.member" and e.get("content", {}).get("membership") in ("join", "invite")}
        return bool(getattr(resp, "events", None)) and members <= {self.user_id, person}
