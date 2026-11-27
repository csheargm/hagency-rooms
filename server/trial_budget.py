"""The trial agent's monthly budget guard (ADR 0025): trials never push the Ollama plan into pay-as-you-go.

The OLLAMA_API_KEY may be shared with other projects, and the plan has a monthly credit allowance with
pay-as-you-go beyond it. So, before each trial answer:
  - Ollama's balance (GET https://ollama.com/api/balance, checked at most every TRIAL_BUDGET_CHECK_S, as
    its rate limit is shared by every user of the key): trials stop once the plan's used share of this
    period passes TRIAL_MAX_SHARE (default 0.5), and always once its included credits are gone. An
    older plan reporting session/weekly "remaining_percent" is read the same way.
  - Our own count of the tokens trial answers used this UTC month: trials stop at TRIAL_MONTHLY_TOKENS.
    A high estimate is reserved before each call (and replaced by Ollama's count after), so no call
    passes the cap. It applies always, and is the only guard while the balance has never been read.
  - The balance is only read between calls and other users of the key spend too: the share is a soft
    line. For a hard one, set a spending limit for pay-as-you-go in the Ollama account itself.
  - Ollama answering 429 (too many requests) or 402 (payment needed): trials pause (Retry-After, else
    TRIAL_PAUSE_S) and the operator sees it in the log.
State (month's tokens, a pause, the last balance reading) is kept in data/trial-budget.json, so a
restart keeps it; an unreadable file stops trials (logged) rather than starting a fresh month.
"""
import datetime
import json
import logging
import math
import os
import re
import time
from pathlib import Path

import aiohttp

from common.bridge_config import ROOT
from server import trial_limits

log = logging.getLogger("trial")
BALANCE_URL = "https://ollama.com/api/balance"
DEFAULTS = {"TRIAL_MAX_SHARE": 0.5, "TRIAL_MONTHLY_TOKENS": 5_000_000, "TRIAL_BUDGET_CHECK_S": 300, "TRIAL_PAUSE_S": 900}
BALANCE_TIMEOUT_S = 10
MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
MAX_NUMBER = 10**15                    # far above any count or time here; a bigger one is a damaged file
NEAR_SHARE, NEAR_CHECK_S = 0.1, 60    # within 10 points of the limit: the balance is read every minute
MIN_PAUSE_S, MAX_PAUSE_S = 60, 24 * 3600   # a Retry-After is kept within these
BUDGET_FILE = ROOT / "data" / "trial-budget.json"
SHARE, TOKENS, PAUSED, WINDOW, DAMAGED = "share", "tokens", "paused", "window", "damaged"   # why trials are stopped


def setting(name: str) -> float:
    """One of DEFAULTS' settings (TRIAL_MAX_SHARE is a fraction, 0 to 1)."""
    return setting_or(name, DEFAULTS[name])


