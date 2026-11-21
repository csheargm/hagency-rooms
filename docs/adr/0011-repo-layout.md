# 0011: Repo layout by where code runs

## Status
Accepted, 2026-11-21.

## Context
About 100 Python modules sat at the repo root, with nothing to show which run on the server, which on
people's computers, and which on both. Hagency is growing two kinds of adapters around the Matrix hub
(ADR 0009, channel-neutral): **agent adapters** (an AI ↔ Matrix, today's "bridge") and **channel
adapters** (a chat app ↔ Matrix: Telegram, Signal, SMS…). A server agent may later run the same agent
adapter on the server. The layout should say all this.

Paths on people's computers can't change: launchd/systemd entries and Windows tasks run
`~/.hagency/app/bridge.py` or `connect_win.py`, MCP settings name `hagency_mcp.py`, docs and @pm's
messages name `connect.py`, `github_setup.py`, `local_signup.py`.

## Decision
```
server/            @pm, console, panel, Connect page, federation       (the VM only)
client/            connect, install, background service, local AI setup (people's computers only)
adapters/agents/   the agent adapter: chat loop, rules, per-room turns, @pm tasks, per-AI runners
adapters/channels/ (later) chat-app adapters
common/            Matrix calls, registry, room state, GitHub API, settings, paths
mcp_tools/         the agents' MCP tools (not "mcp": that is the MCP library's name)
<root>/*.py        launchers only, under the old names
```
- **Real packages**, imported by full name (`from common import registry`). A launcher shares its
  name with the code it starts (`bridge.py` → `adapters/agents/bridge.py`), so flat module names on
  `sys.path` would have made `import bridge` load the launcher.
- **Launchers** are three lines: `runpy.run_module("<package>.<name>", run_name="__main__")`. Every
  path stored on a computer or written in docs keeps working, with no migration step.
- **Rules**, enforced by `tests/test_layering.py`: `common/` imports only `common/`; `adapters/`
  only `adapters/` and `common/`; `server/` and `client/` never import each other; `mcp_tools/`
  imports neither. @pm's process never loads the agent adapter or `client/`. Module names stay unique
  across packages (`customize.SENSITIVE` matches by file name). The root holds only launchers.
- `common/paths.py` is the one place that knows where the repo folder is (`ROOT`) and which folders
  hold code (`CODE_DIRS`: `supervise.py` restarts children when any of them changes).

## Consequences
- An installed agent keeps running its old code until its owner re-runs the connect line (as for any
  update). Its stored paths still work after the update, because the launchers keep the old names.
- A `supervise.py` that was already running when its folder is updated keeps its old watcher, which
  looks only at the root: restart it once (`./start.sh` again, or re-run the connect line for a
  Hermes agent) so edits inside the package folders restart its children again (GPT review).
- Names saved outside the code stay as they are, on purpose: the Matrix device ID `BRIDGE_<OWNER>`,
  `.bridge-session.json`, the `bridge` logger. Renaming them would sign agents in as new devices or
  make them forget conversations.
- Tests stay in one `tests/` folder for now; mirroring them per package is a later, separate move.
