"""Where the repo is, for code that now lives one folder down (docs/adr/0011-repo-layout.md).

ROOT is the repo (or installed app) folder: static/, .env, agents.json, data/ and rooms/ are
there, as before the move. LAUNCHERS is the folder of the small scripts people, services and
@pm's container run by path (docs/adr/0012-launchers-in-bin.md). CODE_DIRS are the folders
holding the code itself, the launchers included.
"""
from pathlib import Path

ROOT = Path(__file__).parent.parent
LAUNCHERS = "bin"
CODE_DIRS = ("common", "client", "server", "adapters", "mcp_tools", LAUNCHERS)


def launcher(app: Path, name: str) -> Path:
    """The launcher `name` ("bridge.py") of the installation or copy at `app`."""
    return app / LAUNCHERS / name
