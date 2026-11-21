"""Graceful restarts for the agent adapters and pm (driven by supervise.py).

On SIGTERM a process stops syncing, writes its Matrix sync position to a hand-off file, finishes
the turns/tasks it already started, and exits. The next process resumes syncing from there, so
messages sent during the restart are handled exactly once instead of being skipped as history.

The saved position is the last sync batch whose events were ALL handled, plus the ids handled
after it: matrix-nio advances client.next_batch before it runs event callbacks, so saving
next_batch after a mid-batch stop would silently skip the rest of that batch.
"""
import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Callable, Coroutine
from pathlib import Path

from nio import AsyncClient, LoginResponse, SyncError, SyncResponse
from common import paths

# git-ignored (rooms/); tests point this elsewhere so their hand-offs never reach the real agent adapters
HANDOFF_DIR = Path(os.environ.get("HANDOFF_DIR", paths.ROOT / "rooms" / ".handoff"))
DRAIN_TIMEOUT_S = 1200           # agent turn limit (600 s) plus its gh/git calls
HANDOFF_MAX_AGE_S = DRAIN_TIMEOUT_S + 300   # older hand-offs are ignored: treat it as a cold start
CLOCK_SKEW_MS = 5_000            # tolerance when dropping events from before startup
SYNC_SLACK_MS = 60_000           # events since the token are at most one long-poll older than it

log = logging.getLogger("lifecycle")


async def communicate(proc: asyncio.subprocess.Process, data: bytes | None, timeout_s: float) -> tuple[bytes, bytes]:
    """proc.communicate() that never leaves the child running: on timeout OR cancellation (a
    restart's drain ending, polling being cancelled) the child is killed and reaped first."""
    try:
        return await asyncio.wait_for(proc.communicate(data), timeout_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await asyncio.shield(proc.wait())
        raise


LOGIN_WAIT_S = 15 * 60


async def login(client: AsyncClient, password: str, device_name: str, sleep=asyncio.sleep):
    """client.login, waiting out Synapse's per-account sign-in limit (429) instead of failing: a
    failed start restarts and signs in again, which only keeps the limit hit (seen live on @pm)."""
    waited = 0.0
    while True:
        resp = await client.login(password, device_name=device_name)
        if isinstance(resp, LoginResponse) or getattr(resp, "status_code", "") != "M_LIMIT_EXCEEDED" or waited >= LOGIN_WAIT_S:
            return resp
        pause = min(max((getattr(resp, "retry_after_ms", None) or 5000) / 1000, 1.0), LOGIN_WAIT_S - waited)
        log.warning("Synapse limits %s's sign-ins: waiting %.0fs", client.user_id, pause)
        await sleep(pause)
        waited += pause


def _path(name: str) -> Path:
    return HANDOFF_DIR / f"{name}.json"


class SyncPoint:
    """Tracks the last fully handled sync batch and the events handled since (see module doc)."""
    def __init__(self, client: AsyncClient, handled: list[str] | None = None) -> None:
        self.token: str | None = None
        self.handled: set[str] = set(handled or [])     # unbounded, but emptied after every batch
        self.stop = asyncio.Event()                      # set by SIGTERM (installed in first_sync)
        client.add_response_callback(self._batch_done, SyncResponse)   # runs after event callbacks
        client.add_response_callback(self._sync_failed, SyncError)

    async def _sync_failed(self, response: SyncError) -> None:
        """Signed out by the server (Disconnect gave the agent a new password): stop for good, a clean
        exit, instead of retrying the dead sign-in many times a second (seen live)."""
        if response.status_code == "M_UNKNOWN_TOKEN" and not getattr(response, "soft_logout", False):   # soft: recoverable
            log.error("signed out by the server (disconnected): stopping; connect again with a new code")
            self.stop.set()

    async def _batch_done(self, response: SyncResponse) -> None:
        self.token = response.next_batch
        self.handled.clear()

    def first_time(self, event_id: str) -> bool:
        """False for an event already handled before a restart; call once per event handled."""
        if event_id in self.handled:
            return False
        self.handled.add(event_id)
        return True


def save_handoff(name: str, point: SyncPoint) -> None:
    token = point.token
    if not token:
        return
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(name).with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "handled": sorted(point.handled), "ms": int(time.time() * 1000)}, f)
    os.replace(tmp, _path(name))


def take_handoff(name: str) -> dict | None:
    """The previous process's sync position, if recent. Kept on disk until drop_handoff(), so a
    process killed during startup leaves it for the next one."""
    p = _path(name)
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable hand-off %s: %s", p, e)   # cold start is the safe fallback
        data = None
    if data and time.time() * 1000 - data["ms"] < HANDOFF_MAX_AGE_S * 1000:
        return data
    return None


def drop_handoff(name: str) -> None:
    _path(name).unlink(missing_ok=True)


async def first_sync(client: AsyncClient, name: str, point: SyncPoint, register: Callable[[], None],
                     set_cutoff: Callable[[int], None]) -> None:
    """Resume from a hand-off (callbacks see what was missed) or skip history (callbacks added after).
    set_cutoff(ms) is called BEFORE any callback runs: events older than it count as stale."""
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, point.stop.set)   # before any I/O
    except NotImplementedError:
        pass   # Windows has no asyncio signal handlers; there connect_win.py stops the agent by ending it
    handoff = take_handoff(name)
    if handoff:
        point.handled.update(handoff.get("handled", []))
        set_cutoff(handoff["ms"] - SYNC_SLACK_MS)
        register()
        await client.sync(timeout=0, since=handoff["token"], full_state=True)
        log.info("%s resumed from hand-off", name)
    else:
        set_cutoff(int(time.time() * 1000))
        await client.sync(timeout=0, full_state=True)    # sets point.token: nothing before is ours
        register()
    drop_handoff(name)                                   # superseded by this process's own position


async def serve(client: AsyncClient, name: str, point: SyncPoint, busy: set[asyncio.Task],
                side: Coroutine | None = None) -> None:
    """Sync until SIGTERM, then hand off the sync token and let `busy` tasks finish."""
    runners = {asyncio.create_task(client.sync_forever(timeout=30_000))}
    if side is not None:
        runners.add(asyncio.create_task(side))
    stopper = asyncio.create_task(point.stop.wait())
    done, _ = await asyncio.wait({*runners, stopper}, return_when=asyncio.FIRST_COMPLETED)
    for t in (*runners, stopper):
        t.cancel()
    for t in runners:
        if t in done and t.exception():
            raise t.exception()          # a crash, not a restart: let the supervisor see it
        with contextlib.suppress(asyncio.CancelledError):
            await t
    save_handoff(name, point)
    if busy:
        log.info("%s finishing %d running task(s) before restart", name, len(busy))
        await asyncio.wait(busy, timeout=DRAIN_TIMEOUT_S)
    await client.close()


def is_stale(event, started_ms: int) -> bool:
    """An event from before this process started (a replay after a restart or a first sync)."""
    return event.server_timestamp < started_ms - CLOCK_SKEW_MS
