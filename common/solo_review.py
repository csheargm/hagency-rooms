"""Solo review (docs/adr/0023-solo-review.md), the parts both sides share (R3): @pm (server/pm_solo.py) and an
agent adapter (adapters/agents/solo_github.py) read and write exactly these.

A room's switch lives in its own Matrix state (SOLO_EVENT), written only by @pm after its checks, and it
counts only if @pm wrote it (like the room's repo and mode: room_state.binding_in). ON by default (owner
decision): a room that never set it is on ("default"), an explicit off stays off, and any other sender's event
is off (fail closed). Its GitHub part (the admin bypass asked for, added or known) names its repo: it never
carries over to another repo, and @pm says what was left on the old one (pm_solo_rules.follow_repo).
"""
from nio import AsyncClient, RoomGetStateResponse

from common import room_state

SOLO_EVENT = "xyz.hagencylabs.solo_review"
TASK_KIND = "solo_ruleset"     # @pm → the room owner's agent: let repo admins merge past GitHub's review rule
# GitHub's "Repository admin" role (actor_id 5), allowed past a ruleset only when merging a pull request.
ADMIN_BYPASS = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
NO_BYPASS = "GitHub's review rule doesn't let this account merge past it"   # in a failed merge's error
BYPASS_STEPS = ("**To let it merge:** on GitHub open **{repo} → Settings → Rules → Rulesets**, open the rule that "
                "requires a pull request review, and under **Bypass list** add **Repository admin** with "
                "**For pull requests only**.")
TAKE_OUT = ("On GitHub open **{repo} → Settings → Rules → Rulesets** and take **Repository admin** out of the "
            "Bypass list of the review rule (Hagency added it there).")


def content(on: bool, added: list[int] | None = None, repo: str | None = None, ok: bool = False,
            default: bool = False, asked: str | None = None) -> dict:
    """added: the rulesets whose admin bypass Hagency itself added (only those are taken out again); repo: the
    repo the GitHub part is about; ok: GitHub's review rule is known to let repo admins merge past it; default:
    nobody chose on or off (on); asked: the repo whose bypass @pm already asked a 👍 for (asked once)."""
    return {"on": on is True, "bypass_added": sorted({int(i) for i in (added or [])}),
            "repo": repo if isinstance(repo, str) else None, "ok": ok is True, "default": default is True,
            "asked": asked if isinstance(asked, str) else None}


def default_state() -> dict:
    """A room that never set it: on, nobody's choice."""
    return content(True, default=True)


def state_in(events: list, pm: str) -> dict:
    """The room's SOLO_EVENT content if @pm wrote it; none at all: the default (on); anyone else's or unparsable: off."""
    for ev in events:
        if isinstance(ev, dict) and ev.get("type") == SOLO_EVENT and ev.get("state_key") == "":
            c = ev.get("content")
            if ev.get("sender") != pm or not isinstance(c, dict):
                return content(False)
            added = c.get("bypass_added")
            ids = [i for i in added if isinstance(i, int)] if isinstance(added, list) else []
            return content(c.get("on") is True, ids, c.get("repo"), c.get("ok"), c.get("default"), c.get("asked"))
    return default_state()


def event_state(ev: object, pm: str) -> dict:
    """One GET of SOLO_EVENT (?format=event): never set (404 M_NOT_FOUND) is the default; any other error is off."""
    if isinstance(ev, dict) and "_status" in ev:
        return default_state() if ev.get("_status") == 404 and ev.get("errcode") == "M_NOT_FOUND" else content(False)
    if not isinstance(ev, dict) or ev.get("type") != SOLO_EVENT or ev.get("state_key") != "":
        return content(False)             # GPT review: a malformed answer is never "never set"
    return state_in([ev], pm)


def live(state: dict, bound_repo: str | None) -> bool:
    """On (by default or by choice) in a room with a repo. The bypass consent is per repo (bypass_ok)."""
    return state["on"] and bool(bound_repo)


def bypass_ok(state: dict, repo: str | None) -> bool:
    """GitHub's review rule on `repo` is known to let repo admins merge past it (recorded for that very repo)."""
    return state["ok"] and bool(repo) and state["repo"] == repo


async def read(client: AsyncClient, room_id: str, strict: bool = False) -> dict:
    """This room's solo review state now, with "bound": the room's repo from the same read. Unreadable: off (fail
    closed), or with strict, room_state.StateUnreadable (never the base of a write: review)."""
    r = await client.room_get_state(room_id)
    if not isinstance(r, RoomGetStateResponse):
        if strict:
            raise room_state.StateUnreadable(f"couldn't read {room_id}'s state: {r}")
        return {**content(False), "bound": None}
    pm = room_state.pm_id(client)
    return {**state_in(r.events, pm), "bound": room_state.repo_in(room_state.binding_in(r.events, pm))}


async def enabled(client: AsyncClient, room_id: str) -> bool:
    state = await read(client, room_id)
    return live(state, state["bound"])


async def still_allowed(client: AsyncClient, room_id: str, repo: str | None) -> bool:
    """An admin merge into `repo` may still go ahead: the room is Autonomous, solo review is on, and `repo` is the
    room's repo, read now (review: never into another repo)."""
    state = await read(client, room_id)
    return live(state, state["bound"]) and state["bound"] == repo and await room_state.level(client, room_id) == "autonomous"


PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")
WAITING = ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED")


def checks_state(rollup: object) -> str:
    """"passed", "pending" or "failed" for a PR's statusCheckRollup (none at all: passed). A merge past the review
    rule (--admin) must never skip a red or unfinished check."""
    state = "passed"
    for c in rollup if isinstance(rollup, list) else [None]:
        if not isinstance(c, dict):
            return "failed"
        if c.get("conclusion") in PASSED or c.get("state") == "SUCCESS":
            continue
        if c.get("status") in WAITING or c.get("state") in WAITING or (c.get("status") and not c.get("conclusion")
                                                                       and c.get("status") != "COMPLETED"):
            state = "pending"
        else:
            return "failed"
    return state


def is_admin_bypass(actor: object) -> bool:
    """An entry that already lets repository admins past (pull requests only, or always)."""
    return (isinstance(actor, dict) and actor.get("actor_type") == ADMIN_BYPASS["actor_type"]
            and actor.get("actor_id") == ADMIN_BYPASS["actor_id"] and actor.get("bypass_mode") in ("pull_request", "always"))
