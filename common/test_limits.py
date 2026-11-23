"""HAGENCY_TEST_LIMITS=1 (deploy/.env): higher per-person limits while the site is being tested.

The owner's choice (2026-11-23): testers hit "that's enough for now" long before an abuser would.
Off (any other value, or unset) is the public limits. Only per-person convenience caps change here;
the limits that stop guessing and floods (wrong codes, failed sign-ins, emails, server-wide totals)
never do. Read once at import: @pm's container gets it at start (deploy/docker-compose.yml).
"""
import os

SWITCH = "HAGENCY_TEST_LIMITS"


def on() -> bool:
    return os.environ.get(SWITCH, "").strip() == "1"


def pick(public: int, testing: int) -> int:
    """The test-stage value when the switch is on, else the public one."""
    return testing if on() else public
