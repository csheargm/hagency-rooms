""""find a project": one short message per listed or open project room. Screen test: one block of raw
room-ID pills ending "open one, then Join or Ask to join", and Join failed (403, not invited). Each
message has the room's plain name, its topic, and a public repo's name (never a private repo's):
  - a listed room: 🙋 under it. Only the person who asked, once, within a day: I ask the room's owners
    and maintainers exactly as for a knock (pm_joining.ask_to_join: 👍 Let in · 👎 Decline, then my
    invite), so it needs neither Element's "Ask to join" nor a repo;
  - an open room: a link that joins it (its address, room_address.py, when it has one).
Mixed into pm_projects.Projects (client, say, offer_thumb, ask_to_join, room_name).
"""
import logging
import re
import time
import urllib.parse

from nio import MatrixRoom, ReactionEvent

from server import project_rooms
from common import room_state
from common.bridge_config import SERVER_NAME
from server.pm_joining import ASK
from server.project_rooms import plain

log = logging.getLogger("loop")
MAX_LISTED = 10                # one message each: a room's chat never gets a flood
MAX_SCANNED = 300              # "find a project" reads at most this many rooms
FINDS_PER_HOUR = 3             # "find a project" per person (each reads many rooms)
OFFER_S = 86400                # a 🙋 is answerable for a day
TOPIC_MAX = 160
FIND_RE = re.compile(r"^\W*(please\s+)?(find|list|show|browse)\s+(me\s+)?(a\s+|the\s+|some\s+)?projects?(\s+to join)?\W*$", re.I)
NONE_YET = "No projects are listed yet. Make your own: \"@pm make me a project room\"."
HEADER = "Projects you can join, one per message below (they're also under Explore rooms in Element):"
ASKED_OWNERS = ("I've asked the owners of **{name}**. You'll get an invite on the left (under Invites) "
                "if they let you in.")
GONE = "That room isn't listed any more, so I can't ask for you."
NOT_ASKED = ("I couldn't ask the owners of **{name}** for you: you may have asked today already, or asked to join "
             "several rooms this hour, or nobody there can let people in right now. Try again later.")


def join_link(room: MatrixRoom) -> str:
    """A link that opens the room in Element: its address when it has one, never a raw ID if avoidable."""
    alias = getattr(room, "canonical_alias", None)
    target = alias if isinstance(alias, str) and alias else room.room_id
    return f"https://matrix.to/#/{urllib.parse.quote(target)}?via={urllib.parse.quote(SERVER_NAME)}"


def describe(room: MatrixRoom, vis: str, repo: str | None, repo_private: bool, person: str) -> tuple[str, bool]:
    """(one room's message, whether it gets a 🙋)."""
    topic = plain(" ".join((getattr(room, "topic", None) or "").split()))
    topic = topic[:TOPIC_MAX].rstrip() + ("…" if len(topic) > TOPIC_MAX else "")
    head = f"**{plain(room.display_name)}**" + (f" · {repo}" if repo and not repo_private else "") + (f": {topic}" if topic else "")
    if person in room.users and person not in room.invited_users:
        return f"{head}. You're in it already.", False
    if vis == project_rooms.OPEN:
        return f"{head}. Anyone can join: [join it]({join_link(room)}).", False
    return f"{head}. Click {ASK} below to ask to join; its owners decide.", True


class Finding:
    def find_offers(self) -> dict:
        """event id of one room's line → {room, person, chat, at} (a restart forgets: they ask again)."""
        return self.__dict__.setdefault("_find_offers", {})

    async def answer_find(self, room_id: str, person: str) -> None:
        recent = [t for t in self.__dict__.setdefault("_finds", {}).get(person, []) if time.time() - t < 3600]
        if len(recent) >= FINDS_PER_HOUR:
            await self.say(room_id, "I just listed them; they're also under Explore rooms in Element.")
            return
        self._finds[person] = [*recent, time.time()]
        offers = self.find_offers()
        for eid in [e for e, o in offers.items() if time.time() - o["at"] > OFFER_S]:
            offers.pop(eid, None)             # expired: pruned as new ones come
        try:
            posted = False
            for rid, text, ask in await self.find_projects(person):
                if await project_rooms.visibility(self.client, rid) == project_rooms.PRIVATE:
                    continue                  # made private while I was listing (GPT review)
                if not posted:
                    posted = bool(await self.say(room_id, HEADER))   # only once there is a room to show
                event = await self.say(room_id, text)
                if ask:
                    self.find_offers()[event] = {"room": rid, "person": person, "chat": room_id, "at": time.time(),
                                                 "name": plain(self.room_name(rid))}   # as shown: never a later name
                    await self.offer_thumb(room_id, event, ASK)
            if not posted:
                await self.say(room_id, NONE_YET)
        except (RuntimeError, OSError) as e:  # SendError, network: they can ask again
            log.error("couldn't list projects for %s: %s", person, e)

    async def find_projects(self, person: str) -> list[tuple[str, str, bool]]:
        """[(room, its message, 🙋 or not)] for the listed and open rooms, at most MAX_LISTED."""
        found = []
        for rid, room in list(self.client.rooms.items())[:MAX_SCANNED]:
            if len(found) >= MAX_LISTED:
                break
            vis = await project_rooms.visibility(self.client, rid)
            if vis == project_rooms.PRIVATE:
                continue
            repo = await room_state.repo_of(self.client, rid)
            found.append((rid, *describe(room, vis, repo, await project_rooms.repo_is_private(repo), person)))
        return found

    async def on_find_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was a click on one of my "find a project" lines (used or not)."""
        offer = self.find_offers().get(event.reacts_to)
        if offer is None or offer["chat"] != room.room_id:
            return False
        if (event.sender != offer["person"] or event.key.replace("️", "") != ASK
                or time.time() - offer["at"] > OFFER_S):
            return True                       # only the person who asked, within a day
        self.find_offers().pop(event.reacts_to, None)   # once
        try:
            if await project_rooms.visibility(self.client, offer["room"], strict=True) == project_rooms.PRIVATE:
                await self.say(room.room_id, GONE)   # never its name now: it may have been renamed since
                return True
            asked = await self.ask_to_join(offer["room"], event.sender)
        except (RuntimeError, OSError) as e:  # StateUnreadable, network: the button works again
            log.warning("couldn't forward %s's request for %s: %s", event.sender, offer["room"], e)
            self.find_offers()[event.reacts_to] = offer
            await self.say(room.room_id, f"I couldn't ask just now; click {ASK} again in a minute.")
            return True
        await self.say(room.room_id, (ASKED_OWNERS if asked else NOT_ASKED).format(name=offer["name"]))
        return True
