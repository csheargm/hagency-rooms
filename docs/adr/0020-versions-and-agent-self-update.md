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
- The panel's footer shows `Hagency Rooms v1.0.0 · abc1234 · 2026-11-25`.
  - The release number is the repo's `VERSION` file, kept nowhere else, bumped by the pull request
    that makes a release (CONTRIBUTING.md) and tagged `v<number>` on GitHub (added after the owner
    asked for a number as well as the commit and date, as SuperElf shows).
  - An agent's report carries its number too (`"number"`, optional: code from before `VERSION` sends
    none and is shown without one). A server is always deployed before agents update, so the stricter
    field check of an older @pm never sees it.
  - "Up to date" still compares commits, never numbers: two commits can share a number between releases.

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
2. @pm sends the agent a to-device message, `xyz.hagencylabs.agent.update`: the server's commit and
   a click id. The agent echoes the id, so a late answer never counts for a newer click.
3. The agent (`adapters/agents/agent_update.py`) acts only on that message type, and only when the
   sender is @pm. Then `adapters/agents/self_update.py` checks, in order:
   - **Not the owner's own version:** the running code is the official install, not a `live-*` copy,
     and the owner has no `custom-use.json` choice.
   - **The install is the official one:** the folder is `~/.hagency/app`, with its own `.git`, origin
     `https://github.com/csheargm/hagency-rooms`, on branch `master`.
   - **Nothing edited:** `git status` shows no changed tracked files, and no file is marked
     assume-unchanged or skip-worktree (status hides those). It checks again after pip, just before the code moves.
   - **One update at a time:** a lock in `.git`, because a person's agents on one computer share the install.
   - **Only official code, only forward:** it fetches master from that URL (the URL itself, never a
     remote name). The requested commit must be on that master and a descendant of what runs now.
     Otherwise it does nothing, and "already newer" counts as success.
   - **Dependencies first:** it installs the new commit's `requirements.txt` with this agent's own
     Python before moving the code. If pip fails, nothing has changed.
   - Then `git merge --ff-only` to that commit. Git runs with hooks and fsmonitor off, with no
     system or global config (a `url.insteadOf` there could redirect the fetch), and without replace objects.
4. The agent says how it went to @pm in one to-device message, `xyz.hagencylabs.agent.update.result`.
   - The result is one word from a fixed list (`code_report.RESULTS`); the panel maps each to plain words.
   - Nothing else from that computer is ever shown.
5. After a successful update, the agent finishes the turns it has started, hands off its sync position
   (lifecycle.py, as on SIGTERM), and starts again on the new code.
   - On macOS and Linux it re-executes itself in place (`os.execv` with `sys.orig_argv`), so a service,
     `bin/supervise.py` and a terminal run (`--foreground`) all continue.
   - On Windows it exits with code 75, and the Windows keeper starts it again.
   - An agent whose install another agent already updated answers "current" but still runs old code,
     so it restarts too.
   - Its new report then shows **Up to date**. With no answer within 5 minutes (for example, a click
     during its start), the panel says so and offers Update again.

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
  - **A Windows agent run in a window (`--foreground`).** It ends after updating; its next start runs the new code.
- **Automatic updates when idle:** ADR 0024 (on by default, the same checks).
- **The restart waits for running turns** (up to 20 minutes, lifecycle.py). The panel says "once it finishes
  what it's doing".
- **An agent can claim any commit.** It only changes what its own owner sees.
- **A shallow clone** can't prove that an older target is behind it, and answers "not_newer". @pm only asks
  agents that are behind.
- **pip runs the new requirements' installers**, as the connect line already does. If pip fails partway,
  the code stays old but some packages may be newer.
- **pip updates the install's `.venv`, which an owner's own version (`live-*`) shares.** Their code is
  never replaced, but its packages may be newer. A fast-forward can also replace a git-ignored local file
  if master starts tracking that path (unlikely: state lives in data/, custom/ and rooms/).
- **The clone's own `.git/config` is trusted.** It is the person's own file, and anyone who can write it
  can already run code as them.
