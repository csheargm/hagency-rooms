"""@pm's side of one-step invite links (docs/adr/0014-one-step-invite-link.md; the store is
invite_links.py, the page invite_page.py):
  - "@pm invite link" (optionally "for 3 days") in a project room, from its owner or a maintainer, or
    the panel's "Make an invite link": the link goes to my private chat with them, never the room;
  - "cancel invite link <id>" (the maker, or an owner or maintainer of that room), anywhere;
  - "@pm cancel invite links" in the room: all of its unused links (owner or maintainer);
  - redeem_invite: the page's sign-in found a person; I invite them and say hello in the room.
Mixed into pm_welcome.Welcome (client, user_id, say, private_room, addressed_text, is_one_to_one).
"""
import asyncio
import logging
import re

from aiohttp import ClientError
from nio import MatrixRoom, RoomInviteError, RoomInviteResponse, RoomMessageText

from common import identity
from common import room_state
from server import console_personal
from server import invite_links
from server import invite_page
from server import project_rooms

log = logging.getLogger("loop")
MAKE_RE = re.compile(r"^\W*(please\W+)?((make|create|send|give)( me)?\s+)?(an?\s+|one\s+)?invite link"
                     r"(\s+for\s+(?P<days>\d{1,3})\s+days?)?(\s+please)?\W*$", re.I)
CANCEL_RE = re.compile(r"^\W*cancel\s+(the\s+)?invite link\s+(?P<id>[0-9a-f]{8})\W*$", re.I)
CANCEL_ALL_RE = re.compile(r"^\W*cancel\s+(all\s+)?(the\s+)?(this room's\s+)?invite links\W*$", re.I)
ONLY_MODS = "Only the room's owner or a maintainer can make invite links."
NOT_HERE = "Invite links are for project rooms. Ask me in the project room you want to invite someone to."
SENT = "I've sent you an invite link in our private chat, {name}."
LINK_DM = ("Here's an invite link to **{room}**. It lets one person in, once, within {days} days:\n\n{url}\n\n"
           "Send it only to the person you're inviting. When they open it, they create an account or sign in, "
           "and they're in the room.\n\nTo cancel it, say \"cancel invite link {id}\" here.")
HELLO = "👋 Welcome, {person}! {by} invited you here with a link."
INVITE_WAIT_S = 30
NO_ROOM = "This room no longer takes invite links. Ask the person who sent it for a new one."


def invited(resp) -> bool:
    """A real success: nio's empty-body success, and HTTP 200 when the transport is known (GPT re-check)."""
    transport = getattr(resp, "transport_response", None)
    return isinstance(resp, RoomInviteResponse) and (transport is None or getattr(transport, "status", None) == 200)


def refused(resp) -> bool:
    """A definite no: a Matrix error code (M_FORBIDDEN, ...), never a proxy's 5xx page or a parse failure."""
    transport = getattr(resp, "transport_response", None)
    status = getattr(transport, "status", None) if transport is not None else None
    return (isinstance(resp, RoomInviteError) and str(resp.status_code or "").startswith("M_")
            and (status is None or 400 <= status < 500))


def days_in(text: str) -> int | None:
    m = MAKE_RE.match(text)
    if m is None:
        return None
    return max(1, min(int(m.group("days") or invite_links.DEFAULT_DAYS), invite_links.MAX_DAYS))


