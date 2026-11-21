"""Project rooms, in plain words to @pm (each also has a click path: the home page, the Hagency panel):
  - "make me a project room [called X]": I create it, the person is its owner, I moderate it; they
    accept its invite and pick the repo in the panel (their agent follows them in, per their setting);
  - "find a project": the listed and open rooms, one message each, with 🙋 to ask to join (pm_find.py);
  - "make this room private / listed / open" (owner or maintainer): who can find and join it;
  - "ask to join owner/name": pm_joining.py;
  - "remove the repo" / "this room has no repo" (owner or maintainer, with a ✅): pm_unbind.py.
ONE PROJECT ROOM PER REPO: @pm's loop runs per room, so two rooms on one repo would hand the same
GitHub issues out twice. When a room picks a repo another room already runs, I answer with
🙋 "Ask to join that room" and ➕ "Make a separate room anyway": a separate room may discuss the
repo, but its mode stays off (room_state.is_discussion). I never name the other room here: people in this
room may not be allowed to know it (GPT review). The check and the bind happen under one lock.
Mixed into pm_settings.Settings (client, user_id, say, offer_thumb, addressed_text, room_repos, choose_repo).
"""
import asyncio
import logging
import re
import time

from nio import MatrixRoom, ReactionEvent, RoomMessageText, RoomPreset

import panel
import project_rooms
import registry
import room_address
import room_state
from bridge_config import ROOT
from pm_find import FIND_RE, Finding
from pm_joining import ASK, ASKED, Joining
from pm_unbind import UNREPO_RE, RepoRemoval
from project_rooms import plain
from saved_dict import SavedDict

log = logging.getLogger("loop")
SEPARATE = "➕"
ROOMS_PER_HOUR = 3
LOOKUPS_PER_HOUR = 5           # "ask to join owner/name" per person
OFFER_S = 86400                # a 🙋/➕ question is answerable for a day
MAKE_RE = re.compile(r"^\W*(please\s+)?(make|create|start|set up|open)\s+(me\s+)?(a\s+)?(new\s+)?project(\s+room)?"
                     r"(\s+(called|named)\s+(?P<name>[^\n]{1,60}?))?[\s.!]*$", re.I)
ASK_RE = re.compile(r"\bask to join\s+(?:https?://github\.com/)?(?P<repo>[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})", re.I)
VIS_RE = re.compile(r"\b(?:make|set)\s+(?:this|the)\s+(?:room|project)\s+(?:to\s+)?(?P<vis>private|listed|open|public)\b"
                    r"|\b(?P<un>un)?list this (?:room|project)\b", re.I)
RUN_ELSEWHERE = ("This room only discusses its repo: its build loop runs in the room that already works on it. "
                 "Ask there, or ask to join it.")
CONFLICT = ("**{repo}** is already the project of another room here: one room per repo, so two build loops never "
            "take the same issues.\n{ask} Ask to join that room · {sep} Make a separate room anyway (it can discuss "
            "{repo}, but its build loop runs in the other room)")


class Answer(str):
    """My reply, carrying the 🙋/➕ question it asks (pm_settings.set_repo puts the buttons under it)."""
    def __new__(cls, text: str, offer: dict):
        made = super().__new__(cls, text)
        made.offer = offer
        return made


