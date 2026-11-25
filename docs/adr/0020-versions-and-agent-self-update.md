# 0020: Versions, and one-click agent updates

## Status
Accepted, 2026-11-25.

## Context
- While testing Your cards (ADR 0019), the owner's agents ran code from Nov 22. The panel said "Your
  agent runs older code. Run its connect line…", and there was nothing to click.
- PR #58 added "Update my agent": @pm sends the agent's Connect page, and the person still pastes one
  command on that computer.
- Nobody could see which code the server or an agent ran. SuperElf's footer shows the deployed commit
  and its date (its `REVISION` file, written by its deploy).
- The owner asked for versions, plus a real one-click update.

Self-update is security-sensitive. A program on a person's computer would fetch code and run it with
their access, triggered from a web page.

## Decision
### The server's version
- `deploy/deploy.sh` writes `~/hagency/pm/REVISION` next to the files it unpacks: the commit id on
  the first line, the commit time (Unix seconds) on the second.
  - `deploy/release.py` clears it with every other untracked file, so each deploy writes it afresh.
  - `@pm`'s folder has no `.git`.
- `common/version.py` reads `REVISION`. Without one (a git checkout), it asks git.
- The panel's footer shows `Hagency Rooms abc1234 · 2026-11-25`.

### An agent's version
- Each time an agent's adapter signs in, it reports its code to @pm: the commit of `~/.hagency/app`,
  that commit's time, and whether it runs the owner's own version.
  - The report is a to-device message, `xyz.hagencylabs.agent.code` (`common/code_report.py`).
  - Synapse sets the sender, as for the AI report (ADR 0008).
  - It is a separate message from the AI report: a server that predates it ignores it, and the AI
    report's strict field check stays as it is.
- @pm keeps the report on that agent's entry in agents.json (`code`). Only the sender's own entry
  changes.
- My agents shows each connected agent as one of:
  - **Up to date**: its commit is the server's, or newer. Both are on master, so comparing commit times works.
  - **Update available**, with an **Update** button.
  - **Your own version**: no update is offered ("Use it myself", `common/customize.py`).
  - **No version reported** (online, but its code predates this ADR): **Update my agent**, which is PR #58's
    Connect page. That manual update is needed once. After it, updates take one click.

### One-click update
1. The owner clicks **Update**.
   - `console.act("selfupdate")` checks that the agent is theirs, on their own computer, online,
     reported a version, and isn't their own version.
   - It allows one click per agent per minute.
2. @pm sends the agent a to-device message, `xyz.hagencylabs.agent.update`, with one field: the
   server's commit.
3. The agent (`adapters/agents/agent_update.py`) acts only on that message type, and only when the
   sender is @pm. Then `adapters/agents/self_update.py` checks, in order:
   - **Not the owner's own version:** the running code is the official install, not a `live-*` copy,
     and the owner has no `custom-use.json` choice.
   - **The install is the official one:** the folder is `~/.hagency/app`, with its own `.git`, origin
     `https://github.com/csheargm/hagency-rooms`, on branch `master`.
   - **Nothing edited:** `git status` shows no changed tracked files.
   - **Only official code, only forward:** it fetches master from that URL (the URL itself, never a
     remote name). The requested commit must be on that master and a descendant of what runs now.
     Otherwise it does nothing, and "already newer" counts as success.
   - **Dependencies first:** it installs the new commit's `requirements.txt` with this agent's own
     Python before moving the code. If pip fails, nothing has changed.
   - Then `git merge --ff-only` to that commit, with git hooks off.
4. The agent says how it went to @pm in one to-device message, `xyz.hagencylabs.agent.update.result`.
   - The result is one word from a fixed list (`code_report.RESULTS`); the panel maps each to plain words.
   - Nothing else from that computer is ever shown.
5. After a successful update, the agent finishes the turns it has started, hands off its sync position
   (lifecycle.py, as on SIGTERM), and exits with code 75.
   - launchd (KeepAlive on a failed exit), systemd (`Restart=on-failure`) and the Windows keeper
     start it again on the new code.
   - Its new report then shows **Up to date**.

### Why this shape
- **The server can't name the code, only a point on the official master.** A compromised @pm, or anyone
  who could forge its message, can at most move an agent forward along `csheargm/hagency-rooms` master.
  That code would reach the agent anyway at its next connect line.
- **The update target is the server's own commit, not master's tip.** Agents then run the code the server
  was deployed and tested with, and "Up to date" means "matches the server".
- **Owner-only.** The click goes through the panel's session (`console.act`, owner by full ID). The
  agent obeys only @pm's account.

## Consequences
- **Not covered:**
  - **Hermes agents.** They run through `hermes_local.py`, whose gateways would not restart. The panel
    offers them the Connect page (PR #58's path).
  - **Installs at a folder the person chose** (`HAGENCY_APP=…`), which the panel reports as not
    updatable here.
  - **An agent run by `bin/supervise.py`.** It picks up the code change itself.
- **No automatic update when idle yet.** That is a follow-up; it would reuse the same checks.
- **A 75 exit on macOS** waits for launchd's throttle (about 10 s); systemd waits `RestartSec` (30 s).
  The panel says "about a minute".
- **pip runs the new requirements' installers**, as the connect line already does.
