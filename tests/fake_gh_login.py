#!/usr/bin/env python3
"""Stand-in for `gh auth login --web` (its device sign-in), for test_agent_github.py. NOT used in production.

FAKE_GH_MODE picks what it does: "ok" (prints a code, waits for Enter, signs in), "fail" (a code, then exit 1),
"hang" (a code, then waits), "flood" (a code, then far more output than a pipe holds, then waits), "badcode" (a code
in the wrong shape), "silent" (no code at all), "old" (its help lacks --insecure-storage). What it was given (its
environment, stdin) and its pid go to FAKE_GH_LOG as JSON. `gh api` (the token check) answers as FAKE_GH_API says:
"ok" (the default), "revoked" (GitHub's 401), "none" (no token: gh's exit 4), "down" (no network), "hang"; what
it was given goes to FAKE_GH_API_LOG, if set.
"""
import json
import os
import sys
import time
from pathlib import Path

CODE, LOGIN, TOKEN = "WDJB-MJHT", "octo-cat", "gho_" + "x" * 36


def main() -> int:
    mode, args = os.environ.get("FAKE_GH_MODE", "ok"), sys.argv[1:]
    if args[:1] == ["api"]:
        return api(os.environ.get("FAKE_GH_API", "ok"))
    if args[-1:] == ["--help"]:
        print("  --web  Open a browser" + ("" if mode == "old" else "\n  --insecure-storage  Save in plain text"))
        return 0
    if mode == "silent":
        print("something else entirely", flush=True)
        return 1
    print(f"! First copy your one-time code: {'rm -rf ~' if mode == 'badcode' else CODE}", flush=True)
    print(f"token for the curious: {TOKEN}", flush=True)                 # never sent anywhere
    entered = sys.stdin.readline()
    keys = ("GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "BROWSER", "GH_BROWSER", "GH_TOKEN", "GITHUB_TOKEN")
    Path(os.environ["FAKE_GH_LOG"]).write_text(json.dumps({"args": args, "stdin": entered, "pid": os.getpid(),
                                                           "env": {k: os.environ.get(k) for k in keys}}))
    if mode == "flood":
        sys.stdout.write("x" * 200_000 + "\n")           # blocks once the pipe is full: nobody reads it now
        sys.stdout.flush()
    if mode in ("hang", "flood"):
        time.sleep(60)
    if mode == "fail":
        return 1
    hosts = Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"
    hosts.write_text(f"github.com:\n    oauth_token: {TOKEN}\n    git_protocol: https\n    user: {LOGIN}\n")
    print("✓ Authentication complete.", flush=True)
    return 0


def api(mode: str) -> int:
    if os.environ.get("FAKE_GH_API_LOG"):
        Path(os.environ["FAKE_GH_API_LOG"]).write_text(json.dumps({"args": sys.argv[1:], "env": {
            k: os.environ.get(k) for k in ("GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN")}}))
    if mode == "hang":
        time.sleep(60)
    if mode == "ok":
        print(LOGIN)
        return 0
    print({"revoked": "gh: Bad credentials (HTTP 401)", "none": "To get started with GitHub CLI, please run:  gh auth login",
           "down": "error connecting to api.github.com"}[mode], file=sys.stderr)
    return 4 if mode == "none" else 1


if __name__ == "__main__":
    sys.exit(main())
