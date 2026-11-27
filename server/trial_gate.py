"""Who may start a trial build, and how many (ADR 0027), checked by @pm before a job is queued.

The gate: a person with an account on THIS server (not federated), who connected GitHub (ADR 0007:
the account they proved is theirs), whose GitHub account is at least TRIAL_MIN_ACCOUNT_DAYS old
(30). Sign-up is open to any email (ADR 0013), so the caps count per GitHub account id, never per
Matrix id: TRIAL_BUILDS_PER_ACCOUNT a UTC day (3) and TRIAL_BUILDS_TOTAL ever (10), and
TRIAL_BUILDS_PER_DAY for the whole server (20), which the operator's demo allowlist bypasses.
A build is counted when it is queued, and never given back: a failed build still cost something.
The caps come from the worker's heartbeat (trial_build_config.caps), so they live in trial.env only.

The account's age: GitHub's public GET /user/<id> (no token), read once a day per account.
"""
import asyncio
import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from common import github_app
from common import identity
from common.bridge_config import ROOT, SERVER_NAME

USAGE_FILE = ROOT / "data" / "trial-build-usage.json"
AGE_CACHE_S = 24 * 3600
REMOTE = "Trial builds are for people with an account on this server."
NO_GITHUB = ("Trial builds need your GitHub account connected first: in My Hagency, click **Connect GitHub**. "
             "It must be at least {days} days old.")
NO_AGE = "I couldn't check your GitHub account's age just now. Please try again in a few minutes."
TOO_NEW = "Trial builds need a GitHub account at least {days} days old; yours is {age} days old."
PER_DAY = "You've used today's {n} trial builds. They come back at 00:00 UTC."
TOTAL = "You've used all {n} of your trial builds. To keep building, bring your own agent: type \"bring my own\"."
SERVER = "This server has used all of today's trial builds. Please try again tomorrow (after 00:00 UTC)."
_ages: dict[int, tuple[float, float | None]] = {}     # github id → (read at, created at)


def day(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d")


def _created_at(github_id: int) -> float | None:
    req = urllib.request.Request(f"{github_app.API}/user/{int(github_id)}",
                                 headers={"Accept": github_app.REST_ACCEPT, "User-Agent": "hagency-rooms-pm"})
    try:
        with urllib.request.urlopen(req, timeout=github_app.HTTP_TIMEOUT_S) as r:
            raw = json.loads(r.read(200_000) or b"{}")
        stamp = str(raw.get("created_at", "")) if isinstance(raw, dict) else ""
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None               # unknown: the gate says it couldn't check (NO_AGE), never lets it through


async def account_created(github_id: int, now: float, read=None) -> float | None:
    cached = _ages.get(github_id)
    if cached and cached[1] is not None and now - cached[0] < AGE_CACHE_S:
        return cached[1]
    created = await asyncio.to_thread(read or _created_at, github_id)
    _ages[github_id] = (now, created)
    return created


class Usage:
    """{"day", "server", "today": {github id: n}, "total": {github id: n}} on disk (restarts keep it)."""
    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self.path, self.clock, self.lock = path or USAGE_FILE, clock, threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}        # nothing yet (or damaged): counts start at 0, totals too
        if not isinstance(self.data, dict):
            self.data = {}

    def _today(self) -> dict:
        if self.data.get("day") != day(self.clock()):
            self.data = {"day": day(self.clock()), "server": 0, "today": {}, "total": self.data.get("total") or {}}
        return self.data

    def counts(self, github_id: int) -> tuple[int, int, int]:
        with self.lock:
            d, key = self._today(), str(github_id)
            return int(d["today"].get(key, 0)), int(d["total"].get(key, 0)), int(d.get("server", 0))

    def take(self, github_id: int) -> None:
        with self.lock:
            d, key = self._today(), str(github_id)
            d["today"][key] = int(d["today"].get(key, 0)) + 1
            d["total"][key] = int(d["total"].get(key, 0)) + 1
            d["server"] = int(d.get("server", 0)) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.path)


def left_today(usage: Usage, github_id: int | None, caps: dict) -> int:
    if github_id is None:
        return int(caps.get("TRIAL_BUILDS_PER_ACCOUNT", 0))
    today, total, _ = usage.counts(github_id)
    return max(0, min(int(caps["TRIAL_BUILDS_PER_ACCOUNT"]) - today, int(caps["TRIAL_BUILDS_TOTAL"]) - total))


async def refusal(person: str, link: dict | None, caps: dict, usage: Usage, now: float, read=None) -> str | None:
    """Why `person` may not start a build now, or None (go ahead: counted by the caller once queued)."""
    days = int(caps.get("TRIAL_MIN_ACCOUNT_DAYS", 30))
    if identity.split(person)[1] != SERVER_NAME:
        return REMOTE
    if not link:
        return NO_GITHUB.format(days=days)
    created = await account_created(link["id"], now, read)
    if created is None:
        return NO_AGE
    if (age := int((now - created) // 86400)) < days:
        return TOO_NEW.format(days=days, age=max(0, age))
    today, total, server = usage.counts(link["id"])
    if total >= int(caps["TRIAL_BUILDS_TOTAL"]):
        return TOTAL.format(n=caps["TRIAL_BUILDS_TOTAL"])
    if today >= int(caps["TRIAL_BUILDS_PER_ACCOUNT"]):
        return PER_DAY.format(n=caps["TRIAL_BUILDS_PER_ACCOUNT"])
    if server >= int(caps["TRIAL_BUILDS_PER_DAY"]) and person not in (caps.get("demo_users") or []):
        return SERVER
    return None
