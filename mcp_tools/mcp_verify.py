"""hagency MCP tools for "is this issue already done?": the builder found nothing to change, so a
second person's agent checks the repo (read-only) and, once confirmed, the builder closes the issue.
The same steps the Codex/Claude bridge runs in tasks.verify / tasks.close."""
import os
from pathlib import Path

from mcp_tools import mcp_core as core
from adapters.agents import tasks
from mcp_tools.mcp_build import require_autonomous
from mcp_tools.mcp_core import AGENT, claim, mcp, report, safely


MAX_FILES = 500


def repo_text(tree: Path) -> str:
    """Every text file in the clone, path first, all of it counted; refuses what can't be read in full."""
    parts, size = [], 0
    paths = sorted(Path(d) / f for d, dirs, files in os.walk(tree) for f in files)   # never follows links
    paths = [p for p in paths if ".git" not in p.relative_to(tree).parts]
    if len(paths) > MAX_FILES:
        raise tasks.TaskError("the repository has too many files to check in full here")
    for p in paths:
        if p.is_symlink():                         # a link could point anywhere on this computer
            body = "(a symbolic link; not followed)"
        elif p.stat().st_size > tasks.MAX_DIFF_CHARS:
            raise tasks.TaskError("the repository is too large to check in full here")
        else:
            try:
                body = p.read_text()
            except (UnicodeDecodeError, OSError):
                body = "(binary or unreadable file)"   # listed, so the agent knows it exists
        part = f"=== {p.relative_to(tree)} ===\n{body}"
        size += len(part) + 2
        if size > tasks.MAX_DIFF_CHARS:            # never confirm what the checker couldn't fully see
            raise tasks.TaskError("the repository is too large to check in full here")
        parts.append(part)
    return "\n\n".join(parts)


@mcp.tool()
async def get_verify(task_id: str) -> dict:
    """The issue and the repository's files, to check the issue is ALREADY satisfied."""
    problem, t = claim(task_id, "verify")
    if problem:
        return problem

    async def step():
        n, tree, prompt = await tasks.verify_material(AGENT, core.base_for(t), t)
        t["verify_loaded"] = True
        return {"instructions": f"{prompt}\n\nThe repository's files:\n\n{repo_text(tree)}",
                "next": "Decide, then call submit_verify(task_id, already_done, reasons). already_done=true "
                        "only if EVERY acceptance criterion is met by the files as they are."}
    return await safely(task_id, "verify", step)


@mcp.tool()
async def submit_verify(task_id: str, already_done: bool, reasons: str) -> dict:
    """Report whether the issue is already satisfied."""
    problem, t = claim(task_id, "verify")
    if problem:
        return problem
    if not t.get("verify_loaded"):
        return {"error": "not loaded yet", "next": "Call get_verify(task_id) first."}

    async def step():
        verdict = "APPROVE" if already_done else "REQUEST_CHANGES"
        return {"done": report(task_id, tasks.verify_result(int(t["issue"]), verdict, reasons[:3000])), "next": "Stop."}
    return await safely(task_id, "verify", step)


@mcp.tool()
async def close_issue(task_id: str) -> dict:
    """Close an issue another agent confirmed is already done (only while the room is Autonomous)."""
    problem, t = claim(task_id, "close")
    if problem:
        return problem

    async def step():
        require_autonomous(t)
        return {"done": report(task_id, await tasks.close(AGENT, "Hermes", core.base_for(t), t)), "next": "Stop."}
    return await safely(task_id, "close", step)
