# 0012: Launchers in bin/, a root with only what must be there

## Status
Accepted, 2026-11-21. Changes ADR 0011's rule "the root holds only launchers, under the old names".

## Context
After ADR 0011 the root still held eleven 5-line launchers, the local test stack (Compose file,
Element and Synapse settings, `start.sh`, `local.env.example`), the two installers, the GitHub
ruleset and two long logs. ADR 0011 kept the launchers at the root because installed agents'
services name them by path. The product is still in development with very few installs, so we
organise the root fully now and pay a one-time migration, rather than carry the clutter.

## Decision
```
bin/      the 11 launchers: bridge, connect, connect_win, loop, supervise, setup_accounts,
          hermes_local, hagency_mcp, onboard, local_signup, github_setup (.py)
local/    the local test server: start.sh, docker-compose.yml, element-config.json,
          synapse-overrides.yaml, local.env.example (local/local.env, git-ignored)
client/   also connect.sh and connect.ps1 (still served at /connect.sh and /connect.ps1)
docs/     also REVIEWS.md, TIMELOG.md and github-ruleset.json (beside GITHUB_SETUP.md)
<root>    README.md, LICENSE, SECURITY.md, CONTRIBUTING.md, requirements.txt, .github/, .gitignore
          and the folders
```
- A launcher sets `sys.path[0]` to the repo (not `bin/`) and runs its module with `runpy`, so it
  works by path from any folder, and `import onboard` can never load a launcher.
- `common/paths.py` names the folder once (`LAUNCHERS`, `launcher(app, name)`); it is in `CODE_DIRS`,
  so `supervise.py` restarts its children when a launcher changes.
- The installers are served by @pm from `client/`; their public URLs (`/connect.sh`, `/connect.ps1`,
  in people's chat history) are unchanged. Nothing fetches them from raw.githubusercontent.
- `tests/test_layering.py` checks the exact list of root entries, that `bin/` holds only launchers,
  and that each finds its code when run by path from another folder.
- Names saved outside the code still never change: the device ID `BRIDGE_<OWNER>`,
  `.bridge-session.json`, the `bridge` logger.

## Migration for existing installs
A background service written before this names `~/.hagency/app/bridge.py` or `hermes_local.py`
(launchd, systemd), or a Windows task runs `…\app\connect_win.py`. The only update path is the
connect line (`git pull`, then `bin/connect.py`), so the rewrite happens there:
- `bin/connect.py` first calls `agent_service.migrate(APP)`: every launchd plist / systemd unit of
  **this installation** (all agents, further ones too) whose arguments name an old root launcher is
  rewritten to `bin/` with the same escaping/quoting, then loaded again (launchd: bootout + bootstrap,
  since it keeps the loaded copy; systemd: daemon-reload). Other installations' services are left
  alone. If the service manager refuses, the file is put back as it was (so the next run tries again),
  the failure is printed and the run goes on (it installs this agent's own service afresh anyway).
- Windows: `connect.ps1` recognises keepers at both the old and the new path (`$Keepers`), so it
  stops and replaces the old task; new tasks run `bin\connect_win.py`. Right after the update,
  `Move-OldKeepers` points the stopped task or shortcut at `bin\`, so a run that then fails (an
  expired code) starts a keeper that exists. `agent_service._ours` (stop) recognises both too.
- Hermes profiles' `hagency_mcp.py` path is rewritten by `hermes_profile.set_code_dir` every time the
  supervisor starts a Hermes gateway, and by the connect run's set-up.
- An owner's own version (customize) made before the move has no `bin/bridge.py`: it runs the
  official version instead of failing to start.
- `local/start.sh` still reads a `./local.env` left at the root.
- Tested in `tests/test_service_migration.py` with services exactly as the old code wrote them.

Compatibility stubs at the old root names were considered and not kept: the rewrite is exact
(it replaces only the argument strings the old code wrote for this installation's own path) and
runs on the one path every update takes.

TODO(remove after 2027-02-01): `agent_service.OLD_ROOT_LAUNCHERS`, the old half of `keeper_paths`
and `migrate`, the root entry of `$Keepers` and `Move-OldKeepers` in `connect.ps1`, and the `./local.env` fallback in
`local/start.sh`.

## Consequences
- Until its owner re-runs the connect line, an installed agent runs as before (nothing pulls code
  by itself). Right after the pull, before `bin/connect.py` runs, a crashing agent can't be restarted
  from the old path; the same run fixes it seconds later.
- A command copied from an older message (`~/.hagency/app/connect.py --stop`) no longer exists after
  the update: it is now `~/.hagency/app/bin/connect.py --stop`; Disconnect in the panel still works.
- A local `start.sh` supervisor that was already running when its checkout is updated starts its
  children by the old relative path: start it again as `local/start.sh`.
- The installer comes from the deployed server, the code from master. Between this merge and its
  deploy, the previous server's installer pulls the new tree and then can't find the root
  `connect.py`: deploy right after merging. For the same reason, don't roll the public server back
  to a commit from before this change; fix forward instead. Root stubs would cover this window, at
  the cost of a root with 11 files kept for one release. We chose not to keep them, given very few
  installs and a window of minutes.
- `deploy/release.py` and `bootstrap.sh` now look for `bin/loop.py`; deploying a commit from before
  this change uses that commit's own copies, so a rollback still works.
