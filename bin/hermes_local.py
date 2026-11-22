"""Starts client/hermes_local.py, which runs on a person's own computer. This path stays the same:
installed agents, services and settings name it (docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("client.hermes_local", run_name="__main__", alter_sys=True)
