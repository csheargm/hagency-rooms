"""hagency MCP server: lets any MCP-capable agent (Hermes, Claude Code, Codex...) take part in a
Hagency room's GitHub loop without a bridge. The agent does the thinking and the file edits;
this server does the rest, the same way the Codex/Claude bridge does (tasks.py):
  - reads the task pm addressed to this agent (only pm, only this agent);
  - clones/commits/pushes/opens PRs/reviews/merges with the agent's OWN GitHub identity
    (.gh/<agent>/), so the agent itself never holds a GitHub token or runs git;
  - posts the result event pm waits for, as the agent's Matrix account.

Run by the agent as a stdio MCP server, e.g. in a Hermes profile's config.yaml:
  mcp_servers: {hagency: {command: .../.venv/bin/python, args: [.../hagency_mcp.py],
                env: {HAGENCY_AGENT: anna-hermes-agent, HAGENCY_ROOM: "!room:server"}}}
"""
from mcp_tools.mcp_build import (finish_build, finish_fix, get_review, merge_pr, read_file, start_build, start_fix,
                       submit_review, write_files)
from mcp_tools.mcp_context import project_status, read_project_file
from mcp_tools.mcp_core import mcp, next_task, report_failure
from mcp_tools.mcp_custom import (finish_code_change, go_back_to_official, read_my_code, start_code_change,
                        write_my_code)
from mcp_tools.mcp_plan import get_plan_request, open_issues, submit_plan
from mcp_tools.mcp_verify import close_issue, get_verify, submit_verify
from mcp_tools.mcp_web import ask_owner, fetch_page, web_search

# Importing the tool modules registers their tools on `mcp`; re-exported for tests and tooling.
__all__ = ["ask_owner", "fetch_page", "web_search", "finish_build", "finish_fix", "get_plan_request", "start_fix", "project_status", "read_project_file", "get_review", "mcp", "merge_pr", "next_task", "open_issues",
           "read_file", "report_failure", "start_build", "submit_plan", "submit_review", "write_files",
           "get_verify", "submit_verify", "close_issue", "start_code_change", "read_my_code", "write_my_code",
           "finish_code_change", "go_back_to_official"]

if __name__ == "__main__":
    mcp.run()
