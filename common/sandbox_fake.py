"""The sandbox interface of common/sandbox.py, in memory, for tests (NOT used in production; ADR 0027).

FakeBox answers our own fixed commands (the egress probe, `env`, the file listing) like a sandbox with
a working firewall would, and hands every other command to `on_run(box, cmd)`. `outside` holds files at
absolute paths (the test report). FakeSandboxes keeps every box it made, so tests can check each was killed.
"""
import asyncio
import json
import time
from dataclasses import dataclass, field

from common.sandbox import (BUILD, ENV_CMD, LIST_CMD, PROBE_CMD, REAP_AFTER_S, Result, SandboxError, is_stale,
                            parse_listing, sha256)

BLOCKED = 6                       # curl: couldn't resolve host


@dataclass
class FakeBox:
    kind: str
    job: str
    on_run: object = None
    egress_open: bool = False
    environ: str = "PATH=/usr/bin\nHOME=/home/user\n"
    probe_exit: int | None = None
    files: dict = field(default_factory=dict)
    outside: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    killed: bool = False
    ran: list = field(default_factory=list)
    id: str = "fake"
    started: float = 0.0

    async def run(self, cmd: str, timeout: float = 120, cwd: str | None = None) -> Result:
        self.ran.append(cmd)
        if cmd == PROBE_CMD:
            code = self.probe_exit if self.probe_exit is not None else 0 if self.egress_open else BLOCKED
            return Result(code, "", "" if code == 0 else "could not resolve host")
        if cmd == ENV_CMD:
            return Result(0, self.environ)
        if cmd == LIST_CMD:
            return Result(0, json.dumps({"files": {p: [sha256(d), len(d)] for p, d in self.files.items()},
                                         "links": list(self.links), "other": [], "truncated": False}))
        if self.on_run is None:
            return Result(127, "", "no such command")
        result = self.on_run(self, cmd)
        return await result if asyncio.iscoroutine(result) else result

    async def run_capped(self, cmd: str, limit: int, timeout: float = 120) -> Result:
        r = await self.run(cmd, timeout)
        return Result(r.exit_code, (r.stdout + r.stderr)[:limit])

    async def read_capped(self, path: str, limit: int) -> bytes | None:
        where = self.outside if path.startswith("/") else self.files
        return where[path][:limit] if path in where else None

    async def write_files(self, files: dict[str, bytes]) -> None:
        self.files.update(files)

    async def read(self, path: str, limit: int = 1024 * 1024) -> bytes:
        where = self.outside if path.startswith("/") else self.files
        if path not in where:
            raise SandboxError("no such file in the sandbox")
        if len(where[path]) > limit:
            raise SandboxError("a file in the sandbox is too big")
        return where[path]

    async def listing(self) -> dict:
        return parse_listing((await self.run(LIST_CMD)).stdout)

    async def kill(self) -> None:
        self.killed = True


class FakeSandboxes:
    """Hands out FakeBoxes; `boxes` keeps every one made (tests check they were all killed)."""
    def __init__(self, on_run=None, egress_open: bool = False, refuse: bool = False, environ: str | None = None,
                 clock=time.time, probe_exit: int | None = None) -> None:
        self.on_run, self.egress_open, self.refuse, self.environ, self.clock = on_run, egress_open, refuse, environ, clock
        self.probe_exit = probe_exit
        self.boxes: list[FakeBox] = []
        self.reaped: list[int] = []

    async def create(self, kind: str, job_id: str) -> FakeBox:
        if self.refuse:
            raise SandboxError("the sandbox service refused a sandbox (fake)")
        box = FakeBox(kind, job_id, self.on_run, self.egress_open and kind == BUILD, probe_exit=self.probe_exit,
                      id=f"sbx-{len(self.boxes) + 1}", started=self.clock())
        if self.environ is not None:
            box.environ = self.environ
        self.boxes.append(box)
        return box

    async def reap(self, now: float | None = None, older_than_s: int = REAP_AFTER_S) -> int:
        now = self.clock() if now is None else now
        self.reaped.append(older_than_s)
        live = [b for b in self.boxes if not b.killed and is_stale(b.started, now, older_than_s)]
        for box in live:
            box.killed = True
        return len(live)
