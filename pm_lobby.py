"""@pm's front doors besides Welcome, so nobody has to guess the next step:
  - the Lobby (#lobby:<server>): a public room in the server's room directory that anyone joins,
    people from other Matrix servers too (the site links to it: "Already on Matrix?");
  - in any room I'm in (the Lobby, a project room), someone without an agent who joins gets ONE
    line from me, once ever, with a 👍 "Get your agent" button: their click opens my private offer;
  - the Welcome room's topic points at my private chat ("Start here 👉 …").
Mixed into pm_welcome.Welcome (client, user_id, say, offer_thumb, offer_again, is_newcomer, dm_rooms).
"""
import asyncio
import logging

from aiohttp import ClientError
from nio import MatrixRoom, ReactionEvent, RoomPreset, RoomVisibility

import identity
import matrix_calls
import project_rooms
import server_admins
from bridge_config import ROOT, SERVER_NAME
from project_rooms import LOBBY_ALIAS, LOBBY_LOCALPART
from saved_dict import SavedDict

log = logging.getLogger("loop")
LOBBY_TOPIC = ("Hagency Rooms' lobby: people and their AI agents build software together. New here? Click 👍 "
               "under my greeting and I'll message you how to get your agent.")
WELCOME_TOPIC = "Start here 👉 open 'Product Manager' on the left"
WELCOME_NAME = "Welcome"
GET = "👍"
GREET = "Hi {person}! Click 👍 below to get your agent: I'll send you a private message with the next step."


class Lobby:
    def lobby_greeted(self) -> dict:
        """person → {"room", "event", "done"}: my one greeting line for them, ever (on disk)."""
        if "_lobby_greeted" not in self.__dict__:
            self._lobby_greeted = SavedDict(ROOT / "data" / "lobby-greeted.json")
        return self._lobby_greeted

    async def ensure_lobby(self) -> None:
        """The Lobby exists: public, in the room directory, joinable from any server."""
        try:
            if lobby := await matrix_calls.room_of_alias(self.client, LOBBY_ALIAS):
                project_rooms.FRONT_DOOR_IDS.add(lobby)
                await self.publish_lobby(lobby)
                return
        except (RuntimeError, OSError, asyncio.TimeoutError, ClientError) as e:   # unknown: never a second Lobby
            log.warning("couldn't check for the Lobby: %s", e)   # it still works; the next start tries again
            return
        made = await self.client.room_create(alias=LOBBY_LOCALPART, name="Lobby", topic=LOBBY_TOPIC,
                                             preset=RoomPreset.public_chat, visibility=RoomVisibility.public)
        project_rooms.FRONT_DOOR_IDS.update([made.room_id] if hasattr(made, "room_id") else [])   # GPT re-check
        if not hasattr(made, "room_id"):
            log.warning("couldn't create %s: %s", LOBBY_ALIAS, made)

    async def publish_lobby(self, room_id: str) -> None:
        """An existing Lobby is in the room directory. Screen test: one made while Synapse still refused
        publishing (room_list_publication_rules now allow @pm) was missing from Explore rooms."""
        if await matrix_calls.in_directory(self.client, room_id):
            return
        state = await self.client.room_get_state(room_id)     # GPT review: only the Lobby I made, still public
        events = {e.get("type"): e for e in getattr(state, "events", []) if e.get("state_key") == ""}
        if ((events.get("m.room.create") or {}).get("sender") != self.client.user_id
                or ((events.get("m.room.join_rules") or {}).get("content") or {}).get("join_rule") != "public"):
            raise RuntimeError(f"{LOBBY_ALIAS} points to {room_id}, which isn't a public room I made: not listed")
        if not await project_rooms.publish(self.client, room_id, True):
            raise RuntimeError(f"the room directory refused to list {LOBBY_ALIAS}")
        log.info("listed %s in the room directory", LOBBY_ALIAS)

    async def ensure_welcome_topic(self, room_id: str) -> None:
        """Welcome's name and topic. Synapse's auto-created room has no name, so Element showed
        its raw address (#welcome:…) in newcomers' room lists (seen live)."""
        room = self.client.rooms.get(room_id)
        if room is None:
            return
        for kind, key, value in (("m.room.name", "name", WELCOME_NAME), ("m.room.topic", "topic", WELCOME_TOPIC)):
            if getattr(room, key, None) != value:
                resp = await self.client.room_put_state(room_id, kind, {key: value})
                if not hasattr(resp, "event_id"):    # the room still works; its name and topic are only pointers
                    log.warning("couldn't set Welcome's %s: %s", key, resp)

    async def on_room_join(self, room: MatrixRoom, person: str) -> None:
        """Someone without an agent joined a room I'm in: one line, once ever, with a 👍."""
        greeting = self.__dict__.setdefault("_lobby_greeting", set())
        if (person in self.lobby_greeted() or person in greeting or not self.is_newcomer(person)
                or self.is_private_chat(room)):
            return
        greeting.add(person)
        try:
            if identity.split(person)[1] == SERVER_NAME and await asyncio.to_thread(server_admins.is_admin, person):
                return                        # the server's admins run it: no greeting
            event = await self.say(room.room_id, GREET.format(person=person), mention=person)
            self.lobby_greeted()[person] = {"room": room.room_id, "event": event}
            await self.offer_thumb(room.room_id, event, GET)
        except RuntimeError as e:             # loop.SendError: they're greeted at their next join instead
            log.error("couldn't greet %s in %s: %s", person, room.room_id, e)
        finally:
            greeting.discard(person)

    async def on_lobby_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was a click on my greeting (only the person greeted counts, once)."""
        entry = self.lobby_greeted().get(event.sender)
        if not entry or entry.get("event") != event.reacts_to or entry.get("room") != room.room_id:
            return False
        clicking = self.__dict__.setdefault("_lobby_clicking", set())
        if event.key.replace("️", "") != GET or entry.get("done") or event.sender in clicking:
            return True
        clicking.add(event.sender)
        try:
            if await self.offer_again(room, event.sender):   # pm_welcome.py: my private offer, and where it is
                self.lobby_greeted()[event.sender] = {**entry, "done": True}   # only once it went out (GPT review)
        except RuntimeError as e:             # loop.SendError: the button still works for another click
            log.error("couldn't send %s my offer: %s", event.sender, e)
        finally:
            clicking.discard(event.sender)
        return True
