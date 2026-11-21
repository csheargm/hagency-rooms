"""pm: the autonomous GitHub loop, coordinated in a Matrix room.

Watches open `agent-ok` issues on each room's own repo, per room level (xyz.hagencylabs.loop; pm_settings.py):
  assign a builder agent → it claims the issue and opens a PR → ask a DIFFERENT person's agent
  to review → when GitHub itself reports the PR mergeable+approved: Assist posts
  "ready — a person merges", Autonomous asks the builder agent to merge.
pm holds no GitHub credentials: it only reads (ambient gh) and talks to agents.

Usage: python loop.py
"""
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import markdown
from nio import (AsyncClient, RoomMessageAudio, InviteMemberEvent, LoginResponse, MatrixRoom, ReactionEvent, RoomMemberEvent,
                 RoomMessageText, RoomSendResponse)

from common import github
from common import lifecycle
from server import ownership
from server import panel
from common.redact import room_safe
from common import room_state
from server.loop_progress import GITHUB_STAGES, Progress, migrate, num
from server.pm_chat import Chat
from server.pm_settings import Settings
from server.pm_follow import Follow
from common.saved_dict import SavedDict
from server.pm_plan import Planner
from server.pm_presence import Presence
from server.pm_understand import Understanding
from server.pm_voice import Voice
from server.pm_welcome import Welcome
from server.pm_agents import MoreAgents
from server import routing
from server.status_board import Boards
from common.bridge_config import HOMESERVER, ROOT, SERVER_NAME, read_env
from common.lifecycle import is_stale

POLL_S = float(os.environ.get("LOOP_POLL_S", "30"))
FAST_POLL_S = min(POLL_S, 5.0)        # while waiting on GitHub (mergeability, a merge, a close)
GITHUB_WAITS = (*GITHUB_STAGES, "merging", "closing")
STATE_FILE = Path(os.environ.get("LOOP_STATE", ROOT / "loop-state.json"))   # git-ignored; per-issue progress
EXPECTED_STAGE = {"build": "building", "review": "reviewing", "merge": "merging", "fix": "fixing",
                  "verify": "verifying", "close": "closing"}
CHECKER_KINDS = ("review", "verify")      # results only the second agent may report

log = logging.getLogger("loop")


class SendError(RuntimeError):
    """Matrix refused a message; the item keeps its stage and the watchdog retries later."""