class Projects(Finding, Joining, RepoRemoval):
    async def on_project_text(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """True if the message was one of the project commands (answered here)."""
        text = self.addressed_text(event, room)       # my private chat with them: no need to say "@pm"
        if text is None:
            return False
        if m := MAKE_RE.match(text):
            await self.make_project_room(room, event.sender, (m.group("name") or "").strip(" \"'"))
        elif UNREPO_RE.match(text):                   # pm_unbind.py: asks with a ✅ first
            await self.ask_remove_repo(room, event.sender)
        elif FIND_RE.match(text):
            self.in_background(self.answer_find(room.room_id, event.sender))   # never holds up the sync loop
        elif m := ASK_RE.search(text):
            await self.say(room.room_id, ASKED)       # first, and the same words either way: never confirms a room
            self.in_background(self.ask_by_repo(m.group("repo"), event.sender, room.room_id))
        elif m := VIS_RE.search(text):
            vis = {"public": project_rooms.OPEN}.get((m.group("vis") or "").lower(), (m.group("vis") or "").lower())
            vis = vis or (project_rooms.PRIVATE if m.group("un") else project_rooms.LISTED)
            await self.say(room.room_id, await self.set_visibility(room, event.sender, vis))
        else:
            return False
        return True

    def in_background(self, coro) -> None:
        job = asyncio.create_task(coro)
        self.busy.add(job)                    # a restart lets it finish (lifecycle.py)
        job.add_done_callback(self.busy.discard)

    async def ask_by_repo(self, repo: str, person: str, origin: str) -> None:
        recent = [t for t in self.__dict__.setdefault("_lookups", {}).get(person, []) if time.time() - t < 3600]
        if len(recent) >= LOOKUPS_PER_HOUR:
            return                            # a room scan per ask: bounded per person, found or not
        self._lookups[person] = [*recent, time.time()]
        try:
            target = await self.primary_room_for(repo)
            if target and target != origin:
                await self.ask_to_join(target, person)
        except (RuntimeError, OSError) as e:  # SendError, StateUnreadable, network: they can ask again later
            log.error("couldn't forward %s's request: %s", person, e)

    async def make_project_room(self, origin: MatrixRoom, person: str, name: str) -> None:
        local = person.split(":")[0].lstrip("@")
        recent = self.__dict__.setdefault("_made", {}).setdefault(person, [])
        recent[:] = [t for t in recent if time.time() - t < 3600]
        if local.endswith(("-agent", "-bot")) or len(recent) >= ROOMS_PER_HOUR:
            await self.say(origin.room_id, "That's enough new rooms for now; try again in an hour.")
            return
        recent.append(time.time())
        name = name or f"{local}'s project"
        resp = await self.client.room_create(name=name, preset=RoomPreset.private_chat, invite=[person], room_version="12",
                                             power_level_override={"users": {person: 100}})   # they own it; I made it
        if not hasattr(resp, "room_id"):
            await self.say(origin.room_id, f"⚠️ I couldn't create the room: {getattr(resp, 'message', resp)}")
            return
        await panel.add_to_room(self.client, resp.room_id)
        await self.say(resp.room_id, f"This is **{plain(name)}**. Next: pick its GitHub repo in the Hagency panel above "
                                     "(or say \"@pm this room works on owner/name\"), then invite people: Room info → Invite."
                                     + panel.browser_note(resp.room_id))
        await self.say(origin.room_id, f"Made **{plain(name)}**: accept its invite on the left (under Invites), then pick "
                                       f"its GitHub repo in the Hagency panel at the top.{self.agent_note(person)}")

    def agent_note(self, person: str) -> str:
        """"comes along" only for an agent that can join (one run here, or theirs connected at least once:
        pm_presence.py, as the panel's Add an agent); screen test: said to someone whose agent never connected."""
        mine = [a for a in registry.agents_of(person) if a["follow"] != "never"]   # their own setting (GPT review)
        ready = [a for a in mine if a["agent"] != registry.REMOTE or self.ever_connected(a["localpart"])]
        if any(a["follow"] == "always" for a in ready):
            return " Your agent comes along when you join."
        if ready:
            return " When you join, I'll ask whether to bring your agent."
        return " Your agent joins once it's connected on your computer." if mine else ""

    async def set_visibility(self, room: MatrixRoom, sender: str, value: str) -> str:
        if project_rooms.is_front_door(room) or self.is_private_chat(room):
            return "Only project rooms have this setting."
        async with self.lock:                 # never between a private repo's bind and its closing
            try:                              # read now, under the lock: never a cached role or "no repo" on a failed read
                who = await room_state.standing(self.client, room.room_id, sender)
                if who["membership"] != "join" or not who["moderator"]:
                    return "Only the room owner or a maintainer can change who can find and join this room."
                repo = (await room_state.binding_of(self.client, room.room_id, strict=True))[0]
            except room_state.StateUnreadable:
                return "I couldn't read this room's settings just now; please try again."
            private = await project_rooms.repo_is_private(repo) if repo else False
            past = (value == project_rooms.OPEN and not private   # a private repo removed earlier (GPT re-check)
                    and await project_rooms.had_private_repo(self.client, room.room_id))
            failed = (project_rooms.refusal(value, private) or (project_rooms.NOT_OPEN_PAST if past else None)
                      or (private and await project_rooms.history_safe(self.client, room.room_id))
                      or await project_rooms.apply(self.client, room.room_id, value,
                                                   await project_rooms.visibility(self.client, room.room_id)))
            if failed:
                return failed
            note = await room_address.follow(self.client, room.room_id, value != project_rooms.PRIVATE,
                                             room.display_name, repo if private else None)
        return f"This room is now {project_rooms.WORDS[value]}.{note}"

    async def give_addresses(self) -> None:
        """At start: listed and open rooms from before readable addresses existed get one (screen test:
        "final project !5nbmn…" in the directory). The same rules as a visibility change (room_address.
        follow): kept when the room has one, never a private repo's name, under the lock."""
        for room_id, room in list(self.client.rooms.items()):
            if project_rooms.is_front_door(room) or self.is_private_chat(room):
                continue
            try:
                async with self.lock:
                    if await project_rooms.visibility(self.client, room_id, strict=True) == project_rooms.PRIVATE:
                        continue
                    repo = (await room_state.binding_of(self.client, room_id, strict=True))[0]
                    private = await project_rooms.repo_is_private(repo) if repo else False
                    note = await room_address.follow(self.client, room_id, True, room.display_name, repo if private else None)
            except Exception:                 # noqa: BLE001 - GPT review: one room's failure (unreadable, network,
                log.exception("couldn't check %s's address", room_id)   # gh) never stops the others; next start retries
                continue
            if note:
                log.info("address of %s:%s", room_id, note)

    async def close_for_private(self, room_id: str, repo: str) -> tuple[str | None, str]:
        """Before binding `repo` (under the lock): a room anyone can join (its real join rule, not my
        record) becomes listed if the repo is private; if that fails, the repo isn't set. (refusal, note)"""
        if not await project_rooms.repo_is_private(repo):
            return None, ""
        if unsafe := await project_rooms.history_safe(self.client, room_id):   # nobody outside reads its history
            return f"{unsafe} I haven't set the repo.", ""
        try:                                  # an address made before the bind never names the repo
            await room_address.keep_secret(self.client, room_id, self.room_name(room_id), repo)
        except (RuntimeError, OSError, asyncio.TimeoutError) as e:
            log.warning("couldn't check %s's address: %s", room_id, e)
            return "⚠️ I couldn't check this room's address just now, so I haven't set the repo; please try again.", ""
        if await project_rooms.join_rule(self.client, room_id) != "public":
            return None, ""
        failed = await project_rooms.apply(self.client, room_id, project_rooms.LISTED)
        if failed:
            return f"{failed} I haven't set the repo: a room on a private repo can't be open to anyone.", ""
        return None, "\nIts repo is private, so the room is now listed instead of open (people ask to join)."

    async def primary_room_for(self, repo: str, exclude: str | None = None) -> str | None:
        """The room that runs `repo` here (bound to it, not a separate discussion room): every room
        I'm in, read now (the poll's cache is empty right after a start)."""
        for rid in list(self.client.rooms):
            if rid == exclude or (rid in self.room_repos and (self.room_repos[rid] or "").lower() != repo.lower()):
                continue                      # a room the poll has seen on another repo (refreshed every poll)
            found, discussion = await room_state.binding_of(self.client, rid, strict=True)   # unreadable: raises
            if found and found.lower() == repo.lower() and not discussion:
                return rid
        return None

    def discussions(self) -> dict:
        """discussion room → the room that runs its repo (on disk; never in the rooms' own state)."""
        if "_discussions" not in self.__dict__:
            self._discussions = SavedDict(ROOT / "data" / "discussions.json")
        return self._discussions

    def room_name(self, room_id: str) -> str:
        room = self.client.rooms.get(room_id)
        return room.display_name if room is not None else "another room"

    def conflict_answer(self, room_id: str, sender: str, repo: str, other: str) -> str:
        """My answer when `repo` is already run from `other`; it carries its own offer (never a shared
        slot two replies could swap), and set_repo puts the two buttons under it."""
        return Answer(CONFLICT.format(repo=repo, ask=ASK, sep=SEPARATE),
                      {"repo": repo, "other": other, "by": sender, "room": room_id, "at": time.time()})

    async def offer_conflict_buttons(self, room_id: str, answer: str, event_id: str) -> None:
        offer = getattr(answer, "offer", None)
        if offer is None or self.__dict__.get("_bound_at", {}).get(room_id, 0) > offer["at"]:
            return                            # no question, or the room bound since: nothing to ask
        self.__dict__.setdefault("_conflicts", {})[event_id] = offer
        for key in (ASK, SEPARATE):
            await self.offer_thumb(room_id, event_id, key)

    def drop_conflict_offers(self, room_id: str) -> None:
        self.__dict__.setdefault("_bound_at", {})[room_id] = time.time()
        for eid in [e for e, o in self.__dict__.get("_conflicts", {}).items() if o["room"] == room_id]:
            self._conflicts.pop(eid, None)

    async def on_conflict_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        offer = self.__dict__.get("_conflicts", {}).get(event.reacts_to)
        if offer is None or offer["room"] != room.room_id:
            return False
        key = event.key.replace("️", "")
        if (key not in (ASK, SEPARATE) or time.time() - offer["at"] > OFFER_S
                or not await room_state.is_moderator(self.client, room, event.sender)):
            return True                       # the choice is the room's owners' (as choosing the repo is)
        self._conflicts.pop(event.reacts_to, None)
        if key == ASK:
            await self.ask_to_join(offer["other"], event.sender)
            await self.say(room.room_id, "I've asked that room's owners to let you in; its invite appears on the left.")
        else:                                 # re-checked from the start (moderator, access, the other room, under the lock)
            await self.set_repo_separately(room, event.sender, offer["repo"], offer["other"])
        return True

    async def set_repo_separately(self, room: MatrixRoom, sender: str, repo: str, other: str) -> None:
        answer = await self.choose_repo(room, sender, repo, separate_from=other)
        await self.offer_conflict_buttons(room.room_id, answer, await self.say(room.room_id, answer))   # a new conflict: new buttons

    async def on_project_reaction(self, room: MatrixRoom, event: ReactionEvent) -> bool:
        return (await self.on_join_reaction(room, event) or await self.on_conflict_reaction(room, event)
                or await self.on_find_reaction(room, event) or await self.on_repo_removal_reaction(room, event))
