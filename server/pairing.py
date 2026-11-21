"""One-time pairing codes: how a person's own computer gets their agent's sign-in, exactly once.

@pm gives the person a short code in their private chat; connect.py on their computer trades it
for the agent's credentials (remote_agent.py serves POST /connect). A code works once, for
CODE_S seconds, and only its hash is kept on disk. Wrong codes are rate-limited per client and in
total, so guessing is hopeless.
"""
import hashlib
import secrets
import time
from pathlib import Path

from common.saved_dict import SavedDict

CODE_S = 30 * 60
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L: easy to read out
CODE_LEN = 10                                  # ~49 bits
MAX_CODE_CHARS = 32
FAIL_WINDOW_S = 600
FAILS_PER_CLIENT = 5
FAILS_TOTAL = 50                               # behind a proxy every client may look the same


def normalize(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def digest(code: str) -> str:
    return hashlib.sha256(normalize(code).encode()).hexdigest()


def _same(entry, person: str, agent: str | None) -> bool:
    """A code for this agent of this person (agent None: their first; older codes name no agent)."""
    return isinstance(entry, dict) and entry.get("person") == person and entry.get("agent") == agent


class Codes:
    """hash(code) → {"person", "agent" (a further agent's localpart; absent: the first), "expires"},
    on disk; never the code itself."""
    def __init__(self, path: Path) -> None:
        self.codes = SavedDict(path)

    def issue(self, person: str, now: float | None = None, agent: str | None = None) -> str:
        """A new code for this agent of `person`; its older codes (and every expired one) stop working."""
        now = time.time() if now is None else now
        for h, entry in list(self.codes.items()):
            if not isinstance(entry, dict) or _same(entry, person, agent) or entry.get("expires", 0) <= now:
                self.codes.pop(h, None)
        raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
        self.codes[digest(raw)] = {"person": person, "expires": now + CODE_S, **({"agent": agent} if agent else {})}
        return f"{raw[:5]}-{raw[5:]}"

    def revoke(self, person: str, agent: str | None = None) -> None:
        """Every code of this agent of `person` stops working (it was disconnected): all of them in
        memory first, then one save, so a full disk can't leave some live (raises OSError then)."""
        for h, entry in list(self.codes.items()):
            if not isinstance(entry, dict) or _same(entry, person, agent):
                dict.pop(self.codes, h, None)
        self.codes._save()

    def live(self, code: str, now: float | None = None) -> bool:
        """This code is fresh and unused (it stays so: nothing is changed). For the Connect page's
        status only (connect_status.py)."""
        entry = self.codes.get(digest(code)) if isinstance(code, str) and len(code) <= MAX_CODE_CHARS else None
        now = time.time() if now is None else now
        return isinstance(entry, dict) and isinstance(entry.get("expires"), (int, float)) and entry["expires"] > now

    def redeem(self, code: str, now: float | None = None) -> str | None:
        """The person a fresh code was issued to, or None. The code is gone after this either way."""
        return self.redeem_agent(code, now)[0]

    def redeem_agent(self, code: str, now: float | None = None) -> tuple[str | None, str | None, float]:
        """(person, further agent's localpart or None for their first, when it was issued) for a
        fresh code, else (None, None, 0). The code is gone after this either way."""
        if not isinstance(code, str) or not code or len(code) > MAX_CODE_CHARS:
            return None, None, 0
        entry = self.codes.pop(digest(code), None)
        now = time.time() if now is None else now
        if (not isinstance(entry, dict) or not isinstance(entry.get("expires"), (int, float)) or entry["expires"] <= now
                or not isinstance(entry.get("person"), str) or not entry["person"]):
            return None, None, 0
        agent = entry.get("agent")
        if agent is not None and not (isinstance(agent, str) and agent):
            return None, None, 0              # a damaged entry: never read as the first agent's code
        return entry["person"], agent, entry["expires"] - CODE_S


class Limiter:
    """Failed attempts in the last FAIL_WINDOW_S: per client, and for everyone together."""
    def __init__(self) -> None:
        self.fails: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        kept = [t for t in self.fails.get(key, []) if t > now - FAIL_WINDOW_S]
        self.fails[key] = kept
        return kept

    def blocked(self, client: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return len(self._recent(client, now)) >= FAILS_PER_CLIENT or len(self._recent("*", now)) >= FAILS_TOTAL

    def failed(self, client: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        for key in (client, "*"):
            self._recent(key, now).append(now)
