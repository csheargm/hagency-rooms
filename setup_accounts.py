"""Starts common/setup_accounts.py, which runs on the server and people's computers. This path stays the same:
installed agents, services and settings name it (docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("common.setup_accounts", run_name="__main__", alter_sys=True)
