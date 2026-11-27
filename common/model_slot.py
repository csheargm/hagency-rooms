"""The one Ollama Cloud slot that trial traffic may use (ADR 0027): talk and builds together.

The plan allows 3 requests at once and the key is shared with another project, so the talk-only trial
(@pm's process) and trial builds (the trial-worker container) share ONE slot. It is a file lock
(fcntl.flock) on a file in the folder both containers mount (TRIAL_SHARED_DIR), so it holds across
processes and containers on one host. Talk goes first: a waiting talk answer leaves a marker file,
and a build never takes the slot while a fresh marker is there (a build holds it for one model call
at a time, so a talk answer waits at most one build step).

A slot is acquired and released like asyncio.Semaphore (trial_turn.py uses it that way); acquire()
can be cancelled (asyncio.wait_for) at any await and then holds nothing and leaves no marker.
"""
import asyncio
import fcntl
import itertools
import os
import time
from pathlib import Path

TALK, BUILD = "talk", "build"
LOCK_NAME = "ollama-slot.lock"
POLL_S = 0.05
STALE_MARKER_S = 120          # a marker this old belongs to a process that died: ignored
_ids = itertools.count()


def lock_path(folder: Path) -> Path:
    return Path(folder) / LOCK_NAME


class Slot:
    def __init__(self, path: Path, kind: str = TALK, poll_s: float = POLL_S, clock=time.time) -> None:
        if kind not in (TALK, BUILD):
            raise ValueError(f"not a slot kind: {kind}")
        self.path, self.kind, self.poll_s, self.clock = Path(path), kind, poll_s, clock
        self.fd: int | None = None

    def _markers(self) -> list[Path]:
        return list(self.path.parent.glob(f"{self.path.name}.talk.*"))

    def talk_waiting(self) -> bool:
        """A talk answer is waiting for the slot now (a marker refreshed within STALE_MARKER_S)."""
        now = self.clock()
        for marker in self._markers():
            try:
                if now - marker.stat().st_mtime < STALE_MARKER_S:
                    return True
            except FileNotFoundError:
                continue          # removed between the listing and the stat: that talk answer got the slot
        return False

    def _try_lock(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    async def acquire(self) -> bool:
        """Wait for the slot (talk first); True once held. Cancel it to give up (nothing is held then)."""
        marker = self.path.parent / f"{self.path.name}.talk.{os.getpid()}.{next(_ids)}" if self.kind == TALK else None
        try:
            while True:
                if marker is not None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()            # refreshed while waiting: never stale while we wait
                if not (self.kind == BUILD and self.talk_waiting()) and self._try_lock():
                    return True
                await asyncio.sleep(self.poll_s)
        finally:
            if marker is not None:
                marker.unlink(missing_ok=True)

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def locked(self) -> bool:
        return self.fd is not None

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        self.release()
