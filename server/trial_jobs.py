"""Trial builds' queue, shared by @pm and the trial-worker container (ADR 0027).

Both mount one folder (trial_build_config.shared_dir); nothing secret is in it. @pm adds a job when a
visitor clicks "Try a build" and tells them the news; the worker takes one job at a time (one active
build server-wide) and writes its progress and outcome. Every change is a read-modify-write under one
file lock (fcntl.flock on jobs.lock), so the two processes never lose each other's writes.

jobs.json: {job id: {"visitor", "github_id", "dm", "state", "created", "started", "finished",
"step", "outcome": {...}, "told": [events @pm has said]}}. States: queued → running → done | failed.
worker.json: the worker's heartbeat ({"at", "caps"}): builds are offered only while it is fresh.
"""
import contextlib
import fcntl
import json
import os
import secrets
import time
from pathlib import Path

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
HEARTBEAT_S = 90                     # older: the worker is down (or off), and builds aren't offered
KEEP_S = 7 * 24 * 3600               # finished jobs are kept this long (the visitor's history)
BUSY, ALREADY, OFF, REFUSED = "busy", "already", "off", "refused"


class Jobs:
    def __init__(self, folder: Path, clock=time.time) -> None:
        self.folder, self.clock = Path(folder), clock
        self.path, self.lock_path, self.beat_path = self.folder / "jobs.json", self.folder / "jobs.lock", self.folder / "worker.json"
        self.worker_lock = self.folder / "worker.lock"

    @contextlib.contextmanager
    def _locked(self):
        """The jobs, for one read-modify-write; saved when the block ends without an error."""
        self.folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            jobs = self._read()
            yield jobs
            self._write(self.path, jobs)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:
            return {}                 # a damaged file: no jobs (they're re-asked for; nothing runs twice)
        return data if isinstance(data, dict) else {}

    def _write(self, path: Path, data: dict) -> None:
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)

    def all(self) -> dict:
        with self._locked() as jobs:
            return json.loads(json.dumps(jobs))

    @staticmethod
    def _queued(jobs: dict) -> list[str]:
        return sorted((j for j, v in jobs.items() if v.get("state") == QUEUED), key=lambda j: jobs[j].get("created", 0))

    @staticmethod
    def _position(jobs: dict, job_id: str) -> int:
        """1 = next to run; a running build ahead counts."""
        ahead = any(v.get("state") == RUNNING for v in jobs.values())
        return Jobs._queued(jobs).index(job_id) + 1 + ahead

    def enqueue(self, visitor: str, github_id: int, dm: str, limit: int, admit=None,
                charge=None) -> tuple[str, str | None, int]:
        """(QUEUED | ALREADY | BUSY | OFF | REFUSED, job id or REFUSED's reason, position): one job at a time per
        visitor; at most `limit` waiting. All under the lock the worker's shut() takes, against the latest
        heartbeat: OFF when it went; `admit(caps)` → a reason refuses (paused, no repo); `charge()` → what it
        was charged as (kept with the job, so a refund is exact), only for a job really queued (GPT review)."""
        with self._locked() as jobs:
            caps = self.alive()
            if caps is None:
                return OFF, None, 0
            if admit is not None and (why := admit(caps)):
                return REFUSED, why, 0
            mine = next((j for j, v in jobs.items() if v.get("visitor") == visitor and v.get("state") in (QUEUED, RUNNING)), None)
            if mine:
                return ALREADY, mine, 0 if jobs[mine]["state"] == RUNNING else self._position(jobs, mine)
            if len(self._queued(jobs)) >= limit:
                return BUSY, None, 0
            job_id = secrets.token_hex(4)
            jobs[job_id] = {"visitor": visitor, "github_id": github_id, "dm": dm, "state": QUEUED,
                            "created": self.clock(), "told": [QUEUED], "charged": charge() if charge else None}
            return QUEUED, job_id, self._position(jobs, job_id)

    def claim(self) -> tuple[str, dict] | None:
        """The worker: the oldest queued job, now running; None if one runs already or none waits."""
        with self._locked() as jobs:
            if any(v.get("state") == RUNNING for v in jobs.values()) or not self._queued(jobs):
                return None
            job_id = self._queued(jobs)[0]
            jobs[job_id].update(state=RUNNING, started=self.clock())
            return job_id, dict(jobs[job_id])

    def update(self, job_id: str, **fields) -> None:
        with self._locked() as jobs:
            if job_id in jobs:
                jobs[job_id].update(fields)

    def finish(self, job_id: str, state: str, outcome: dict) -> None:
        self.update(job_id, state=state, outcome=outcome, finished=self.clock())

    def fail_unfinished(self, why: str, states=(RUNNING,)) -> list[str]:
        """A worker that starts again (or is switched off): what was underway can't finish."""
        with self._locked() as jobs:
            hit = [j for j, v in jobs.items() if v.get("state") in states]
            for j in hit:                 # a PR it opened already is kept in the outcome: it is open on GitHub
                pr = {"pr": jobs[j]["pr"], "repo": jobs[j].get("repo")} if jobs[j].get("pr") else {}
                jobs[j].update(state=FAILED, outcome={"why": why, **pr}, finished=self.clock())
            return hit

    def news(self) -> list[tuple[str, dict, str]]:
        """@pm: (job id, job, event) not said yet; events are "started" and the end state."""
        out = []
        for job_id, job in self.all().items():
            told = job.get("told") or []
            if job.get("state") in (RUNNING, DONE, FAILED) and "started" not in told and job.get("started"):
                out.append((job_id, job, "started"))
            if job.get("state") in (DONE, FAILED) and job["state"] not in told:
                out.append((job_id, job, job["state"]))
        return out

    def mark_refunded(self, job_id: str) -> str | None:
        """Once only: the day it was charged on, if it was charged and not refunded yet, else None."""
        with self._locked() as jobs:
            job = jobs.get(job_id) or {}
            if not job.get("charged") or job.get("refunded"):
                return None
            job["refunded"] = True
            return job["charged"]

    def told(self, job_id: str, event: str) -> None:
        with self._locked() as jobs:
            if job_id in jobs:
                jobs[job_id]["told"] = [*(jobs[job_id].get("told") or []), event]

    def prune(self) -> None:
        with self._locked() as jobs:
            for j in [j for j, v in jobs.items() if v.get("state") in (DONE, FAILED)
                      and self.clock() - float(v.get("finished") or 0) > KEEP_S]:
                jobs.pop(j)

    def of(self, visitor: str) -> tuple[str, dict] | None:
        """Their job underway (queued or running), with its position."""
        jobs = self.all()
        for job_id, job in jobs.items():
            if job.get("visitor") == visitor and job.get("state") in (QUEUED, RUNNING):
                return job_id, {**job, "position": 0 if job["state"] == RUNNING else self._position(jobs, job_id)}
        return None

    def sandbox_seconds_today(self, day_start: float) -> float:
        return sum(float(v.get("sandbox_s") or 0) for v in self.all().values() if float(v.get("started") or 0) >= day_start)

    # --- the worker's heartbeat -------------------------------------------------------------------
    def beat(self, caps: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._write(self.beat_path, {"at": self.clock(), "caps": caps})

    def stop_beating(self) -> None:
        self.beat_path.unlink(missing_ok=True)

    def shut(self, why: str) -> list[str]:
        """The kill switch, in one step under the queue's lock: no heartbeat (so @pm queues nothing more),
        and everything waiting or running failed with `why`."""
        with self._locked() as jobs:
            self.stop_beating()
            hit = [j for j, v in jobs.items() if v.get("state") in (QUEUED, RUNNING)]
            for j in hit:
                jobs[j].update(state=FAILED, outcome={"why": why}, finished=self.clock())
            return hit

    @contextlib.contextmanager
    def one_worker(self):
        """Held for a worker's whole life: a second one (an overlapping restart) exits instead of failing the
        first one's build and killing its sandboxes (GPT review). Yields False when another holds it."""
        self.folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.worker_lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)

    def alive(self) -> dict | None:
        """The worker's caps while its heartbeat is fresh; None: builds are off."""
        try:
            data = json.loads(self.beat_path.read_text())
        except (OSError, ValueError):
            return None
        fresh = isinstance(data, dict) and 0 <= self.clock() - float(data.get("at") or 0) < HEARTBEAT_S
        return data.get("caps") if fresh and isinstance(data.get("caps"), dict) else None
