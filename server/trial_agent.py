"""The trial agent (ADR 0025): ONE shared account this server runs, @hagency.trial-agent, labelled
"Trial agent (shared, limited)", for visitors who have no agent of their own yet. Talk only
(trial_turn.py answers; trial_model.py is the model): no code is run, no tools, no GitHub writes.

Who it talks to: only visitors who chose it (🧪 in their chat with @pm, "try the trial agent", or the
panel: pm_trial.py), recorded in data/trial.json, and only in rooms such a visitor brought it into
(its own private chat with them, or a room they invited it to: data/trial-rooms.json). When the last
visitor who brought it leaves a room, it leaves too. It ignores every agent, bot and @pm.

Never part of the loop: it is in nobody's agents.json line, so ownership.agents_in never lists it, so
@pm never picks it to build, review or merge, and never takes a result from it. Its name has a "."
that no person's agent name can have (identity.py), and ends in "-agent" so every other agent, and
@pm, already ignore what it says (and it posts notices, which agents don't read either).

It runs inside @pm's process with its own Matrix sign-in; started only when OLLAMA_API_KEY is set.
Messages sent while @pm restarts are not answered (it starts from "now"; it is a trial).
"""
import asyncio
import logging
import time
import urllib.parse
from types import SimpleNamespace

from aiohttp import ClientError
from nio import (AsyncClient, InviteMemberEvent, JoinResponse, LoginResponse, MatrixRoom, RoomMemberEvent,
                 RoomMessageText, RoomPreset, SyncResponse)

from common import lifecycle
from common import setup_accounts
from common.bridge_config import HOMESERVER, ROOT, SERVER_NAME
from common.saved_dict import SavedDict
from server import trial_budget
from server import trial_limits
from server import trial_models
from server.trial_turn import Turns

log = logging.getLogger("trial")
LOCALPART = "hagency.trial-agent"     # "." : never <name>-agent, <name>-<label>-agent or a hashed one (identity.py)
DISPLAY_NAME = "Trial agent (shared, limited)"
CHAT_NAME = "Trial agent"
INVITE_ONLY_ME = 100                  # only the trial agent adds people to its private chats
FRESH_CHAT_S = 600                    # a chat made this recently counts as theirs before sync shows it
HISTORY = {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "invited"}}


def user_id() -> str:
    return f"@{LOCALPART}:{SERVER_NAME}"


def hello(per_day: int) -> str:
    return (f"Hi! I'm the {DISPLAY_NAME}: an AI this server runs so you can try Hagency Rooms before bringing your own. "
            "I can only talk: ask me questions, discuss an idea or a plan, or have me read a pull request. I can't "
            "run code, use tools, open issues, build or merge, and what I say about a PR is never an approval.\n\n"
            "Here, just write. In a project room, invite me (Room info → Invite) and mention me, for example "
            f"\"Trial agent, what does PR #3 change?\". You have {per_day} messages a day (they reset at 00:00 UTC). "
            f"I run {trial_models.default()}; to pick another, type \"use <model>\" in your chat with Product Manager "
            f"({', '.join(trial_models.names())}), or choose it in My Hagency. "
            "What you write to me goes to Ollama Cloud to be answered (see the privacy page), so never send secrets.\n\n"
            "For your own agent, with tools and the GitHub loop: type \"bring my own\" in your chat with Product Manager.")


