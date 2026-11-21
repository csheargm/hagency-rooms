"""Removing a room's GitHub repo (the owner asked: a repo could be switched, never removed). Two ways:
"Remove repo" in the Hagency panel (two clicks in the page, then console.act), or "@pm remove the
repo" / "@pm this room has no repo" in the room (a ✅ under my question; only the asker's click,
within CONFIRM_S). The same people as choosing it: the room owner or a maintainer, read now. No GitHub
check: removing only drops @pm's link to the repo and never touches GitHub. Never under running work
(pm_settings.BUSY: "stop" first). Under the poll's lock and the binding lock, as a bind, it:
  - writes the room's binding with no repo and mode off (room_state.unbound), so each agent's copy
    of the repo goes on its next turn (bridge.answer → room_context.prune_projects with keep=None)
    and an MCP agent's read_project_file finds no repo;
  - frees the repo in @pm's one-room-per-repo records (primary_room_for reads the rooms' state; the
    discussion records naming this room go), so another room can take it;
  - ends an install wait, forgets plans and questions made for the repo, and blanks the status board;
  - keeps a listed or open room's readable address under the visibility rules (room_address.follow).
Mixed into pm_projects.Projects (client, say, offer_thumb, lock, state, pending, boards, kick, ...).
"""
import asyncio
import logging
import re
import time

from nio import MatrixRoom, ReactionEvent

from common import github
from common import intents
from server import project_rooms
from server import room_address
from common import room_state
from server.loop_progress import ACTIVE

log = logging.getLogger("loop")
CONFIRM_S = 10 * 60
UNREPO_RE = re.compile(r"^\W*(please\W+)?(this room has no (github )?repo(sitory)?( now| anymore| any more)?"
                       r"|(remove|drop|unset|clear|forget) (the |this room'?s? |our )?(github )?repo(sitory)?"
                       r"( (from|for|of) this room)?)[\s.!]*$", re.I)
ASK = ("Remove this room's GitHub repo, **{repo}**? Mode goes to off and the agents' copy of it goes. Nothing on "
       "GitHub changes, and you can pick a repo again any time. Click ✅ below to remove it.")
DONE = "This room has no GitHub repo now. Pick one any time in the Hagency panel."
# The same words whether or not a repo choice was waiting for the GitHub App (an install wait ends
# quietly; GPT re-check, batch 12-14: the wait's repo is never named here).
NO_REPO_HERE = "This room has no GitHub repo to remove, and no earlier choice will set one up now."
PAST_KEPT = (" ⚠️ I couldn't mark this room as having held a private repo, so keep it private or listed (never "
             "open): its earlier messages stay readable to anyone who joins.")
NOT_MODERATOR = "Only the room owner or a maintainer can remove this room's repo."
UNREADABLE = "I couldn't check this room just now; please try again in a minute."
NOT_PROJECT = "Only project rooms have a repo."
CHANGED = "This room's repo changed since you asked, so I haven't removed it. Look again and ask again if you still want to."


