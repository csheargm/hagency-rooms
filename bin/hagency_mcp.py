"""Starts mcp_tools/hagency_mcp.py, which runs on an agent's computer (its MCP tools).
Run it by path from any folder (docs/adr/0012-launchers-in-bin.md)."""
import runpy
import sys
from pathlib import Path

sys.path[0] = str(Path(__file__).resolve().parent.parent)   # the repo, not bin/: its packages import from there
runpy.run_module("mcp_tools.hagency_mcp", run_name="__main__", alter_sys=True)
