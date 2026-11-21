"""Starts common/local_signup.py (an operator tool). This path stays the same: docs and deploy/invite.sh name it
(docs/adr/0011-repo-layout.md)."""
import runpy

runpy.run_module("common.local_signup", run_name="__main__", alter_sys=True)