class InviteLinks:
    async def on_invite_text(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """True if the message was about invite links (answered here)."""
        text = self.addressed_text(event, room)
        if text is None:
            return False
        if (days := days_in(text)) is not None:
            answer = await self.make_invite_link(room, event.sender, days)
            await self.say(room.room_id, answer)
        elif m := CANCEL_RE.match(text):
            done = await self.cancel_invite_link(m.group("id").lower(), event.sender)
            await self.say(room.room_id, "Cancelled: that link no longer lets anyone in." if done else
                           "I couldn't find an unused link with that name that you may cancel.")
        elif CANCEL_ALL_RE.match(text):
            await self.say(room.room_id, await self.cancel_room_links(room, event.sender))
        else:
            return False
        return True

    def takes_links(self, room: MatrixRoom | None) -> bool:
        """A project room: not the Welcome room or the Lobby, and not a private chat with me."""
        return room is not None and not project_rooms.is_front_door(room) and not console_personal.is_dm(self, room)

    async def make_invite_link(self, room: MatrixRoom, sender: str, days: int = invite_links.DEFAULT_DAYS) -> str:
        """Send `sender` a new link to `room` privately; what to tell them where they asked."""
        if not self.takes_links(room):
            return NOT_HERE
        if not await room_state.is_moderator(self.client, room, sender):
            return ONLY_MODS
        if refused := invite_links.refusal(room.room_id, sender):
            return refused
        dm = await self.private_room(sender)
        if dm is None:
            return "I couldn't open a private chat with you to send the link. Please try again in a minute."
        token, link_id = invite_links.create(room.room_id, sender, days)
        await self.say(dm, LINK_DM.format(room=identity.plain(room.display_name), days=days,
                                          url=invite_page.link(token), id=link_id))
        log.info("invite link %s made for %s by %s", link_id, room.room_id, sender)   # the id, never the token
        return SENT.format(name=identity.split(sender)[0].lstrip("@"))

    async def may_cancel(self, link: dict, sender: str) -> bool:
        room = self.client.rooms.get(link["room"])
        return link["by"] == sender or (room is not None and await room_state.is_moderator(self.client, room, sender))

    async def cancel_invite_link(self, link_id: str, sender: str) -> bool:
        mine = {link["room"]: await self.may_cancel(link, sender) for link in invite_links.store().values()
                if link["id"] == link_id}
        return invite_links.cancel(link_id, lambda link: mine.get(link["room"], False))

    async def cancel_room_links(self, room: MatrixRoom, sender: str) -> str:
        if not self.takes_links(room):
            return NOT_HERE
        if not await room_state.is_moderator(self.client, room, sender):
            return "Only the room's owner or a maintainer can cancel this room's invite links."
        n = invite_links.cancel_room(room.room_id)
        return f"Cancelled {n} invite link{'s' if n != 1 else ''}." if n else "This room has no unused invite links."

    async def link_stands(self, room: MatrixRoom | None, link: dict) -> bool:
        """The room still takes links and their maker is still in it as an owner or maintainer
        (GPT review: a power level left behind by someone who left counts for nothing)."""
        by = link["by"]
        return (self.takes_links(room) and by in room.users and by not in room.invited_users
                and await room_state.is_moderator(self.client, room, by))

    async def redeem_invite(self, token: object, user: str) -> dict:
        """The page signed in `user`: use the link and invite them. {"state", "room"?} for the page."""
        state, link = invite_links.look_up(token)
        if link is None:
            return {"state": state}
        room = self.client.rooms.get(link["room"])
        if room is not None and user in room.users and user not in room.invited_users:
            return {"state": "member", "room": room.room_id}      # in already: the link stays unused
        if not await self.link_stands(room, link):
            return {"state": "closed", "error": NO_ROOM}           # its maker left, or is no longer an owner or maintainer
        state, link = invite_links.take(token, user)               # once: checked and marked with no await between
        if link is None:
            return {"state": state}
        try:                                              # nio retries timeouts without end by default: bounded here
            resp = await asyncio.wait_for(self.client.room_invite(room.room_id, user), INVITE_WAIT_S)
        except (ClientError, asyncio.TimeoutError) as e:   # it may have gone through: never reopened (GPT review)
            log.warning("invite link %s: no answer inviting %s (%s); kept used", link["id"], user, type(e).__name__)
            return {"state": "unsure"}
        if not invited(resp):
            if refused(resp):                             # the server plainly said no: nobody was invited
                invite_links.give_back(token)
            log.warning("invite link %s: couldn't invite %s: %s", link["id"], user, getattr(resp, "message", resp))
            state = invite_links.look_up(token)[0]        # reopened, or cancelled meanwhile (GPT re-check)
            return {"state": "failed" if state == invite_links.OK else "unsure" if state == invite_links.USED else state}
        invite_links.settle(token)
        log.info("invite link %s used by %s", link["id"], user)
        by = room.user_name(link["by"]) or identity.split(link["by"])[0].lstrip("@")
        await self.say(room.room_id, HELLO.format(person=user, by=identity.plain(by)), mention=user)
        return {"state": invite_links.OK, "room": room.room_id}
