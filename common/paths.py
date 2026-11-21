"""Where the repo is, for code that now lives one folder down (docs/adr/0011-repo-layout.md).

ROOT is the repo (or installed app) folder: the launchers, static/, .env, agents.json, data/ and
rooms/ are there, as before the move. CODE_DIRS are the folders holding the code itself.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
CODE_DIRS = ("common", "client", "server", "adapters", "mcp_tools")
