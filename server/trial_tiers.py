"""Usage tiers for the server agent (ADR 0025): each person's tier record and every tier's limits, here
and only here (R3). Everyone is on FREE unless their record says otherwise; there is no payment code
and no way yet to get another tier, so for now only FREE exists. A paid tier is a new TIERS entry
(its limits) plus whatever later sets a person's record (set_tier).

A tier's limit is a setting name (env-configurable, read by trial_limits.Usage with its defaults) or a
fixed number. "tokens_remote" is the allowance for people from other servers (no sign-up here: less).
"""
import time
from pathlib import Path

from common.saved_dict import SavedDict

FREE = "free"
MESSAGES, TOKENS, TOKENS_REMOTE = "messages", "tokens", "tokens_remote"   # a person's daily limits
BUILDS_DAY, BUILDS_TOTAL = "builds_day", "builds_total"   # builds (ADR 0027): a UTC day, and ever (per GitHub account)
TIERS = {FREE: {MESSAGES: "TRIAL_PER_VISITOR", TOKENS: "TRIAL_VISITOR_TOKENS", TOKENS_REMOTE: "TRIAL_REMOTE_TOKENS",
                BUILDS_DAY: 3, BUILDS_TOTAL: 10, "upgrade": "More usage is coming with paid plans."}}


class Tiers:
    """person → {"tier", "since"} on disk (data/trial-tiers.json); no file: everyone is FREE."""
    def __init__(self, path: Path | None = None) -> None:
        self.records = SavedDict(path) if path else {}

    def tier(self, person: str) -> str:
        """Their tier; no record, or one naming a tier that no longer exists: FREE."""
        record = self.records.get(person)
        name = record.get("tier") if isinstance(record, dict) else None
        return name if name in TIERS else FREE

    def set_tier(self, person: str, name: str) -> bool:
        """Record their tier (only one that exists); False: no such tier."""
        if name not in TIERS:
            return False
        self.records[person] = {"tier": name, "since": time.time()}
        return True

    def limit(self, person: str, what: str) -> str | int:
        """One of their tier's limits: a setting name or a number (trial_limits.Usage.limit resolves it)."""
        return TIERS[self.tier(person)][what]

    def upgrade_line(self, person: str) -> str:
        """What a reached limit adds about more usage on their tier ("" when there's nothing to say)."""
        return TIERS[self.tier(person)].get("upgrade", "")
