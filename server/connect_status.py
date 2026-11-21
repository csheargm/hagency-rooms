"""The Connect page's live status (connect_page.py): POST /connect/status {"code": ...} answers only
{"state": "waiting" | "connected" | "expired"} for that one-time code.

  waiting:   the code is fresh and not used yet;
  connected: connect.py on the person's computer just traded it for the sign-in (POST /connect);
  expired:   anything else (wrong, expired, used long ago, or forgotten after a restart).

It says nothing else: not whose code it is, nor the agent, kind or server. The code travels in the
body, never in a URL. Limits per client (remote_agent.client_of): at most REQUESTS_PER_MIN asks a
minute, and codes that aren't live count as wrong codes on a limiter of its own, so guessing here
is as hopeless as at POST /connect, and the page's polling can never lock out connect.py.
Used codes are remembered in memory only, by hash, for USED_KEEP_S.
"""
import time

from server import pairing

USED_KEEP_S = pairing.CODE_S
REQUESTS_PER_MIN = 30
STATES = ("waiting", "connected", "expired")


class ClientLimiter(pairing.Limiter):
    """Wrong codes per client only: many clients' wrong guesses never stop anyone's status (GPT review)."""
    def blocked(self, client: str, now: float | None = None) -> bool:
        return len(self._recent(client, time.time() if now is None else now)) >= pairing.FAILS_PER_CLIENT

    def failed(self, client: str, now: float | None = None) -> None:
        """Recorded for this client only: no shared list that grows with every guesser (GPT re-check)."""
        now = time.time() if now is None else now
        self._recent(client, now).append(now)
        if len(self.fails) > 10_000:          # many clients: forget the quiet ones
            for c in [c for c, ts in self.fails.items() if not ts or ts[-1] <= now - pairing.FAIL_WINDOW_S]:
                self.fails.pop(c, None)


LIMIT = ClientLimiter()                      # wrong codes asked about here; not connect.py's limiter
_used: dict[str, float] = {}                 # hash(code) → when connect.py used it
_asks: dict[str, list[float]] = {}           # client → recent asks
_redeeming: set[str] = set()                 # hash(code) of codes POST /connect is answering right now


def redeeming(code: str) -> None:
    """POST /connect took this code and is looking up the sign-in: still "waiting", never "expired"."""
    _redeeming.add(pairing.digest(code))


def redeemed(code: str) -> None:
    """That lookup ended (mark_used follows when it gave the sign-in)."""
    _redeeming.discard(pairing.digest(code))


def mark_used(code: str, now: float | None = None) -> None:
    """POST /connect handed out the sign-in for this code."""
    now = time.time() if now is None else now
    for h, at in list(_used.items()):
        if at <= now - USED_KEEP_S:
            _used.pop(h, None)
    _used[pairing.digest(code)] = now


def state(codes: pairing.Codes, code: object, now: float | None = None) -> str:
    """One of STATES for `code`; never anything about its person."""
    now = time.time() if now is None else now
    if not isinstance(code, str) or not code or len(code) > pairing.MAX_CODE_CHARS:
        return "expired"
    used = _used.get(pairing.digest(code))
    if used is not None and used > now - USED_KEEP_S:
        return "connected"
    return "waiting" if pairing.digest(code) in _redeeming or codes.live(code, now) else "expired"


def too_many(client: str, now: float | None = None) -> bool:
    """This client asked too often lately, or got too many wrong codes; counts this ask."""
    now = time.time() if now is None else now
    if len(_asks) > 10_000:                  # many clients: forget the quiet ones
        for c in [c for c, ts in _asks.items() if not ts or ts[-1] <= now - 60]:
            _asks.pop(c, None)
    recent = [t for t in _asks.get(client, []) if t > now - 60]
    if len(recent) >= REQUESTS_PER_MIN or LIMIT.blocked(client, now):   # refused asks aren't counted
        _asks[client] = recent
        return True
    _asks[client] = [*recent, now]
    return False
