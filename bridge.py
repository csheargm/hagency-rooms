"""Starts adapters/agents/bridge.py, which runs wherever an agent runs. This path stays the same:
installed agents, services and settings name it (docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("adapters.agents.bridge", run_name="__main__", alter_sys=True)
