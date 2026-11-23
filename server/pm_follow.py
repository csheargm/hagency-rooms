"""Agents come with their people. @pm joins a project room when a local person invites it; when a
person joins a room @pm is in, their own agent follows according to their setting (registry.py):
  always (default): @pm invites it · ask: @pm asks them for a ✅ · never: nothing.
When the person leaves, their agent is removed too: an agent never works in a room without its
owner. People change the setting in plain words ("@pm don't bring my agent", "ask me first",
"always bring my agent"). Mixed into loop.Loop (client, user_id, say, pending, online_now: pm_presence.py, ...).
"""
import logging
import time

from nio import InviteMemberEvent, MatrixRoom, RoomGetStateEventResponse, RoomInviteResponse, RoomMemberEvent

from server import ownership
from server import pm_settings
from common import registry
from common import room_state
from common.bridge_config import SERVER_NAME
from common.lifecycle import is_stale
from server.pm_chat import CONFIRM_S

log = logging.getLogger("loop")
FOLLOW_WORDS = {"always": "I'll bring your agent along whenever you join a project room",
                "ask": "I'll ask you before bringing your agent into a room",
                "never": "I won't bring your agent into rooms; invite it yourself when you want it"}


class Follow:
    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """People create project rooms in Element and invite @pm: join if a local person asked. From
        another server, only a private chat (pm_welcome.py: how they ask for an agent)."""
        local = event.sender.split(":")[0]
        if not event.sender.endswith(f":{SERVER_NAME}"):
            await self.on_remote_invite(room, event)
            return
        if event.state_key != self.user_id or event.membership != "invite" or local.endswith(("-agent", "-bot")):
            return
        await self.client.join(room.room_id)
        if event.content.get("is_direct") and await self.only_us(room.room_id, event.sender):
            self.direct_chats()[room.room_id] = event.sender     # pm_chat.py: their words here are to me
            # They opened a chat with me themselves (e.g. "Get your agent" on the home page before my
            # own chat reached them): answer with the offer and where it is, never a moderator request.
            try:
                if self.is_newcomer(event.sender):
                    await self.offer_again(room, event.sender)     # pm_welcome.py
                else:
                    await self.say(room.room_id, "Hi! Say \"@pm help\" to see what I can do.")
            except RuntimeError as e:         # loop.SendError: they can ask again ("I want an agent")
                log.error("couldn't answer %s's chat: %s", event.sender, e)
            return
        r = await self.client.room_get_state_event(room.room_id, "m.room.power_levels", "")
        users = r.content.get("users", {}) if isinstance(r, RoomGetStateEventResponse) else {}
        if users.get(self.user_id, 0) >= room_state.MODERATOR_LEVEL:
            return
        try:
            await self.say(room.room_id, "Hi! I'm the Product Manager. This room was made where I'm not a moderator "
                                         "by default, so I need you to make me one, once: Room settings → Roles & "
                                         "Permissions → **Product Manager** → Moderator. Then the Hagency panel appears "
                                         "at the top of this room, where you pick the project's repo.")
        except RuntimeError as e:             # loop.SendError
            log.error("couldn't greet %s: %s", room.room_id, e)   # the room still works once I'm a moderator

    async def on_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        """A person joins → their agent follows (per setting); a person leaves → their agent goes too."""
        if is_stale(event, self.started_ms) or not self.sync_point.first_time(event.event_id):
            return
        person = event.state_key
        if person == self.user_id or person.split(":")[0].endswith(("-agent", "-bot")):
            return                            # people only (from any server: ownership is by full ID)
        try:
            if event.membership == "join" and event.prev_membership != "join":
                await self.bring_agent(room, person)
            elif event.membership in ("leave", "ban") and event.prev_membership == "join":
                await self.remove_agent(room, person)
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as e:
            # SendError, a damaged registry, a network hiccup: membership still works; the log shows why
            log.error("follow-me failed for %s in %s: %s", person, room.room_id, e)

    async def bring_agent(self, room: MatrixRoom, person: str) -> None:
        if self.is_welcome_room(room) or pm_settings.is_my_chat(room, self.user_id, getattr(self, "dm_rooms", {})):
            return                            # Welcome (everyone's first room) and my private chats, old or new: no agents
        local = person.split(":")[0].lstrip("@")
        for found in registry.agents_of(person):  # each of their agents by its own setting (ADR 0005)
            name = found["localpart"]
            agent = f"@{name}:{SERVER_NAME}"
            if not registry.owns(person, name) or agent in room.users or found["follow"] == "never":
                continue                      # not their registered agent, already here, or they said never
            if found["follow"] == "ask":
                if not found["first"] and room.room_id in self.pending:
                    continue                  # one question at a time in a room; they can invite it themselves
                event_id = await self.say(room.room_id, f"Welcome {local}! Bring your agent ({name}) "
                                                        "into this room? Click 👍 below to bring it.")
                self.pending[room.room_id] = {"kind": "bring", "agent": agent, "event_id": event_id,
                                              "until": time.time() + CONFIRM_S, "by": person}
                await self.offer_thumb(room.room_id, event_id)
                continue
            await self.invite_agent(room.room_id, agent, await self.came_along(local, found))

    async def came_along(self, local: str, found: dict) -> str:
        """What I say after inviting their agent: "came along" only if it can really join now (one run
        by this server, or theirs online: pm_presence.py); an invite alone joins nothing (screen test)."""
        which = "" if found["first"] else " " + found["localpart"]
        if found["agent"] != registry.REMOTE or await self.online_now(found["localpart"]):
            said = f"{local} joined, so their agent{which} came along"
        else:
            said = f"I invited {local}'s agent{which}; it joins once it's connected on their computer"
        return said + " (say \"don't bring my agent\" to change that)."

    async def invite_agent(self, room_id: str, agent: str, note: str) -> None:
        resp = await self.client.room_invite(room_id, agent)
        if not isinstance(resp, RoomInviteResponse):
            log.warning("couldn't invite %s to %s: %s", agent, room_id, resp)
            return
        await self.say(room_id, note)

    async def remove_agent(self, room: MatrixRoom, person: str) -> None:
        for found in registry.agents_of(person):   # only their own: never someone else's on a bad record
            agent = f"@{found['localpart']}:{SERVER_NAME}"
            if registry.owns(person, found["localpart"]) and ownership.joined(room, agent):
                await self.client.room_kick(room.room_id, agent, reason="its owner left the room")

    async def set_follow(self, room: MatrixRoom, sender: str, value: str, agent: str | None = None) -> bool:
        """Only ever the sender's OWN setting (by full ID: from any server, never a same-named other),
        for one agent of theirs (None, as in the chat: their first)."""
        local = sender.lstrip("@").partition(":")[0]
        found = registry.find(sender, agent)
        if found is None or not registry.owns(sender, found["localpart"]):
            await self.say(room.room_id, f"{local}, you don't have {'that agent' if agent else 'an agent'} registered here.")
            return False
        registry.set_follow(sender, value, found["localpart"])
        which = "" if found["first"] else f" ({found['localpart']})"
        await self.say(room.room_id, f"OK {local}: {FOLLOW_WORDS[value]}{which}.")
        return True
