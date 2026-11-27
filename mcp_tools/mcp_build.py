"""hagency MCP tools for building, reviewing and merging (see mcp_core.py for the plumbing).
The agent edits files only through read_file/write_files, which are confined to the task's
checkout; git and GitHub run under the agent's own identity in tasks.py."""
import json
from pathlib import Path

from common import github
from mcp_tools import mcp_core as core
from common import room_state, solo_review
from adapters.agents import tasks
from mcp_tools.mcp_core import AGENT, claim, mcp, report, safely

MAX_WRITE_BYTES = 200_000


def save_checkout(task_id: str, co: tasks.Checkout) -> None:
    """On disk, not in memory: the agent may restart this server between its tool calls."""
    (co.root / "task.json").write_text(json.dumps({"task_id": task_id, "issue": co.issue}))


def saved_checkout(task_id: str, t: dict) -> tasks.Checkout | None:
    n = int(t["issue"])
    root = core.base_for(t) / f"issue-{n}"
    try:
        saved = json.loads((root / "task.json").read_text())
    except (OSError, ValueError):
        return None                      # no checkout for any task yet
    if saved.get("task_id") != task_id or not (root / "git").is_dir():
        return None                      # an older task's leftovers: start_build clones afresh
    return tasks.Checkout(n, saved["issue"], root, f"agent/{AGENT}/issue-{n}", t["repo"])


@mcp.tool()
async def start_build(task_id: str) -> dict:
    """Clone the repo for a build task; you then edit files in the returned folder."""
    problem, t = claim(task_id, "build")
    if problem:
        return problem

    async def step():
        co = saved_checkout(task_id, t)
        if co is None:                   # first call: clone; a repeat reuses it (never wipes edits)
            co = await tasks.prepare_build(AGENT, core.base_for(t), t)
            save_checkout(task_id, co)
            core.post(f"build: claimed #{t['issue']}", room=t.get("_room"))
        t["checkout"] = co
        files = sorted(str(p.relative_to(co.tree)) for p in co.tree.rglob("*") if p.is_file())
        return {"checkout": f"complete: the repo's default branch, {len(files)} file(s)",
                "files": files[:200], "instructions": tasks.build_prompt(co),
                "next": "Read files with read_file(task_id, path) and change them with "
                        "write_files(task_id, {path: full new content}); paths are relative to the repo "
                        "(e.g. docs/NOTE.md). Then call finish_build(task_id, summary)."}
    return await safely(task_id, "build", step)


@mcp.tool()
async def start_fix(task_id: str) -> dict:
    """Your PR got "changes requested": check out your own PR branch and read the review."""
    problem, t = claim(task_id, "fix")
    if problem:
        return problem

    async def step():
        co = saved_checkout(task_id, t)
        if co is None:
            co = await tasks.prepare_fix(AGENT, core.base_for(t), t)
            save_checkout(task_id, co)
            core.post(f"fix: revising PR #{t['pr']} after the review", room=t.get("_room"))
        t["checkout"] = co
        return {"instructions": tasks.fix_prompt(co, str(t.get("feedback", ""))),
                "next": "Change files with read_file/write_files as for a build, then call "
                        "finish_fix(task_id, summary)."}
    return await safely(task_id, "fix", step)


@mcp.tool()
async def finish_fix(task_id: str, summary: str) -> dict:
    """Commit and push your revision to the same PR; the reviewer looks again."""
    problem, _ = claim(task_id, "fix")                      # exactly a fix
    if problem:
        return problem
    problem, co = build_checkout(task_id)
    if problem:
        return problem
    t = core._open[task_id]

    async def step():
        return {"done": report(task_id, await tasks.finish_fix(AGENT, co, summary, int(t["pr"]))),
                "next": "Stop; the reviewer looks again."}
    return await safely(task_id, "fix", step)


def repo_path(co: tasks.Checkout, path: str) -> Path:
    """A path inside the checkout; anything that could reach outside it (or into .git) is refused."""
    rel = Path(path)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] == ".git":
        raise ValueError(f"path {path!r} must be relative to the repo, without '..' or .git")
    full = (co.tree / rel).resolve()
    if not full.is_relative_to(co.tree.resolve()):
        raise ValueError(f"path {path!r} leaves the repo")
    return full


def build_checkout(task_id: str) -> tuple[dict | None, tasks.Checkout | None]:
    problem, t = claim(task_id, ("build", "fix"))     # the file tools work on a build or a fix checkout
    if problem:
        return problem, None
    co = t.get("checkout") or saved_checkout(task_id, t)
    if co is None:
        return {"error": "no checkout yet", "next": "Call start_build(task_id) first."}, None
    t["checkout"] = co
    return None, co