class RepoRemoval:
    def repo_removals(self) -> dict:
        """event id of my "remove the repo?" question → {person, room, until} (in memory: CONFIRM_S)."""
        return self.__dict__.setdefault("_repo_removals", {})

    def not_a_project(self, room: MatrixRoom) -> str | None:
        return NOT_PROJECT if project_rooms.is_front_door(room) or self.is_private_chat(room) else None

    async def ask_remove_repo(self, room: MatrixRoom, sender: str) -> None:
        """"@pm remove the repo": ask once with a ✅ (every check runs again on the click)."""
        refused = self.not_a_project(room)
        if not refused and not await room_state.is_moderator(self.client, room, sender):
            refused = NOT_MODERATOR
        repo = None if refused else await room_state.repo_of(self.client, room.room_id)
        if not refused and repo is None and await self.stop_waiting(room.room_id):
            repo = await room_state.repo_of(self.client, room.room_id)   # a bind that was finishing
        if refused or repo is None:
            await self.say(room.room_id, refused or NO_REPO_HERE)
            return
        event_id = await self.say(room.room_id, ASK.format(repo=repo))
        asked, now = self.repo_removals(), time.time()
        for old in [e for e, a in asked.items() if a["until"] < now or (a["person"], a["room"]) == (sender, room.room_id)]:
            del asked[old]                    # expired or replaced questions don't pile up
        asked[event_id] = {"person": sender, "room": room.room_id, "until": now + CONFIRM_S, "repo": repo}
        await self.offer_thumb(room.room_id, event_id, "✅")

    async def stop_waiting(self, rid: str) -> bool:
        """No repo yet, but one chosen and waiting for the GitHub App's install (pm_settings.watch_install):
        "remove the repo" ends that wait, so it can't bind the repo later, and waits for a bind it was
        already finishing (that one is shielded) before the room is read again (GPT review and re-check)."""
        waiting = getattr(self, "install_watches", {}).get(rid)
        if not waiting:
            return False
        self.stop_install_watch(rid)
        await asyncio.gather(waiting[1], return_exceptions=True)   # cancelled, or its bind done
        return True

    async def on_repo_removal_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        """True if this was a reaction to my "remove the repo?" question (handled here)."""
        asked = self.repo_removals().get(event.reacts_to)
        if asked is None or asked["person"] != event.sender or asked["room"] != room.room_id:
            return False
        if not intents.is_confirm_reaction(event.key):
            return True
        self.repo_removals().pop(event.reacts_to)
        if time.time() > asked["until"]:
            await self.say(room.room_id, "That question timed out; say \"@pm remove the repo\" again if you still want to.")
            return True
        await self.remove_repo(room, event.sender, asked["repo"])   # only the repo they were asked about
        return True

    async def remove_repo(self, room: MatrixRoom, sender: str, seen: str) -> str:
        """Remove the room's repo now (already confirmed for `seen`, the repo the person was shown: GPT
        review, a rebind since then is never removed by it); says the answer in the room and returns it."""
        answer = self.not_a_project(room)
        if answer is None and not await room_state.is_moderator(self.client, room, sender):
            answer = NOT_MODERATOR
        if answer is None:
            async with self.lock:             # as choose_repo: nothing starts in between, and it finishes whole
                job = asyncio.ensure_future(self.unbind_checked(room, sender, seen))
                try:
                    answer = await asyncio.shield(job)
                except asyncio.CancelledError:   # GPT review: the lock is held until it has finished
                    await job
                    raise
        await self.say(room.room_id, answer)
        return answer

    async def unbind_checked(self, room: MatrixRoom, sender: str, seen: str) -> str:
        rid = room.room_id
        if any(v.get("room") == rid and v.get("stage") in ACTIVE for v in self.state.values()):
            return settings_text("BUSY")
        failed = await self.write_unbound(rid, sender, seen)
        if failed:
            return failed
        self.forget_repo(rid)
        await self.blank_board(rid)
        await self.address_after_unbind(room)
        log.info("%s removed the repo of %s", sender, rid)
        return DONE + (await self.mark_private_past(rid, seen) or "")

    async def mark_private_past(self, rid: str, repo: str) -> str | None:
        """A private repo's room never becomes open, even with the repo gone: Matrix shows each earlier
        message under the history rule it was sent with, so only keeping newcomers out protects them
        (project_rooms.PRIVATE_PAST_EVENT; GPT review and re-check, batch 12-14)."""
        if not await project_rooms.repo_is_private(repo):
            return None
        done = await self.client.room_put_state(rid, project_rooms.PRIVATE_PAST_EVENT, {"private": True})
        if hasattr(done, "event_id"):
            return None
        log.warning("couldn't mark %s as having held the private repo %s: %s", rid, repo, done)
        return PAST_KEPT

    async def write_unbound(self, rid: str, sender: str, seen: str) -> str | None:
        """The binding with no repo, read and written under the binding lock (pm_settings.write_level);
        the requester is re-read just before the write (GPT review: a demotion while it waited counts)."""
        async with self.__dict__.setdefault("_binding_lock", asyncio.Lock()):
            try:
                repo, _ = await room_state.binding_of(self.client, rid, strict=True)
                if repo is None:
                    return NO_REPO_HERE
                if repo.lower() != seen.lower():
                    return CHANGED
                if not await room_state.joined_moderator(self.client, rid, sender):   # read now, not the cached room
                    return NOT_MODERATOR
            except room_state.StateUnreadable as e:
                log.warning("couldn't check %s before removing its repo: %s", rid, e)
                return UNREADABLE
            resp = await self.client.room_put_state(rid, github.LEVEL_EVENT, room_state.unbound())
        if not hasattr(resp, "event_id"):
            if getattr(resp, "status_code", "") == "M_FORBIDDEN":
                return settings_text("NEED_POWER")
            return f"⚠️ I couldn't save this room's settings: {resp}"
        self.kick()                           # the poll sees the room unbound now
        return None

    def forget_repo(self, rid: str) -> None:
        """@pm's own records of the repo: the room is free of it, and so is the repo."""
        self.stop_install_watch(rid)          # pm_settings.py: an install wait would bind it again
        for records in (self.pending, self.plans, self.last_plans, self.room_repos,
                        getattr(self, "open_issues", {})):
            records.pop(rid, None)
        discussions = self.discussions()      # pm_projects.py: this room's own record, and rooms it ran for
        for room_id in [r for r, runs_in in discussions.items() if r == rid or runs_in == rid]:
            discussions.pop(room_id, None)
        self.drop_conflict_offers(rid)        # an older 🙋/➕ question is settled

    async def blank_board(self, rid: str) -> None:
        """The pinned board says "no repo yet", and the status agents read names no repo (room_context.status).
        Until all of that is done the board stays marked "unbound" and each poll tries again (GPT review)."""
        from server import status_board                   # here: it imports pm_settings, which imports this module
        text = status_board.render({}, "off", [], time.time(), "")
        board = self.boards[rid] = {**self.boards.get(rid, {}), "unbound": True}
        self.save_boards()                    # the retry mark first: a failure or restart below keeps it
        if board.get("event_id") and board.get("text") != text:
            event_id = await self.show(rid, board["event_id"], text, "")
            if event_id and event_id != board["event_id"]:
                board["old_pin"] = board["event_id"]   # posted anew: its pin replaces the old board's
            if event_id:
                board.update(event_id=event_id, text=text, pinned=event_id == board["event_id"])
        if board.get("event_id") and not board.get("pinned"):
            board["pinned"] = await self.pin(rid, board["event_id"], board.get("old_pin"))
            if board["pinned"]:
                board.pop("old_pin", None)
        board["published"] = await self.publish(rid, text, "")
        if board.get("text", text) == text and board.get("pinned", True) and board["published"]:
            board.pop("unbound")
        self.boards[rid] = board
        self.save_boards()

    async def address_after_unbind(self, room: MatrixRoom) -> None:
        """A listed or open room keeps (or gets) its readable address by the same rules as a visibility
        change: an address made while the repo was private never held its name, and nothing is hidden now."""
        try:
            listed = await project_rooms.visibility(self.client, room.room_id, strict=True) != project_rooms.PRIVATE
        except room_state.StateUnreadable as e:   # the repo is removed either way; the next start retries
            log.warning("couldn't read %s's visibility: %s", room.room_id, e)
            return
        if listed:
            note = await room_address.follow(self.client, room.room_id, True, room.display_name, None)
            if note:
                log.info("address of %s after removing its repo:%s", room.room_id, note)


def settings_text(name: str) -> str:
    """pm_settings' own answers (one copy of each text, R3); imported here, not at the top: pm_settings
    imports pm_projects, which imports this module."""
    from server import pm_settings
    return getattr(pm_settings, name)
