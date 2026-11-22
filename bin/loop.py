"""Starts server/loop.py, which runs on the server. This path stays the same:
installed agents, services and settings name it (docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("server.loop", run_name="__main__", alter_sys=True)
