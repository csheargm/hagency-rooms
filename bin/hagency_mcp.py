"""Starts mcp_tools/hagency_mcp.py, which runs on an agent's computer (its MCP tools). This path stays the same:
installed agents, services and settings name it (docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("mcp_tools.hagency_mcp", run_name="__main__", alter_sys=True)
