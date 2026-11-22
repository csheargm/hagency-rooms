"""Live check with the real Codex CLI and the owner's real config: an agent adapter turn sees no
connectors (Gmail, Drive, ...), MCP servers, web search or computer/browser use.
Run: .venv/bin/python -m tests.test_codex_lockdown_live   (needs `codex` signed in)
"""
import asyncio
import re
import tempfile
from pathlib import Path

from adapters.agents import agents
from tests.test_e2e import check

FORBIDDEN = re.compile(r"mcp__|gmail|drive|calendar|outlook|notion|computer|browser|web__|cua_|node_repl", re.I)
ASK = "List the names of every tool available to you right now, including built-ins and any MCP namespaces. One line, comma-separated."


def main():
    for write in (False, True):        # chat turns (read-only) and loop builds (workspace-write)
        text = asyncio.run(agents.run_turn("codex", Path(tempfile.mkdtemp()), ASK, write=write, fresh=True))
        found = sorted({m.group(0).lower() for m in FORBIDDEN.finditer(text)})
        check(f"{'build' if write else 'chat'} turn sees no connectors/MCP/web/computer use", not found,
              f"found {found}" if found else text[:160])


if __name__ == "__main__":
    main()
