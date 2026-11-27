"""The trial agent's monthly budget guard (ADR 0025): trials never push the Ollama plan into pay-as-you-go.

The OLLAMA_API_KEY may be shared with other projects, and the plan has a monthly credit allowance with
pay-as-you-go beyond it. So, before each trial answer:
  - Ollama's balance (GET https://ollama.com/api/balance, checked at most every TRIAL_BUDGET_CHECK_S, as
    its rate limit is shared by every user of the key): trials stop once the plan's used share of this
    period passes TRIAL_MAX_SHARE (default 0.5), and always once its included credits are gone. An
    older plan reporting session/weekly "remaining_percent" is read the same way.
  - Our own count of the tokens trial answers used this UTC month: trials stop at TRIAL_MONTHLY_TOKENS.
    It applies always, and is the only guard while the balance can't be read.
  - Ollama answering 429 (too many requests) or 402 (payment needed): trials pause (Retry-After, else
    TRIAL_PAUSE_S) and the operator sees it in the log.
State (month's tokens, a pause) is kept in data/trial-budget.json, so a restart keeps it.
"""
import datetime
import json
import logging
import os
import time
from pathlib import Path

import aiohttp

from common.bridge_config import ROOT
from server import trial_limits

log = logging.getLogger("trial")
BALANCE_URL = "https://ollama.com/api/balance"
DEFAULTS = {"TRIAL_MAX_SHARE": 0.5, "TRIAL_MONTHLY_TOKENS": 5_000_000, "TRIAL_BUDGET_CHECK_S": 300, "TRIAL_PAUSE_S": 900}
BALANCE_TIMEOUT_S = 10
BUDGET_FILE = ROOT / "data" / "trial-budget.json"
SHARE, TOKENS, PAUSED = "share", "tokens", "paused"   # why trials are stopped


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


class Budget:
    def __init__(self, path: Path | None = None, clock=time.time, fetch=None) -> None:
        self.path, self.clock, self.fetch = path or BUDGET_FILE, clock, fetch or fetch_balance
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}                    # nothing yet (or a damaged file): this month starts at 0
        if not isinstance(self.data, dict):
            self.data = {}
        self.share, self.checked = None, 0.0  # the plan's used share, as last read, and when

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)

    def _month(self) -> dict:
        if self.data.get("month") != month(self.clock()):
            self.data = {"month": month(self.clock()), "tokens": 0, "paused_until": self.data.get("paused_until", 0)}
        return self.data

    def add_tokens(self, n: float) -> None:
        self._month()["tokens"] = int(self.data.get("tokens", 0)) + max(0, round(n))
        self._save()

    def pause(self, seconds: float, why: str) -> None:
        self.data["paused_until"] = self.clock() + max(60.0, seconds)
        self._save()
        log.error("trial agent paused for %.0f min: %s (operator: check the Ollama plan's usage)", max(60.0, seconds) / 60, why)

    async def stopped(self) -> str | None:
        """Why trials must not call the model now (PAUSED, SHARE, TOKENS), or None."""
        now = self.clock()
        if float(self.data.get("paused_until", 0)) > now:
            return PAUSED
        if int(self._month().get("tokens", 0)) >= setting("TRIAL_MONTHLY_TOKENS"):
            return TOKENS
        if now - self.checked >= setting("TRIAL_BUDGET_CHECK_S"):
            self.checked = now
            share = used_share(await self.fetch())
            if share is None:                 # the last reading stands (an "over" one keeps trials stopped)
                log.warning("couldn't read the Ollama balance: the last reading and the monthly token cap guard trials")
            else:
                self.share = share
        return SHARE if self.share is not None and self.share >= min(1.0, setting("TRIAL_MAX_SHARE")) else None

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
