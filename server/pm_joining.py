"""Asking to join a project room. Four ways in, one answer: the room's owners and maintainers decide.
  - Element's "Ask to join" on a listed room (a Matrix knock: project_rooms.py);
  - "@pm ask to join owner/name": the room working on that exact repo, even a private one (my reply
    comes first and is the same whether or not such a room exists: pm_projects.py);
  - 🙋 under my "one room per repo" answer (pm_projects.py), or under a listed room in "find a
    project" (pm_find.py: by room, so a room without a repo works too).
Each of the room's moderators gets a private message: "<full ID> asks to join <room>.
👍 Let in · 👎 Decline". Only that moderator's click counts, and only while they're still a joined
moderator of that room; and only while the request still stands (a knock not withdrawn, the person
not in already). 👍 invites them (their agent follows them in when they join, per their own setting);
👎 turns a knock down. One request per person and room a day, a few per person an hour, and a cap for
everyone together.
Mixed into pm_projects.Projects (client, user_id, say, offer_thumb, private_room).
"""
import logging
import time

from nio import MatrixRoom, ReactionEvent, RoomInviteResponse, RoomKickResponse

from common import test_limits
from common import room_state
from common.bridge_config import ROOT
from server.project_rooms import plain
from common.saved_dict import SavedDict

log = logging.getLogger("loop")
LET_IN, DECLINE, ASK = "👍", "👎", "🙋"
REQUEST_S = 7 * 86400          # a moderator's click counts for a week
AGAIN_S = 86400                # the same person and room: once a day
ASKS_PER_HOUR = test_limits.pick(5, 20)              # per person, all rooms
ALL_ASKS_PER_HOUR = 60         # everyone together: a flood never fans out without end
ASKED = ("I've asked the owners of the room working on that repo, if there is one here. You'll get an invite "
         "on the left (under Invites) if they let you in.")


def local_name(user_id: str) -> str:
    return user_id.split(":")[0].lstrip("@")


class Joining:
    def join_requests(self) -> dict:
        """event id of my question to a moderator → {room, person, moderator, dm, at, knock}."""
        if "_join_requests" not in self.__dict__:
            self._join_requests = SavedDict(ROOT / "data" / "join-requests.json")
        return self._join_requests

    def may_ask(self, room_id: str, person: str, knock: str | None = None) -> bool:
        """The cooldowns live apart from the requests, so a settled request still counts (GPT review)."""
        now, limits = time.time(), self.__dict__.setdefault("_ask_limits", {"pairs": {}, "people": {}, "all": []})
        for eid in [e for e, r in self.join_requests().items() if now - r["at"] > REQUEST_S]:
            self.join_requests().pop(eid, None)          # expired: pruned as new ones come
        limits["pairs"] = {k: t for k, t in limits["pairs"].items() if now - t < AGAIN_S}
        recent = [t for t in limits["people"].get(person, []) if now - t < 3600]
        limits["all"] = [t for t in limits["all"] if now - t < 3600]
        pair = (room_id, person, knock)      # a renewed knock is a new request (its old buttons no longer act)
        if (pair in limits["pairs"] or len(recent) >= ASKS_PER_HOUR
                or len(limits["all"]) >= ALL_ASKS_PER_HOUR):
            return False
        limits["pairs"][pair] = now
        limits["people"][person] = [*recent, now]
        limits["all"].append(now)
        return True

    async def ask_to_join(self, room_id: str, person: str, knock: str | None = None) -> bool:
        """Ask the room's moderators to let `person` in (knock: the knock's event id); True if asked."""
        room = self.client.rooms.get(room_id)
        if room is None or (person in room.users and person not in room.invited_users):
            return False
        mods = await room_state.moderators(self.client, room)   # first: a failed read never uses up a try (GPT review)
        if not [m for m in mods if m != person] or not self.may_ask(room_id, person, knock):
            return False
        asked = False
        for mod in mods:
            if mod == person:
                continue
            try:
                dm = await self.private_room(mod)
                if dm is None:
                    continue
                event = await self.say(dm, f"{person} asks to join **{plain(room.display_name)}**. "
                                           f"{LET_IN} Let in · {DECLINE} Decline")
            except RuntimeError as e:         # loop.SendError: the other moderators may still be asked
                log.error("couldn't ask %s about %s: %s", mod, person, e)
                continue
            self.join_requests()[event] = {"room": room_id, "person": person, "moderator": mod, "dm": dm,
                                           "at": time.time(), "knock": knock}
            for key in (LET_IN, DECLINE):
                await self.offer_thumb(dm, event, key)
            asked = True
        return asked

    async def on_knock(self, room: MatrixRoom, person: str, event_id: str) -> None:
        """Element's "Ask to join" on a listed room."""
        if not await self.ask_to_join(room.room_id, person, knock=event_id):
            log.info("knock on %s by %s not forwarded (asked already, too often, or no moderator)", room.room_id, person)

    async def still_wanted(self, req: dict) -> bool:
        """The request still stands: a knock is still that very knock (not withdrawn and renewed); anyone
        else isn't in, invited or banned. Raises StateUnreadable when the room can't be read."""
        now = await room_state.standing(self.client, req["room"], req["person"])
        if req.get("knock"):
            return now["membership"] == "knock" and now["event_id"] == req["knock"]
        return now["membership"] not in ("join", "invite", "ban")

    async def on_join_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was a click on one of my join questions (used or not)."""
        req = self.join_requests().get(event.reacts_to)
        if req is None:
            return False
        key = event.key.replace("️", "")
        if (event.sender != req["moderator"] or room.room_id != req["dm"] or key not in (LET_IN, DECLINE)
                or time.time() - req["at"] > REQUEST_S):
            return True
        target, who = self.client.rooms.get(req["room"]), local_name(req["person"])
        try:
            clicker = await room_state.standing(self.client, req["room"], event.sender)
            if target is None or clicker["membership"] != "join" or not clicker["moderator"]:
                await self.say(room.room_id, "You're no longer an owner or maintainer of that room, so I can't act on this.")
                return True
            if not await self.still_wanted(req):
                self.settle(req)
                await self.say(room.room_id, f"Nothing to do: {who}'s request no longer stands.")
                return True
        except room_state.StateUnreadable as e:   # never decided on a guess: the click can be tried again
            log.warning("join decision on %s postponed: %s", req["room"], e)
            await self.say(room.room_id, "I couldn't check that room just now; click again in a minute.")
            return True
        if key == LET_IN:
            resp = await self.client.room_invite(req["room"], req["person"])
            done, said = isinstance(resp, RoomInviteResponse), f"Invited {who} to **{plain(target.display_name)}**."
        else:
            resp = await self.client.room_kick(req["room"], req["person"], "An owner declined the request to join") \
                if req.get("knock") else RoomKickResponse()
            done, said = isinstance(resp, RoomKickResponse), f"Declined: {who} isn't let in."
        if done:                              # one answer settles it for every moderator asked, and every
            self.settle(req, every=True)      # way they asked (a knock and 🙋): never a 👍 after a 👎 (GPT review)
        await self.say(room.room_id, said if done else f"⚠️ That didn't work: {getattr(resp, 'message', resp)}. Try again.")
        return True

    def settle(self, req: dict, every: bool = False) -> None:
        """This request only (never a renewed knock's), or, once decided, all of theirs for that room."""
        key = (lambda r: (r["room"], r["person"])) if every else (lambda r: (r["room"], r["person"], r.get("knock")))
        for eid in [e for e, r in self.join_requests().items() if key(r) == key(req)]:
            self.join_requests().pop(eid, None)