def setting_or(name: str, default: float) -> float:
    """A number from the environment; malformed or negative: `default`."""
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def month(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m")


def used_share(data: object) -> float | None:
    """How much of the plan's period is used (0..1), from /api/balance's answer; None: not readable."""
    included = data.get("included") if isinstance(data, dict) else None
    if not isinstance(included, dict):
        return None
    allowance, left = included.get("allowance_usd"), included.get("balance_usd")
    if isinstance(allowance, (int, float)) and isinstance(left, (int, float)) and allowance > 0:
        return 1.0 if left <= 0 else min(1.0, max(0.0, (allowance - left) / allowance))
    shown = [w.get("remaining_percent") for w in (included.get("session"), included.get("weekly")) if isinstance(w, dict)]
    shown = [p for p in shown if isinstance(p, (int, float))]
    return (100 - min(shown)) / 100 if shown else None


def is_window(data: object) -> bool:
    """The reading is an older plan's session/weekly window (it resets in hours), not a period's credits."""
    included = data.get("included") if isinstance(data, dict) else None
    return isinstance(included, dict) and not isinstance(included.get("allowance_usd"), (int, float))


def valid(data: object) -> bool:
    """A budget file as this module writes it (GPT review: a well-formed JSON object isn't enough)."""
    if not isinstance(data, dict):
        return False
    if not data:
        return True                           # nothing counted yet
    numbers = [data.get(k, 0) for k in ("paused_until", "checked")] + ([data["share"]] if "share" in data else [])
    return (isinstance(data.get("month"), str) and bool(MONTH_RE.fullmatch(data["month"]))
            and isinstance(data.get("tokens"), int) and 0 <= data["tokens"] < MAX_NUMBER
            and all(isinstance(v, (int, float)) and -MAX_NUMBER < v < MAX_NUMBER for v in numbers)   # no overflow, NaN or inf
            and 0 <= float(data.get("share", 0)) <= 1)


class Budget:
    def __init__(self, path: Path | None = None, clock=time.time, fetch=None) -> None:
        self.path, self.clock, self.fetch = path or BUDGET_FILE, clock, fetch or fetch_balance
        self.damaged = False
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {}                    # nothing yet: this month starts at 0
        except (OSError, ValueError):
            self.data = {}
            self.damaged = True               # GPT review: fail closed, never a fresh month by a broken file
        if not valid(self.data):
            self.data, self.damaged = {}, True
        if self.damaged:
            log.error("the trial budget file %s is unreadable: trials stay stopped until it is fixed or removed", self.path)
        # the plan's used share as last read, and when: kept on disk, so a restart keeps an "over" reading
        share = self.data.get("share")
        self.share = float(share) if isinstance(share, (int, float)) and math.isfinite(share) else None
        self.checked = float(self.data.get("checked", 0)) if isinstance(self.data.get("checked"), (int, float)) else 0.0
        self.window = self.data.get("window") is True      # the reading came from an older plan's session/weekly window

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)

    def _month(self) -> dict:
        if self.data.get("month") != month(self.clock()):
            kept = {k: self.data[k] for k in ("paused_until", "share", "checked", "window") if k in self.data}
            self.data = {"month": month(self.clock()), "tokens": 0, **kept}
        return self.data

    def add_tokens(self, n: float) -> None:
        data = self._month()                  # first: a settle after midnight on the 1st counts in the new month
        data["tokens"] = max(0, int(data.get("tokens", 0)) + round(n))
        self._save()

    def reserve(self, n: float) -> bool:
        """Count `n` (an estimate, on the high side) before a call; False, and nothing counted, if it would
        pass this month's token cap. Saved first, so a crash mid-call still leaves it counted."""
        if int(self._month().get("tokens", 0)) + n > setting("TRIAL_MONTHLY_TOKENS"):
            return False
        self.add_tokens(n)
        return True

    def settle(self, reserved: float, used: float) -> None:
        """Ollama's own count replaces the estimate (a failed call keeps the estimate)."""
        self.add_tokens(max(0.0, used) - reserved)

    def pause(self, seconds: float, why: str) -> None:
        seconds = min(max(MIN_PAUSE_S, seconds), MAX_PAUSE_S) if math.isfinite(seconds) else setting("TRIAL_PAUSE_S")
        self._month()["paused_until"] = self.clock() + seconds
        self._save()
        log.error("trial agent paused for %.0f min: %s (operator: check the Ollama plan's usage)", seconds / 60, why)

    async def stopped(self) -> str | None:
        """Why trials must not call the model now (DAMAGED, PAUSED, TOKENS, SHARE, WINDOW), or None."""
        now = self.clock()
        if self.damaged:
            return DAMAGED
        if float(self.data.get("paused_until", 0)) > now:
            return PAUSED
        if int(self._month().get("tokens", 0)) >= setting("TRIAL_MONTHLY_TOKENS"):
            return TOKENS
        limit = min(1.0, setting("TRIAL_MAX_SHARE"))
        near = self.share is not None and self.share >= limit - NEAR_SHARE   # close: read it more often
        if now - self.checked >= (NEAR_CHECK_S if near else setting("TRIAL_BUDGET_CHECK_S")):
            self.checked = self.data["checked"] = now
            balance = await self.fetch()
            share = used_share(balance)
            if share is not None:
                self.window = self.data["window"] = is_window(balance)
            if share is None:                 # the last reading stands (an "over" one keeps trials stopped)
                log.warning("couldn't read the Ollama balance: the last reading and the monthly token cap guard trials")
            else:
                self.share = self.data["share"] = share
            self._save()
        if self.share is None or self.share < limit:
            return None
        return WINDOW if self.window else SHARE

    def paused_for(self) -> float:
        return max(0.0, float(self.data.get("paused_until", 0)) - self.clock())


async def fetch_balance() -> object:
    """/api/balance's JSON, or None (unreachable, refused, or rate limited: the token cap still applies)."""
    headers = {"Authorization": f"Bearer {trial_limits.api_key()}"}
    url = os.environ.get("TRIAL_BALANCE_URL", "").strip() or BALANCE_URL
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=BALANCE_TIMEOUT_S)) as r:
                return await r.json(content_type=None) if r.status == 200 else None
    except (aiohttp.ClientError, TimeoutError, ValueError):   # asyncio.TimeoutError is TimeoutError (3.11+)
        return None                                           # said by stopped(): "only the token cap"
