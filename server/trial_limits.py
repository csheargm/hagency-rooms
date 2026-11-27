"""The trial agent's settings and daily caps (ADR 0025). Everything comes from the environment
(deploy/.env, passed in by docker-compose.yml); with no OLLAMA_API_KEY the trial is off.

Each visitor also has a daily token allowance (TRIAL_VISITOR_TOKENS; TRIAL_REMOTE_TOKENS for people
from other servers), charged with the same high estimate as the month's cap and settled after.
Caps count model calls per UTC day: TRIAL_PER_VISITOR (default 50) for each visitor and
TRIAL_PER_SERVER (default 1000) for the whole server. The counts are kept on disk
(data/trial-usage.json), so a restart never hands out a fresh day's allowance. A call is counted
before it is made and never given back: a timed-out answer may still have cost the server.
"""
import datetime
import json
import os
import threading
import time
from pathlib import Path

from common.bridge_config import ROOT

KEY_ENV = "OLLAMA_API_KEY"
DEFAULT_URL = "https://ollama.com/api/chat"
DEFAULTS = {"TRIAL_PER_VISITOR": 50, "TRIAL_PER_SERVER": 1000, "TRIAL_MAX_TOKENS": 1500, "TRIAL_TIMEOUT_S": 90,
            # tokens a visitor may use a day (weighted, as the month's cap counts them): a few throwaway accounts
            # can't spend the month (independent review); people from other servers (no sign-up here) get less
            "TRIAL_VISITOR_TOKENS": 150_000, "TRIAL_REMOTE_TOKENS": 50_000}
USAGE_FILE = ROOT / "data" / "trial-usage.json"
VISITOR, SERVER, TOKENS = "visitor", "server", "visitor_tokens"   # which cap was reached


def api_key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def enabled() -> bool:
    """On only with a key; the welcome flow is unchanged without one."""
    return bool(api_key()) and number("TRIAL_PER_SERVER") > 0


def api_url() -> str:
    return os.environ.get("TRIAL_API_URL", "").strip() or DEFAULT_URL


def number(name: str) -> int:
    """A whole-number setting; a missing or malformed value is its default, a negative one 0."""
    try:
        return max(0, int(os.environ.get(name, "").strip()))
    except ValueError:
        return DEFAULTS[name]


def day(now: float) -> str:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d")


def resets_in(now: float) -> str:
    """When the caps reset, in words: midnight UTC."""
    t = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    left = (24 * 3600 - (t.hour * 3600 + t.minute * 60 + t.second)) // 60
    return f"at 00:00 UTC (in {left // 60} h {left % 60} min)"


class Usage:
    """Today's counts: {"day", "server", "visitors": {id: n}, "told": {id: [cap, ...]}}, on disk."""
    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self.path, self.clock, self.lock = path or USAGE_FILE, clock, threading.Lock()
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}                    # no file yet (or a damaged one): today starts at 0
        if not isinstance(self.data, dict):
            self.data = {}

    def _today(self) -> dict:
        today = day(self.clock())
        if self.data.get("day") != today or not isinstance(self.data.get("visitors"), dict):
            self.data = {"day": today, "server": 0, "visitors": {}, "told": {}}
        return self.data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)

    def used(self, visitor: str) -> int:
        with self.lock:
            return int(self._today()["visitors"].get(visitor, 0))

    def take(self, visitor: str) -> str | None:
        """Count one call for `visitor`: None when allowed, else which cap (VISITOR or SERVER) is reached."""
        with self.lock:
            data = self._today()
            if int(data.get("server", 0)) >= number("TRIAL_PER_SERVER"):
                return SERVER
            if int(data["visitors"].get(visitor, 0)) >= number("TRIAL_PER_VISITOR"):
                return VISITOR
            data["server"] = int(data.get("server", 0)) + 1
            data["visitors"][visitor] = int(data["visitors"].get(visitor, 0)) + 1
            self._save()
            return None

    def allowance(self, visitor: str, remote: bool) -> int:
        return number("TRIAL_REMOTE_TOKENS" if remote else "TRIAL_VISITOR_TOKENS")

    def tokens_used(self, visitor: str) -> int:
        with self.lock:
            return int(self._today().setdefault("tokens", {}).get(visitor, 0))

    def reserve_tokens(self, visitor: str, n: float, remote: bool) -> bool:
        """Count `n` of today's allowance before a call; False (nothing counted) if it doesn't fit."""
        with self.lock:
            tokens = self._today().setdefault("tokens", {})
            if int(tokens.get(visitor, 0)) + n > self.allowance(visitor, remote):
                return False
            tokens[visitor] = int(tokens.get(visitor, 0)) + round(n)
            self._save()
            return True

    def settle_tokens(self, visitor: str, reserved: float, used: float) -> None:
        """Ollama's count replaces the estimate (a failed call keeps it)."""
        with self.lock:
            tokens = self._today().setdefault("tokens", {})
            tokens[visitor] = max(0, int(tokens.get(visitor, 0)) + round(max(0.0, used) - reserved))
            self._save()

    def give_back(self, visitor: str) -> None:
        """A message that never reached the model (budget, or they left meanwhile) doesn't count."""
        with self.lock:
            data = self._today()
            if int(data["visitors"].get(visitor, 0)) > 0:
                data["visitors"][visitor] = int(data["visitors"][visitor]) - 1
                data["server"] = max(0, int(data.get("server", 0)) - 1)
                self._save()

    def first_notice(self, visitor: str, cap: str) -> bool:
        """True once a day per visitor and cap: the limit is said once, then the agent stays quiet."""
        with self.lock:
            told = self._today().setdefault("told", {}).setdefault(visitor, [])
            if cap in told:
                return False
            told.append(cap)
            self._save()
            return True