@mcp.tool()
def read_file(task_id: str, path: str) -> dict:
    """Read one file of the build's checkout (path relative to the repo)."""
    problem, co = build_checkout(task_id)
    if problem:
        return problem
    try:
        return {"path": path, "content": repo_path(co, path).read_text(errors="replace")[:MAX_WRITE_BYTES]}
    except (OSError, ValueError) as e:
        return {"error": str(e)}


@mcp.tool()
def write_files(task_id: str, files: dict[str, str]) -> dict:
    """Create or replace files in the build's checkout: {relative path: full new content}."""
    problem, co = build_checkout(task_id)
    if problem:
        return problem
    try:
        targets = {path: repo_path(co, path) for path in files}       # check every path before writing any
        if sum(len(c.encode()) for c in files.values()) > MAX_WRITE_BYTES:
            raise ValueError(f"more than {MAX_WRITE_BYTES} bytes in one call")
        for path, full in targets.items():
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(files[path])
    except (OSError, ValueError) as e:
        return {"error": str(e), "next": "Fix the paths and call write_files again."}
    return {"written": sorted(files), "next": "Call finish_build(task_id, summary) when every change is made."}


@mcp.tool()
async def finish_build(task_id: str, summary: str) -> dict:
    """Commit your edits, push the branch and open the PR (as your GitHub identity)."""
    problem, _ = claim(task_id, "build")                    # exactly a build: a fix never opens a new PR
    if problem:
        return problem
    problem, co = build_checkout(task_id)
    if problem:
        return problem

    async def step():
        return {"done": report(task_id, await tasks.finish_build(AGENT, "Hermes", co, summary)),
                "next": "Stop; pm takes it from here."}
    return await safely(task_id, "build", step)


@mcp.tool()
async def get_review(task_id: str) -> dict:
    """The PR diff and the issue's acceptance criteria for a review task."""
    problem, t = claim(task_id, "review")
    if problem:
        return problem

    async def step():
        n, pr, issue, head, diff = await tasks.review_material(AGENT, t)
        t["head"] = head
        if len(diff) > tasks.MAX_DIFF_CHARS:
            return {"done": report(task_id, await tasks.submit_review(AGENT, n, pr, head, "REQUEST_CHANGES",
                                                                      "Diff too large to review in full.", t["repo"],
                                                                      t.get("solo") is True)),
                    "next": "Stop."}
        return {"instructions": tasks.review_prompt(n, pr, issue, diff),
                "next": "Decide, then call submit_review(task_id, approve, reasons). approve=true only if "
                        "EVERY acceptance criterion is met and nothing else changed."}
    return await safely(task_id, "review", step)


@mcp.tool()
async def submit_review(task_id: str, approve: bool, reasons: str) -> dict:
    """Post your review on GitHub (pinned to the commit you reviewed) and report it."""
    problem, t = claim(task_id, "review")
    if problem:
        return problem

    if "head" not in t:
        return {"error": "review not loaded yet", "next": "Call get_review(task_id) first."}

    async def step():
        verdict = "APPROVE" if approve else "REQUEST_CHANGES"
        result = await tasks.submit_review(AGENT, int(t["issue"]), int(t["pr"]), t["head"], verdict, reasons[:3000],
                                             t["repo"], t.get("solo") is True)   # solo review: a comment (ADR 0023)
        return {"done": report(task_id, result), "next": "Stop."}
    return await safely(task_id, "review", step)


def require_autonomous(t: dict) -> None:
    """Merging and closing happen only while the room is Autonomous, re-read from the room now."""
    binding = core.api("GET", core.room_path(t["_room"], f"/state/{github.LEVEL_EVENT}/"), token=core.token())
    level = room_state.level_of(binding)
    if level != "autonomous":
        raise tasks.TaskError(f"room is {level}, not autonomous")
    if t.get("admin") is True:   # an admin merge (solo review, ADR 0023): only while @pm's own switch is on, read now
        ev = core.api("GET", core.room_path(t["_room"], f"/state/{solo_review.SOLO_EVENT}/?format=event"), token=core.token())
        if not solo_review.live(solo_review.state_in([ev], core.PM_ID), room_state.repo_in(binding)):
            raise tasks.TaskError("solo review is off in this room")


@mcp.tool()
async def merge_pr(task_id: str) -> dict:
    """Merge the approved PR, only if the room is Autonomous and GitHub says it is ready."""
    problem, t = claim(task_id, "merge")
    if problem:
        return problem

    async def still() -> bool:            # read again right before an admin merge (ADR 0023)
        require_autonomous(t)
        return True

    async def step():
        require_autonomous(t)
        return {"done": report(task_id, await tasks.merge(AGENT, "Hermes", core.base_for(t), {**t, "_still": still})),
                "next": "Stop."}
    return await safely(task_id, "merge", step)