class TrialAgent(Turns):
    def __init__(self, client: AsyncClient, eligible, ask=None, usage=None, data_dir=None, budget=None, busy=None,
                 stopping: asyncio.Event | None = None) -> None:
        folder = data_dir or ROOT / "data"
        self.client, self.eligible, self.started_ms = client, eligible, int(time.time() * 1000)
        self.visitors = SavedDict(folder / "trial.json")          # visitor → {"dm", "since"}
        self.rooms = SavedDict(folder / "trial-rooms.json")      # room → [visitors who brought me there]
        self.usage = usage or trial_limits.Usage(folder / "trial-usage.json")
        self.budget = budget or trial_budget.Budget(folder / "trial-budget.json")   # the plan's month: never into pay-as-you-go
        self.slot = asyncio.Semaphore(1)  # one model call at a time: the key's plan allows 3, shared with others
        self.ask_model = ask
        self.history: dict = {}                                    # (room, visitor) → recent turns, in memory only
        self.answering: set = set()                                # visitors with an answer underway (trial_turn.py)
        self.enrolling: dict = {}                                  # visitor → one enrolment at a time
        self.waiting: set = set()                                  # answers underway (trial_turn.on_message)
        self.busy = busy if busy is not None else set()            # @pm's: a restart lets them finish (lifecycle.serve)
        self.stopping = stopping or asyncio.Event()                # @pm's SIGTERM: no new answers start

    def me(self) -> str:
        return self.client.user_id

    def running(self) -> bool:
        task = getattr(self, "task", None)
        return task is None or not task.done()     # None: driven by a test, not by start()

    def sponsors(self, room_id: str) -> list:
        return list(self.rooms.get(room_id) or [])

    def add_sponsor(self, room_id: str, visitor: str) -> None:
        if visitor not in self.sponsors(room_id):
            self.rooms[room_id] = [*self.sponsors(room_id), visitor]

    def dm_of(self, visitor: str) -> str | None:
        """Our private chat, while they're in it or invited, or made so recently sync may not show it yet."""
        entry = self.visitors.get(visitor) or {}
        dm, room = entry.get("dm"), self.client.rooms.get(entry.get("dm") or "")
        if room is None:
            return dm if dm and time.time() - float(entry.get("since") or 0) < FRESH_CHAT_S else None
        return dm if visitor in room.users or visitor in getattr(room, "invited_users", {}) else None

    async def enroll(self, visitor: str) -> str | None:
        """Make `visitor` one of mine and open (or reuse) our private chat; its room id, or None. One at a
        time per visitor (the panel and the chat at once make one chat: GPT review)."""
        async with self.enrolling.setdefault(visitor, asyncio.Lock()):
            dm = self.dm_of(visitor) or await self.open_chat(visitor)
            if dm and not (self.visitors.get(visitor) or {}).get("greeted"):   # a hello that failed is sent again
                await self.post(dm, hello(trial_limits.number("TRIAL_PER_VISITOR")))
                self.visitors[visitor] = {**self.visitors[visitor], "greeted": True}
            return dm

    async def open_chat(self, visitor: str) -> str | None:
        resp = await self.client.room_create(is_direct=True, invite=[visitor], preset=RoomPreset.private_chat,
                                             name=CHAT_NAME, initial_state=[HISTORY],
                                             power_level_override={"invite": INVITE_ONLY_ME})
        if not hasattr(resp, "room_id"):
            log.warning("couldn't open a trial chat with %s: %s", visitor, resp)
            return None
        self.visitors[visitor] = {**(self.visitors.get(visitor) or {}), "dm": resp.room_id, "since": time.time(),
                                  "greeted": False}   # their model kept
        self.add_sponsor(resp.room_id, visitor)
        return resp.room_id

    def model_of(self, visitor: str) -> str:
        return trial_models.chosen((self.visitors.get(visitor) or {}).get("model"))

    def set_model(self, visitor: str, name: str) -> bool:
        """Their choice of model, only an allowlisted one, kept with them; False: not allowed (or not a visitor)."""
        if visitor not in self.visitors or not trial_models.allowed(name):
            return False
        self.visitors[visitor] = {**self.visitors[visitor], "model": trial_models.chosen(name)}
        return True

    def is_visitor(self, user: str) -> bool:
        return user in self.visitors and self.eligible(user)

    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Join only when one of my visitors invites me; anything else is declined."""
        if event.state_key != self.me() or event.membership != "invite":
            return
        if not self.is_visitor(event.sender):
            await self.client.room_leave(room.room_id)    # declined: never in a room no visitor of mine asked for
            return
        joined = await self.client.join(room.room_id)
        if isinstance(joined, JoinResponse):
            self.add_sponsor(room.room_id, event.sender)
        else:
            log.warning("couldn't join %s for %s: %s", room.room_id, event.sender, joined)

    async def invites_while_away(self) -> None:
        """Invites that came while @pm was restarting: the same check as a live one."""
        for room in list(getattr(self.client, "invited_rooms", {}).values()):
            inviter = getattr(room, "inviter", None)
            if inviter:
                await self.on_invite(room, SimpleNamespace(state_key=self.me(), membership="invite", sender=inviter))

    async def reconcile(self) -> None:
        """At start: rooms whose visitors left while @pm was away (no leave event is replayed): their records
        go, and I leave a room nobody who brought me is still in (or invited to: a chat not yet accepted)."""
        for room_id in list(self.rooms):
            room = self.client.rooms.get(room_id)
            here = [v for v in self.sponsors(room_id) if room is not None
                    and (v in room.users or v in getattr(room, "invited_users", {}))]
            if here:
                if here != self.sponsors(room_id):
                    self.rooms[room_id] = here
                continue
            self.rooms.pop(room_id, None)
            if room is not None:
                await self.client.room_leave(room_id)

    async def on_member(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        """The last visitor who brought me here left: I leave too."""
        if event.membership not in ("leave", "ban") or event.state_key not in self.sponsors(room.room_id):
            return
        left = [v for v in self.sponsors(room.room_id) if v != event.state_key]
        if left:
            self.rooms[room.room_id] = left
            return
        self.rooms.pop(room.room_id, None)
        await self.client.room_leave(room.room_id)

    async def post(self, room_id: str, text: str) -> None:
        """A plain notice (agents and bots don't read notices; no markup from a model's words)."""
        resp = await self.client.room_send(room_id, "m.room.message", {"msgtype": "m.notice", "body": text})
        if not hasattr(resp, "event_id"):
            raise RuntimeError(f"couldn't post in {room_id}: {resp}")


async def sign_in() -> AsyncClient:
    """My account (made once, password in .env like @pm's), signed in, with my display name."""
    env = await asyncio.to_thread(setup_accounts.load_env)
    profile = await asyncio.to_thread(setup_accounts.api, "GET", f"/_matrix/client/v3/profile/{urllib.parse.quote(user_id())}")
    # No account although a password is saved (its first registration failed, or a fresh database): verify
    # signs in with it and registers it again if that fails (GPT review), as setup_accounts.ensure_pm does.
    password, _ = await asyncio.to_thread(setup_accounts.ensure_user, env, LOCALPART, setup_accounts.shared_secret(),
                                          False, "_status" in profile)
    client = AsyncClient(HOMESERVER, user_id(), device_id="TRIAL_AGENT")
    resp = await lifecycle.login(client, password, device_name="trial")
    if not isinstance(resp, LoginResponse):
        await client.close()
        raise setup_accounts.AccountError(f"the trial agent couldn't sign in: {resp}")
    await client.set_displayname(DISPLAY_NAME)
    return client


async def run(agent: TrialAgent) -> None:
    client = agent.client
    try:
        first = await client.sync(timeout=0, full_state=True)   # from now: no history is answered
        if not isinstance(first, SyncResponse):               # never reconcile against an empty view (GPT review)
            raise RuntimeError(f"the trial agent's first sync failed: {first}")
        agent.started_ms = int(time.time() * 1000)
        client.add_event_callback(agent.on_invite, InviteMemberEvent)
        client.add_event_callback(agent.on_member, RoomMemberEvent)
        client.add_event_callback(agent.on_message, RoomMessageText)
        await agent.reconcile()                               # first: an invite joined just now isn't synced yet
        await agent.invites_while_away()
        await client.sync_forever(timeout=30_000)
    finally:
        for task in list(agent.waiting):                      # sync ended: nothing more goes out through it
            task.cancel()
        await client.close()


def start_later(loop) -> None:
    """Sign in in the background and set loop.trial once online: a rate-limited sign-in (up to 15 min)
    must never hold up @pm itself (GPT review). Until then the trial simply isn't offered."""
    if not trial_limits.enabled():
        return

    async def online() -> None:
        loop.trial = await start(loop.trial_eligible, loop.busy, loop.sync_point.stop)
    loop.trial_starting = asyncio.create_task(online())


async def start(eligible, busy=None, stopping=None) -> TrialAgent | None:
    """The trial agent, running, or None (no key: off; a failure is logged and the offer stays hidden,
    since @pm only offers it while this returned an agent)."""
    if not trial_limits.enabled():
        return None
    try:
        client = await sign_in()
    except (setup_accounts.AccountError, OSError, ClientError, asyncio.TimeoutError, AttributeError) as e:
        log.warning("trial agent not started: %s", e)    # optional feature: off, and never offered (see docstring)
        return None
    agent = TrialAgent(client, eligible, busy=busy, stopping=stopping)
    agent.task = asyncio.create_task(run(agent))
    # A crash ends the task: running() turns False, so @pm stops offering it and says it's unavailable.
    agent.task.add_done_callback(lambda t: t.cancelled() or not t.exception() or log.error("trial agent stopped: %s", t.exception()))
    log.info("trial agent online as %s (models %s)", user_id(), ", ".join(trial_models.names()))
    return agent
