"""hagency MCP tools for planning: draft issues from the room's discussion, then open the ones
the room approved (planning.py does the checking and the GitHub calls)."""
from adapters.agents import planning
from mcp_tools.mcp_core import AGENT, claim, mcp, report, safely


@mcp.tool()
def get_plan_request(task_id: str) -> dict:
    """The room's discussion and the already-planned work, for a plan task."""
    problem, t = claim(task_id, "plan")
    if problem:
        return problem
    return {"instructions": planning.plan_prompt(str(t.get("discussion", "")), [str(x) for x in t.get("existing", [])]),
            "next": "Call submit_plan(task_id, items) with items = [{title, criteria: [..]}] (an empty list "
                    "if nothing new was agreed). Don't post the plan yourself; @pm shows it to the room."}


@mcp.tool()
def submit_plan(task_id: str, items: list[dict]) -> dict:
    """Hand your plan to @pm, which shows it in the room for the owner's ✅."""
    problem, t = claim(task_id, "plan")
    if problem:
        return problem
    try:
        checked = [planning.clean_item(x) for x in items][:planning.MAX_ITEMS]
    except planning.PlanError as e:
        return {"error": str(e), "next": "Fix the items and call submit_plan again."}
    report(task_id, {"kind": "plan", "issue": t.get("issue"), "items": checked,
                     "line": f"plan drafted: {len(checked)} item(s)"})
    return {"done": f"{len(checked)} item(s) sent to @pm", "next": "Say only \"Plan sent to @pm.\" and stop; @pm asks the room for a 👍."}


@mcp.tool()
async def open_issues(task_id: str) -> dict:
    """Open the issues the room approved, under your own GitHub identity."""
    problem, t = claim(task_id, "file_issues")
    if problem:
        return problem

    async def step():
        numbers = await planning.file_issues(AGENT, t["items"], str(t.get("approved_by", "the room owner")),
                                             t["repo"])      # claim() checked it is the room's own repo
        line = "opened " + ", ".join(f"#{n}" for n in numbers) if numbers else "no issues to open"
        return {"done": report(task_id, {"kind": "file_issues", "numbers": numbers, "line": line}), "next": "Stop."}
    return await safely(task_id, "file_issues", step)
