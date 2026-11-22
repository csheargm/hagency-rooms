"""Starts adapters/agents/bridge.py, which runs wherever an agent runs.
Run it by path from any folder (docs/adr/0012-launchers-in-bin.md)."""
import runpy
import sys
from pathlib import Path

sys.path[0] = str(Path(__file__).resolve().parent.parent)   # the repo, not bin/: its packages import from there
runpy.run_module("adapters.agents.bridge", run_name="__main__", alter_sys=True)
