"""A room's settings, changed only through here (chat and the console both call these):
  - its repo: one room, one repo, chosen by the owner or a maintainer ("@pm this room works on
    owner/name" or the console). Stored as a list in the room's LEVEL_EVENT for multi-repo later;
    a project that spans repos is a Matrix Space with one room per repo;
  - its mode (off / assist / autonomous): stopping is open to anyone, never binds a room;
  - the one-time console link, sent in a private room (it is a credential).
Mixed into loop.Loop (client, user_id, say, level, state, room_repos, pending, dm_rooms, kick).
"""
import asyncio
import logging
import urllib.parse

from nio import MatrixRoom, RoomPreset

from server import console
from common import github
from common import github_app
from server import github_link
from server import github_setup
from common import registry
from server import repo_refusals
from common import room_state
from server.loop_progress import ACTIVE
from server.pm_projects import RUN_ELSEWHERE, Projects
from server.project_rooms import plain
from common.setup_accounts import DISPLAY_NAMES

log = logging.getLogger("loop")
# Codes and console links go to my private chats: only I can invite anyone there, and someone
# invited later can't read what was said before their invite (GPT review 23). "invited", not
# "joined": the person reads my offer, sent while they were still invited.
HISTORY = "invited"
PRIVATE_HISTORY = {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": HISTORY}}
PM_ONLY = 100
CHAT_NAMES = (DISPLAY_NAMES["pm"], "Hagency (private)")   # my private chats' names, now and before
NO_REPO = ("This room has no GitHub repo yet. Say \"@pm this room works on owner/name\" (owner or maintainer), "
           "or pick it in the Hagency panel above.")
INSTALL_RECHECK_S = 30        # after an install link: look again this often...
INSTALL_RECHECKS = 20         # ...this many times (10 minutes), then the person asks again
BUSY = "Work is still running on the current repo; say \"stop\" and let it finish first."
KEEP = object()               # write_level: keep the room's "only discusses its repo" as it is
NOT_MODERATOR = "Only the room owner or a maintainer can choose this room's repo."
NEED_POWER = ("I can't save this room's settings yet: Matrix lets only moderators do that. Room owner, please "
              "make me a Moderator (Room settings → Roles & Permissions → @pm → Moderator), then ask again.")
STOPPED_MEANWHILE = "Someone said stop just now, so I didn't change the mode."

NOT_CONNECTED = ("First connect your GitHub account, so I know it's yours: click **Connect GitHub** in the Hagency "
                 "panel, or say \"connect my github\" in our private chat. Then ask again.")


async def may_write(user: str, repo: str) -> bool:
    """The person can write to `repo`: so binding a room to a repo needs real access to it, not just a
    room of one's own (anyone can create a room). With the GitHub App (the public server) that is the
    GitHub account they connected (github_link.py, ADR 0007), read as the App; else their own agent's
    GitHub identity (local dev: gh). Reads only; nothing is changed."""
    if github_app.configured():
        return await github_link.may_write(user, repo)
    agent = registry.agent_of(user)           # by full ID: @anna:elsewhere is not our anna
    if not registry.owns(user, agent):
        return False
    return bool((await github_setup.identity(agent, repo)).get("push"))


def is_safe_chat(room: MatrixRoom) -> bool:
    """One of my private chats made with the safeguards above (older ones are replaced, not reused)."""
    return getattr(room, "history_visibility", "") == HISTORY and room.power_levels.defaults.invite >= PM_ONLY


def is_my_chat(room: MatrixRoom, me: str, dm_rooms: dict) -> bool:
    """One of my private chats with someone (now or an older one): never a project room."""
    return len(room.users) <= 2 and me in room.users and (room.room_id in dm_rooms.values()
                                                           or getattr(room, "name", None) in CHAT_NAMES)


class Settings(Projects):
    async def set_repo(self, room: MatrixRoom, sender: str, repo: str) -> str:
        """One room, one repo: the owner or a maintainer names it; never swapped under running work.
        Says the answer in the room and returns it (the console shows it too)."""
        answer = await self.choose_repo(room, sender, repo)
        await self.offer_conflict_buttons(room.room_id, answer, await self.say(room.room_id, answer))   # pm_projects.py
        return answer

    async def bind_checked(self, rid: str, sender: str, repo: str, separate_from: str | None, proof=None) -> str:
        """Under the lock: one project room per repo, a private repo's room never open, then bind.
        proof: the GitHub link the write check used; a Disconnect (or a new account) since then refuses."""
        if self.busy_on_other_repo(rid, repo):    # before anything changes (closing the room included)
            return BUSY
        try:
            other = await self.primary_room_for(repo, exclude=rid)   # pm_projects.py: every room, read now
        except room_state.StateUnreadable as e:   # never a second primary on a guess (GPT review)
            log.warning("one-room-per-repo check failed: %s", e)
            return "I couldn't check the other rooms on this repo just now; please try again in a minute."
        if other and other != separate_from:
            return self.conflict_answer(rid, sender, repo, other)
        problem = await self.requester_problem(rid, (sender, proof), repo)   # before the privacy writes too
        if problem:
            return problem
        refused, note = await self.close_for_private(rid, repo)
        return refused or self.solo_bound(rid, sender, repo, await self.bind(rid, repo, other, (sender, proof)) + note)

    async def requester_problem(self, rid: str, requester: tuple, repo: str) -> str | None:
        """At the state write (GPT review, batch 5+6): the requester is still joined and a moderator,
        read now (an install wait outlives a departure), and still has the GitHub link the write
        check read (a Disconnect while @pm awaited the privacy check or the state read refuses)."""
        sender, proof = requester
        try:
            if not await room_state.joined_moderator(self.client, rid, sender):
                return NOT_MODERATOR
        except room_state.StateUnreadable as e:  # never bind on a guess
            log.warning("requester check before binding %s failed: %s", rid, e)
            return "I couldn't check this room just now; please try again in a minute."
        if github_app.configured() and github_link.link_of(sender) != proof:
            return repo_refusals.LINK_CHANGED
        return None

    async def choose_repo(self, room: MatrixRoom, sender: str, name: str, separate_from: str | None = None) -> str:
        rid, repo = room.room_id, github.allowed_repo(name)
        if not await room_state.is_moderator(self.client, room, sender):
            return NOT_MODERATOR
        if not github.REPO_RE.match(name):
            return "Tell me the repo as owner/name, e.g. \"@pm this room works on octocat/hello\"."
        if github_app.configured() and github_link.link_of(sender) is None:
            return NOT_CONNECTED                  # before the repo is looked at: says nothing about it
        if repo is None:                          # only when the operator lists repos (github.allowed_repo)
            return repo_refusals.not_listed(name)
        proof = dict(github_link.link_of(sender) or {}) or None   # what the write check reads (ADR 0007)
        try:
            allowed = await may_write(sender, repo)
        except github_app.NotInstalled:           # access is read as the App: not until it's there
            self.watch_install(room, sender, repo)
            return repo_refusals.install(repo)    # the one missing step (repo_refusals.py: confirms nothing)
        if not allowed:                           # a private repo (or GitHub can't say): the install words
            told = await repo_refusals.cant_write(sender, repo)   # too, and a wait that stays quiet unless
            if told is None:                      # it can bind (repo_refusals.py: never an existence oracle)
                self.watch_install(room, sender, repo)
            return told or repo_refusals.install(repo)
        missing = await self.app_missing(room, sender, repo)
        if missing:
            return missing
        if github.any_repo():                     # no list to spell it: GitHub's own spelling, one key everywhere
            repo = await github_app.full_name(repo)
        self.stop_install_watch(rid)              # this choice is final: an older install wait ends
        async with self.lock:                     # the same lock as polling and results: nothing starts in between
            binding = asyncio.ensure_future(self.bind_checked(rid, sender, repo, separate_from, proof))
            try:
                return await asyncio.shield(binding)
            except asyncio.CancelledError:        # an install wait cancelled mid-bind: the bind (Matrix state
                await binding                     # and @pm's own records) finishes whole, under the lock
                raise

    async def app_missing(self, room: MatrixRoom, sender: str, repo: str) -> str | None:
        """None when @pm can read `repo`; else the one-click install link (and I look again after)."""
        try:
            if await github_app.installed(repo):
                return None
        except github.GitHubError as e:          # its text never carries the key or a token
            return f"I can't check {repo} on GitHub right now ({str(e)[:160]}); please try again in a minute."
        self.watch_install(room, sender, repo)
        return repo_refusals.install(repo)

    def watch_install(self, room: MatrixRoom, sender: str, repo: str) -> None:
        """Look for the App on `repo` for a while, then choose it again for them (await_install)."""
        watch = self.install_watches.get(room.room_id)
        if watch is None or watch[0] != repo or watch[1].done():   # a newer choice replaces an older wait
            self.stop_install_watch(room.room_id)
            self.install_watches[room.room_id] = (repo, asyncio.create_task(self.await_install(room, sender, repo)))

    async def await_install(self, room: MatrixRoom, sender: str, repo: str) -> None:
        """After the install words: once the App is on `repo` and the person can write there, choose it
        again for them (every check re-runs). Otherwise it ends quietly, saying only what a public repo
        allows (repo_refusals.cant_write): an answer that differs would confirm a private repo. The wait
        stays registered until it ends, so a newer choice in the room cancels it even during that last
        bind (GPT review, GitHub App)."""
        try:
            for _ in range(INSTALL_RECHECKS):
                await asyncio.sleep(INSTALL_RECHECK_S)
                try:
                    if await github_app.installed(repo):
                        break
                except github.GitHubError as e:  # GitHub unreachable for a moment: the next look may work
                    log.warning("install re-check for %s: %s", repo, str(e)[:160])
            else:
                return                            # never installed: they were told the step once
            if not await self.may_bind_now(room, sender, repo):
                return
            await self.set_repo(room, sender, repo)   # every check again, and the answer
        finally:
            if self.install_watches.get(room.room_id, ("", None))[1] is asyncio.current_task():
                del self.install_watches[room.room_id]     # (a newer wait may have replaced this one)

    async def may_bind_now(self, room: MatrixRoom, sender: str, repo: str) -> bool:
        """The App is there: true if they can write; else only a public repo's refusal is said."""
        try:
            if await may_write(sender, repo):
                return True
        except github_app.NotInstalled:           # gone again already: quiet, they can ask again
            return False
        told = await repo_refusals.cant_write(sender, repo)
        if told:
            await self.say(room.room_id, told)
        return False

    def stop_install_watch(self, rid: str) -> None:
        watch = self.install_watches.get(rid)
        if watch is None or watch[1] is asyncio.current_task():   # the wait itself binding: it stays registered
            return
        del self.install_watches[rid]
        watch[1].cancel()

    def busy_on_other_repo(self, rid: str, repo: str) -> bool:
        return any(v.get("room") == rid and v.get("repo") != repo and v["stage"] in ACTIVE for v in self.state.values())

    async def bind(self, rid: str, repo: str, runs_in: str | None = None, requester: tuple | None = None) -> str:
        """requester: (who asked, their GitHub link then), re-checked at the state write."""
        if self.busy_on_other_repo(rid, repo):
            return BUSY
        current = await room_state.repo_of(self.client, rid)      # the binding itself, not the poll's cache
        keep = current == repo and runs_in is None
        failed = await self.write_level(rid, None if keep else "off", repo, runs_in is not None,
                                        requester=requester)      # None: as it is, read under the lock
        if failed:
            return failed
        if runs_in:                               # which room runs it: mine to know, not this room's state
            self.discussions()[rid] = runs_in
        elif rid in self.discussions():           # (no write when there's nothing to forget)
            self.discussions().pop(rid, None)
        if current != repo:                       # a plan made for the old repo is never filed in the new one
            self.pending.pop(rid, None)
            self.plans.pop(rid, None)
            self.last_plans.pop(rid, None)
        self.room_repos[rid] = repo
        self.drop_conflict_offers(rid)            # pm_projects.py: an older 🙋/➕ question is settled
        moved = current and current.lower() != repo.lower()   # from a private repo: never open after (pm_unbind)
        past = (await self.mark_private_past(rid, current) if moved else None) or ""
        if runs_in:
            return f"This room now discusses **{repo}**; its build loop runs in the room that already works on it.{past}"
        return (f"This room now works on **{repo}**. Check that it is ready (the agents' write access, the merge "
                f"rule and the agent-ok label) with **Check readiness** in the Hagency panel above.{past}")

    async def send_console_link(self, room: MatrixRoom, sender: str) -> None:
        """A one-time sign-in link to the console for this room, sent privately (it's a credential)."""
        dm = await self.private_room(sender)
        if dm is None:
            await self.say(room.room_id, "I couldn't open a private chat with you; the console is also in this "
                                         "room's Hagency panel (room info → Extensions).")
            return
        code = console.new_link(sender, room.room_id)               # the session it gives is for this room only
        url = f"{console.public_url()}/console?room={urllib.parse.quote(room.room_id)}#login={code}"
        await self.say(dm, f"Your Hagency console for **{plain(room.display_name)}** (works once, for 10 minutes): {url}")
        await self.say(room.room_id, f"Sent you a private console link, {sender.split(':')[0].lstrip('@')}.")

    async def private_room(self, user: str) -> str | None:
        known = self.dm_rooms.get(user)
        room = self.client.rooms.get(known) if known else None
        if room is not None and set(room.users) <= {self.user_id, user} and is_safe_chat(room):
            return known                      # still just the two of us (joined or invited)
        # else a new chat: an older one ("Hagency (private)") lets them invite others, who'd read back
        resp = await self.client.room_create(is_direct=True, invite=[user], preset=RoomPreset.private_chat,
                                             name=DISPLAY_NAMES["pm"], initial_state=[PRIVATE_HISTORY],
                                             power_level_override={"invite": PM_ONLY})
        if not hasattr(resp, "room_id"):
            log.warning("couldn't open a DM with %s: %s", user, resp)
            return None
        self.dm_rooms[user] = resp.room_id
        return resp.room_id

    async def change_level(self, room_id: str, level: str, gen: int | None = None) -> str | None:
        """Write the room's loop level (an error line, or None). A mode change drops a confirmation still waiting; a stop,
        from anywhere, invalidates everything in flight (pm_understand.invalidate); gen: a stop since then wins."""
        if level == "off":
            self.invalidate(room_id)
        elif self.pending.get(room_id, {}).get("kind", "autonomous") in ("autonomous", "mode", "implicit"):   # pm_implicit.py
            self.pending.pop(room_id, None)
        return await self.write_level(room_id, level, unbound_ok=level == "off", gen=gen)   # stopping never binds a room

    async def write_level(self, room_id: str, level: str | None, repo: str | None = None, discussion=KEEP,
                          unbound_ok: bool = False, requester: tuple | None = None, gen: int | None = None) -> str | None:
        """Write the room's level (and repo): callers have checked that a moderator asked. A room that only
        discusses a repo another room runs never leaves "off". Every binding write reads and writes under
        one short lock (not the poll's), so a "stop" can't write back a repo a rebind just replaced."""
        async with self.__dict__.setdefault("_binding_lock", asyncio.Lock()):
            bound_repo, bound_discussion = await room_state.binding_of(self.client, room_id)   # one read
            repo = repo or bound_repo
            if repo is None:
                return None if unbound_ok else NO_REPO
            level = level or await self.level(room_id)   # "keep the level": the one in force now, not before the lock
            only_discusses = bound_discussion if discussion is KEEP else discussion
            if only_discusses and level != "off":
                return RUN_ELSEWHERE
            if problem := requester and await self.requester_problem(room_id, requester, repo):   # the last await before the write
                return problem
            if gen is not None and self.generation.get(room_id, 0) != gen:
                return STOPPED_MEANWHILE      # GPT review: a stop while the mode change was checked wins
            resp = await self.client.room_put_state(room_id, github.LEVEL_EVENT, room_state.content(level, repo, only_discusses))
        if not hasattr(resp, "event_id"):
            if getattr(resp, "status_code", "") == "M_FORBIDDEN":    # a room someone made: I joined at power 0
                return NEED_POWER
            return f"⚠️ I couldn't save this room's settings: {resp}"
        self.kick()                           # start (or stop) now, not at the next poll
        return None