class Loop(Progress, Boards, Chat, Settings, Planner, Understanding, Follow, Voice, Welcome, Presence, MoreAgents):
    def __init__(self) -> None:
        self.user_id = f"@{github.PM_BOT}:{SERVER_NAME}"
        self.client = AsyncClient(HOMESERVER, self.user_id, device_id="PM_BOT")
        self.state: dict = migrate(json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {})
        self.started_ms = int(time.time() * 1000)
        self.sync_point = lifecycle.SyncPoint(self.client)   # restart hand-off position
        # One transition at a time: results and polling never interleave on an item, and a
        # restart lets an in-flight transition finish (it is in `busy`) instead of cutting it.
        self.lock = asyncio.Lock()
        self.busy: set[asyncio.Task] = set()
        self.board_file = STATE_FILE.with_name(STATE_FILE.stem + "-boards.json")   # git-ignored
        self.boards: dict = self.load_boards()      # room → pinned status message (status_board.py)
        self.open_issues: dict[str, list] = {}      # room → queued issues, for the board
        # room → autonomous switch or plan awaiting ✅ (pm_chat.py); kept on disk so a restart (e.g. a
        # live code fix) doesn't drop a plan someone is about to approve
        self.pending: dict = SavedDict(STATE_FILE.with_name(STATE_FILE.stem + "-pending.json"))
        self.plans: dict = {}                       # room → planning task in flight (pm_plan.py)
        self.install_watches: dict = {}             # room → (repo, task) waiting for the GitHub App (pm_settings.py)
        self.generation: dict = {}                  # room → bumped by "stop" (pm_chat.py)
        self.room_repos: dict = {}                  # room → its repo (room_state), refreshed every poll
        self.dm_rooms: dict = SavedDict(STATE_FILE.with_name(STATE_FILE.stem + "-dms.json"))  # person → our DM
        self.last_plans: dict = SavedDict(STATE_FILE.with_name(STATE_FILE.stem + "-plans.json"))

    def save(self) -> None:
        for item in self.state.values():            # when an item finished, for the board's "last 24 h"
            if item["stage"] in ("done", "stopped") and "ended" not in item:
                item["ended"] = time.time()
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, STATE_FILE)

    async def dispatch(self, room_id: str, item: dict, agent: str, body: str, task: dict,
                       resend: bool = False) -> None:
        """Send a task to one agent; only a result echoing one of this step's task_ids advances it.
        A resend adds an id (a slow original's result still counts); a new step starts afresh."""
        task_id = uuid.uuid4().hex
        ids = [*item.get("task_ids", []), task_id] if resend else [task_id]
        # agent/body/task are kept so the watchdog can resend a step that never got an answer.
        item.update(task_id=task_id, task_ids=ids, agent=agent, body=body, task=task, sent_at=time.time(),
                    sends=item.get("sends", 1) + 1 if resend else 1)
        if await self.level(room_id) == "off":
            item["held"] = True       # stopped: nothing new goes out; tick sends it when the room resumes
            self.save()
            return
        item.pop("held", None)
        self.save()
        await self.say(room_id, body, agent, {**task, "task_id": item["task_id"], "repo": item["repo"]})

    async def say(self, room_id: str, body: str, mention: str | None = None, task: dict | None = None,
                  plain: bool = False, reply_to: str | None = None) -> str:
        if task is not None:          # the one thing every agent surely reads; bridges use the task field
            body += " — use your hagency tools: call next_task"
        content = {"msgtype": "m.notice" if task is None else "m.text", "body": body}
        source = github.linkify(body, (task or {}).get("repo") or self.room_repos.get(room_id))
        if mention:
            content["m.mentions"] = {"user_ids": [mention]}
            source = source.replace(mention, f"[{mention.split(':')[0]}](https://matrix.to/#/{mention})")  # a pill
        if not plain:                 # plain: a model's words, shown as text (no links or formatting)
            content.update(format="org.matrix.custom.html", formatted_body=markdown.markdown(source))
        if task is not None:
            content[github.TASK_KEY] = {**task, "agent": mention}         # task["repo"]: the room's repo
        if reply_to:                  # attached to that message (e.g. a voice message's transcript)
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        resp = await self.client.room_send(room_id, "m.room.message", content)
        if not isinstance(resp, RoomSendResponse):
            raise SendError(f"could not post to {room_id}: {resp}")
        return resp.event_id

    async def level(self, room_id: str) -> str:
        return await room_state.level(self.client, room_id)

    def agents_in(self, room: MatrixRoom) -> list[str]:
        return ownership.agents_in(room)      # registered agents whose owner is here too

    # --- chat: agent results -----------------------------------------
    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if is_stale(event, self.started_ms) or not self.sync_point.first_time(event.event_id):
            return
        result = event.source.get("content", {}).get(github.RESULT_KEY)
        if isinstance(result, dict) and event.sender in self.agents_in(room):
            await self.guarded(self.take_result(room, event.sender, result))
        elif self.is_person(room, event.sender):
            try:
                await self.on_welcome_text(room, event) or await self.on_human(room, event)   # pm_welcome.py first
            except (SendError, github.GitHubError) as e:
                log.error("couldn't answer %s: %s", event.sender, e)
                try:
                    await self.say(room.room_id, f"⚠️ I couldn't do that just now ({room_safe(e)}); please ask again")
                except SendError:
                    pass   # Matrix itself is refusing; the log line above is all we can do

    async def on_reaction_event(self, room: MatrixRoom, event: ReactionEvent) -> None:
        if not is_stale(event, self.started_ms) and self.is_person(room, event.sender):
            try:
                await self.on_welcome_reaction(room, event) or await self.on_reaction(room, event)
            except SendError as e:
                log.error("couldn't confirm for %s: %s", event.sender, e)   # they can react again

    def is_person(self, room: MatrixRoom, user_id: str) -> bool:
        """A joined human: not me, not an agent (agents never change how the room runs)."""
        local = user_id.split(":")[0]
        return (user_id != self.user_id and not local.endswith("-agent") and not local.endswith("-bot")
                and user_id in room.users and user_id not in room.invited_users)

    async def take_result(self, room: MatrixRoom, sender: str, result: dict) -> None:
        async with self.lock:
            try:
                await self.on_result(room, sender, result)
            except (KeyError, TypeError, ValueError, SendError, github.GitHubError) as e:
                # A malformed result (or GitHub briefly unreachable) from a room member must not kill the sync loop; the item
                # stays at its stage and a person can see the log line and take over.
                log.error("bad result from %s: %s", sender, e)
            await self.refresh_boards()
        self.kick()                           # its GitHub follow-up (merge, next issue) now, not at the poll

    async def guarded(self, coro) -> None:
        """Run a transition so a restart can't cut it in half: it finishes even if we are cancelled."""
        job = asyncio.create_task(coro)
        self.busy.add(job)
        job.add_done_callback(self.busy.discard)
        await asyncio.shield(job)

    async def on_result(self, room: MatrixRoom, sender: str, result: dict) -> None:
        kind = result.get("kind")
        if kind in ("plan", "file_issues"):
            await self.on_plan_result(room, sender, result)
            return
        if room.room_id not in self.room_repos:     # before the first poll: read it (only @pm writes it)
            self.room_repos[room.room_id] = await room_state.repo_of(self.client, room.room_id)
        key = self.key_for_result(room.room_id, result)
        item = self.state.get(key)
        if not isinstance(kind, str) or not item or item.get("repo") != self.room_repos.get(room.room_id):
            return
        if item["stage"] != EXPECTED_STAGE.get(kind) or result.get("task_id") not in item.get("task_ids", [item.get("task_id")]):
            return   # late/duplicate result for a step that's no longer current
        if sender != item.get("reviewer" if kind in CHECKER_KINDS else "builder") or (
                kind in CHECKER_KINDS and sender not in routing.checkers_for([sender], item["builder"], ownership.owner_id)):
            return   # only the agent the step was given to, and a checker only of ANOTHER person's work
        if result.get("error"):
            await self.stop(room.room_id, key, item, f"{sender.split(':')[0][1:]} couldn't {kind} it")
        elif kind == "build" and result.get("already_done"):
            item["stage"] = "verifying"     # the builder's "nothing to do" needs a second agent's check
            if await self.pick_reviewer(room, item, key, f"verify #{num(key)} is already done"):
                await self.dispatch(room.room_id, item, item["reviewer"], f"{item['reviewer']}: verify #{num(key)} is already done",
                                    {"kind": "verify", "issue": num(key)})
        elif kind == "verify":
            if result.get("verdict") == "approve":
                item["stage"] = "confirmed_done"
            else:
                await self.stop(room.room_id, key, item, "the agents disagree on whether it's already done")
        elif kind == "close":
            item["stage"] = "close_sent"    # done only once GitHub itself reports CLOSED
        elif kind == "build":
            item.update(stage="reviewing", pr=int(result["pr"]))
            if not await self.pr_is_builders(item, key):
                await self.stop(room.room_id, key, item, f"PR #{item['pr']} isn't {item['builder']}'s branch")
                return
            if await self.pick_reviewer(room, item, key, f"review PR #{item['pr']}"):
                await self.dispatch(room.room_id, item, item["reviewer"],
                                    f"{item['reviewer']}: review PR #{item['pr']} against #{num(key)}",
                                    {"kind": "review", "issue": num(key), "pr": item["pr"]})
        elif kind in ("review", "fix") and int(result["pr"]) == item["pr"]:
            await self.after_review_step(room.room_id, item, key, result)      # loop_progress.py
        elif kind == "merge":
            item["stage"] = "merge_sent"    # done only once GitHub itself reports MERGED
        self.save()

    async def pick_reviewer(self, room: MatrixRoom, item: dict, key: str, what: str) -> bool:
        """Choose a DIFFERENT person's agent as the checker; stop the item if there is none."""
        others = routing.checkers_for(routing.available(self.agents_in(room), self.agent_online), item["builder"], ownership.owner_id)
        if not others:
            await self.stop(room.room_id, key, item, f"there's no second person's agent here to {what.split()[0]} it")
            return False
        item["reviewer"] = others[num(key) % len(others)]
        await self.say(room.room_id, f"{what.split()[0]} requested from {item['reviewer'].split(':')[0][1:]}")
        return True

    async def pr_is_builders(self, item: dict, key: str) -> bool:
        """The builder reports a PR number; check it is really its own branch for this issue."""
        info = await github.run_json(["pr", "view", str(item["pr"]), "-R", item["repo"], "--json",
                                      "headRefName,isCrossRepository,state"])
        branch = f"agent/{item['builder'].split(':')[0][1:]}/issue-{num(key)}"
        return info.get("headRefName") == branch and not info.get("isCrossRepository") and info.get("state") == "OPEN"

    async def run(self) -> None:
        resp = await lifecycle.login(self.client, read_env()["PW_PM"], device_name="pm")   # waits out a 429
        if not isinstance(resp, LoginResponse):
            raise SystemExit(f"pm login failed: {resp}")
        self.client.add_event_callback(self.on_invite, InviteMemberEvent)   # pending invites too (pm_follow.py)
        await lifecycle.first_sync(
            self.client, github.PM_BOT, self.sync_point,
            lambda: (self.client.add_event_callback(self.on_message, RoomMessageText),
                     self.client.add_event_callback(self.on_reaction_event, ReactionEvent),
                     self.client.add_event_callback(self.on_voice, RoomMessageAudio),
                     self.client.add_event_callback(self.on_member, RoomMemberEvent),
                     self.client.add_event_callback(self.on_welcome_member, RoomMemberEvent)),
            lambda ms: setattr(self, "started_ms", ms))
        log.info("pm online (one repo per room; poll %ss)", POLL_S)
        await self.ensure_welcome_room()     # pm_welcome.py: where every new account starts
        await panel.start(self)              # the side panel (panel.py); never fatal
        await lifecycle.serve(self.client, github.PM_BOT, self.sync_point, self.busy, side=self.poll_forever())

    async def poll_forever(self) -> None:
        """Check GitHub every POLL_S, or at once when kick() says something changed."""
        self.wake = asyncio.Event()
        self.addressing = asyncio.create_task(self.guarded(self.at_start()))   # status_board.py: background, finishes on shutdown
        while True:
            await self.guarded(self.locked_tick())
            self.wake.clear()
            waiting_on_github = any(v["stage"] in GITHUB_WAITS for v in self.state.values())
            try:
                await asyncio.wait_for(self.wake.wait(), FAST_POLL_S if waiting_on_github else POLL_S)
            except asyncio.TimeoutError:
                pass                          # the regular poll

    def kick(self) -> None:
        """Act now instead of at the next poll (after a ✅, a mode change or new issues)."""
        if (wake := getattr(self, "wake", None)) is not None:
            wake.set()

    async def locked_tick(self) -> None:
        async with self.lock:
            try:
                await self.tick()
            except (github.GitHubError, SendError) as e:
                log.error("tick failed: %s", e)   # transient gh/network/Matrix errors: retry next tick
            await self.refresh_boards()
            await self.check_agents()             # pm_presence.py: "your agent is offline"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [pm] %(message)s")
    logging.getLogger("nio").setLevel(logging.WARNING)
    try:
        asyncio.run(Loop().run())
    except KeyboardInterrupt:
        pass  # Ctrl-C is the normal way to stop the loop


if __name__ == "__main__":
    main()
