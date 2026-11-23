"""Make an invite link (docs/adr/0015-one-click-home-steps.md): step ③ on Element's home page.

GET /invite/new signs the person in (signin_page.py, static/signin.js; who it is: page_session.py), then asks:
  POST /invite/new/rooms {access_token|session} → {state: "ok", rooms: [{id, name}]}: the project rooms they
                                                 are in as an owner or maintainer (pm_invite.link_refusal's rule)
  POST /invite/new/make  {session, room, days?} → {state: "ok", link, id, days}, or {state, error}
The link is shown on that page only (not in a chat); it is the same link @pm's "invite link" makes
(invite_links.create, ADR 0014), with the same limits. Its id, never its token, is logged.
"""
import logging

from aiohttp import web

from common import room_state
from server import invite_links
from server import invite_page
from server import page_session
from server import pm_invite
from server import signin_page

log = logging.getLogger("loop")
PAGE_PATH = "/invite/new"
ASSETS = {f"{PAGE_PATH}/page.js": ("invite_new.js", "application/javascript")}
SCOPE = "invite-new"                       # its page sessions and limits (page_session.py)
NOT_IN = "You're not in that room."
DAY_CHOICES = (1, 3, 7, 30)               # what the page offers; anything else is the default


async def rooms_of(loop, user: str) -> list[dict]:
    """The project rooms `user` is in (joined) where they may make invite links, by name."""
    found = []
    for room in list(loop.client.rooms.values()):
        if (user in room.users and user not in room.invited_users and loop.takes_links(room)
                and await room_state.is_moderator(loop.client, room, user)):
            found.append({"id": room.room_id, "name": invite_page.shown(room.display_name)})
    return sorted(found, key=lambda r: r["name"].lower())


def days_of(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value in DAY_CHOICES \
        else invite_links.DEFAULT_DAYS


async def make(loop, user: str, room_id: object, days: int) -> dict:
    """A new link to that room for `user`, if they may make one now."""
    room = loop.client.rooms.get(room_id) if isinstance(room_id, str) else None
    if room is None or user not in room.users or user in room.invited_users:
        return {"state": "refused", "error": NOT_IN}
    try:                                      # read now, not my cached copy (GPT re-check): demoted or gone counts
        standing = await room_state.joined_moderator(loop.client, room.room_id, user)
    except room_state.StateUnreadable:
        return {"state": "failed", "error": "I couldn't check that room just now. Please try again in a minute."}
    if not standing:
        return {"state": "refused", "error": pm_invite.ONLY_MODS}
    if refused := await loop.link_refusal(room, user):   # its last step, the limits, has no await after it
        return {"state": "refused", "error": refused}
    token, link_id = invite_links.create(room.room_id, user, days)
    log.info("invite link %s made for %s by %s on the page", link_id, room.room_id, user)   # the id, never the token
    return {"state": "ok", "link": invite_page.link(token), "id": link_id, "days": days}


def routes(loop) -> list:
    async def rooms(body, user):
        return {"state": "ok", "rooms": await rooms_of(loop, user)}

    async def new_link(body, user):
        return await make(loop, user, body.get("room"), days_of(body.get("days")))
    return [web.get(PAGE_PATH, signin_page.page("invite_new.html")), *(web.get(p, signin_page.assets(ASSETS)) for p in ASSETS),
            web.post(f"{PAGE_PATH}/rooms", page_session.handler(SCOPE, rooms)),
            web.post(f"{PAGE_PATH}/make", page_session.handler(SCOPE, new_link))]
