"""The trial-worker container's process (ADR 0027): `python -m server.trial_worker`.

Only it holds the E2B key, the Trial App's key and its own Ollama key (deploy/trial.env); it never
gets @pm's .env, the registration secret or @pm's App key, never joins the database network, and needs
no Matrix account: @pm tells visitors the news from the shared folder (pm_build.py).

It runs one build at a time, server-wide, and beats a heartbeat while it is up, so @pm offers builds
only then. At start, whatever was underway is failed ("the server restarted") and every sandbox of
ours is killed; while running, a reaper kills any older than common/sandbox.REAP_AFTER_S. Over the
day's sandbox time (TRIAL_SANDBOX_S_PER_DAY) it takes no new build until 00:00 UTC.
Kill switch: TRIAL_BUILD=off in trial.env (then restart it): every sandbox of ours is killed, waiting
and running builds fail with "switched off", the heartbeat goes, and it exits; talk is unaffected.
Not configured: it exits at once, and nothing anywhere changes.
"""
import asyncio
import datetime
import logging
import signal
import sys
import time

from common import model_slot
from common import sandbox
from server import trial_budget
from server import trial_build
from server import trial_build_config as config
from server import trial_github
from server import trial_jobs
from server import trial_pool

log = logging.getLogger("trial-build")
POLL_S = 5
BEAT_S = 30


def day_start(now: float) -> float:
    t = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    return t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


async def beating(jobs: trial_jobs.Jobs) -> None:
    while True:
        jobs.beat(config.caps())
        await asyncio.sleep(BEAT_S)


async def run_one(d: trial_build.Deps, job_id: str, job: dict) -> str:
    beat = asyncio.create_task(beating(d.jobs))     # a long build keeps the offer (and the queue) alive
    try:
        state, outcome = await trial_build.build(d, job_id, job)
    except Exception as e:   # noqa: BLE001 — a bug must fail this one build plainly, never stop the worker
        log.exception("trial build %s crashed: %s", job_id, type(e).__name__)
        state, outcome = trial_jobs.FAILED, {"why": "error"}
    finally:
        beat.cancel()
    d.jobs.finish(job_id, state, outcome)
    log.info("trial build %s: %s %s", job_id, state, outcome.get("why", outcome.get("pr", "")))
    return state


async def tick(d: trial_build.Deps) -> str | None:
    """One round: heartbeat, reaper, tidy, and at most one build; the build's end state, if one ran."""
    d.jobs.beat(config.caps())
    try:
        await d.sandboxes.reap()
    except Exception as e:   # noqa: BLE001 — E2B unreachable: retried next round, and E2B's own timeout
        log.warning("the sandbox reaper couldn't run: %s", type(e).__name__)   # (15 min) ends every sandbox anyway
    d.jobs.prune()
    if d.jobs.sandbox_seconds_today(day_start(time.time())) >= config.number("TRIAL_SANDBOX_S_PER_DAY"):
        return None               # today's sandbox time is used: builds wait for tomorrow
    claimed = d.jobs.claim()
    return await run_one(d, *claimed) if claimed else None


async def serve(d: trial_build.Deps, stop: asyncio.Event, poll_s: float = POLL_S) -> bool:
    """Run until `stop`; False at once if another worker is running (it keeps its builds and sandboxes)."""
    with d.jobs.one_worker() as mine:
        if not mine:
            log.error("another trial-worker is running: this one exits")
            return False
        for job_id in d.jobs.fail_unfinished("restart"):
            log.warning("trial build %s was underway at restart: failed", job_id)
        await d.sandboxes.reap(older_than_s=0)      # nothing of ours can be legitimately alive now
        try:
            while not stop.is_set():
                await tick(d)
                try:
                    await asyncio.wait_for(stop.wait(), poll_s)
                except asyncio.TimeoutError:
                    continue      # the next round
        finally:
            d.jobs.stop_beating()
    return True


async def shut_down(jobs: trial_jobs.Jobs, sandboxes) -> bool:
    """The kill switch: no sandbox of ours survives, nothing waits, and @pm stops offering builds. Only once
    no worker runs (its lifetime lock; GPT review): False while one still does (recreate it first)."""
    with jobs.one_worker() as alone:
        if not alone:
            log.error("a trial-worker is still running: recreate it with TRIAL_BUILD=off")
            return False
        for job_id in jobs.shut("off"):
            log.warning("trial build %s: switched off", job_id)
        if sandboxes is not None:
            log.warning("switched off: killed %s sandbox(es)", await sandboxes.reap(older_than_s=0))
    return True


def deps() -> trial_build.Deps:
    folder = config.shared_dir()
    gh = trial_github.TrialGitHub(config.env("TRIAL_APP_ID"), str(config.key_path()), config.repos())
    return trial_build.Deps(
        gh=gh, sandboxes=sandbox.E2BSandboxes(config.env("E2B_API_KEY"), config.template(), config.number("TRIAL_SANDBOX_TIMEOUT_S")),
        pool=trial_pool.Pool(folder / "pool.json", config.repos(), config.number("TRIAL_LEASE_DAYS")),
        jobs=trial_jobs.Jobs(folder), slot=model_slot.Slot(model_slot.lock_path(folder), model_slot.BUILD),
        budget=trial_budget.Budget(folder / "build-budget.json"))


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    jobs = trial_jobs.Jobs(config.shared_dir())
    if config.switched_off():
        done = await shut_down(jobs, sandbox.E2BSandboxes(config.env("E2B_API_KEY"), config.template(), 0)
                               if config.env("E2B_API_KEY") else None)
        return 0 if done else 1           # refused: non-zero, so Compose tries it again (GPT review)
    if gone := config.missing():
        jobs.stop_beating()
        log.info("trial builds are off (not configured: %s)", ", ".join(gone))
        return 0
    log.info("trial builds on: %d demo repo(s) in %s", len(config.repos()), config.org())
    task = asyncio.create_task(serve(deps(), asyncio.Event()))
    # docker stop: the build underway is cancelled, so its `finally`s run (its sandbox killed, its token revoked,
    # the heartbeat removed); the job stays "running" and the next start fails it plainly (GPT review)
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        log.info("trial-worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
