"""Starts server/github_setup.py (an operator tool). This path stays the same: docs and @pm's messages name it
(docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("server.github_setup", run_name="__main__", alter_sys=True)
