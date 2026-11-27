# 0024: Automatic agent updates

## Status
Accepted, 2026-11-27. Builds on ADR 0020 (versions and one-click updates).

## Context
- After ADR 0020, an agent on an owner's computer updated only when its owner clicked **Update** in My
  agents. After each deploy, every owner had to click it for every agent.
- The owner decided that agents update themselves, on by default. The safety stays where ADR 0020 put
  it: `self_update.py` only moves the official install forward along the official master.
- Two live bugs on 2026-11-27, same area:
  - An owner's two agents share one install (`~/.hagency/app`). Asked together, one updated it while the
    other found the update lock taken, said "failed", and kept running old code.
  - @pm sent the new task kind `solo_ruleset` to an agent on older code. The room read
    "⚠️ solo_ruleset failed: unknown task kind 'solo_ruleset'".

## Decision
### The switch
- One **Auto-update** switch per owner, in My agents (`console_auto_update.toggle`, action `autoupdate`).
- It is stored as `auto_update` on the owner's line in agents.json, where their other settings live.
  Absent means on; only `false` turns it off.

### When @pm asks
- **After a deploy:** every `pm_presence.CHECK_S` (the first check right after @pm starts on new code),
  `auto_update.sweep` looks at each agent of each owner whose switch is on.
- **When an agent reports older code:** its code report at sign-in (`pm_kind_reports`), which also proves
  it is online.
- The target is always the server's own commit (`console_update.SERVER`), never master's tip.
- Only agents whose code report says `"waits": "yes"` are asked (`code_report.WAITS`), i.e. code that
  waits for its work, below. Older code still shows the Update button once, with "after that it updates
  itself automatically".
- Hermes agents, an owner's own version and offline agents are never asked.

### The agent never cuts off work
- The request carries `"auto": true`. The agent (`agent_update.wait_idle`) then waits until it has no task
  running: no turn, build, review or merge (`self.tasks`), and no GitHub sign-in.
- It waits at most `code_report.IDLE_WAIT_S` (30 minutes), then answers the new result word `busy` and
  keeps working. @pm asks again after `BUSY_AGAIN_S`, or at its next code report.
- After that, everything is ADR 0020's update: the same checks, then restart once its turns are done.

### One install, several agents
- `self_update._Lock` now waits for another agent's update, up to `LOCK_WAIT_S` (pip's and git's timeouts
  added up). Before, it answered "failed" at once.
- The second agent then finds the install already at the target (or newer), and `check_target` answers
  "current". `agent_update.stale()` sees the install differs from the code it loaded, reports "updated"
  and restarts onto it.
- @pm asks one agent per owner at a time, at least `STAGGER_S` (90 s) apart. It waits until that agent's
  ask is answered or has timed out.

### Back-off
- A failure (`failed`, `current` while it still reports older code, or no answer within its whole wait)
  waits `BASE_BACKOFF_S` (10 minutes), doubling up to `MAX_BACKOFF_S` (6 hours).
- After `MAX_FAILURES` (3), @pm stops asking for this server version. It also stops at once on a refusal
  that retrying can't change (`own`, `not_official`, `edited`, `not_newer`).
- **Retry update** (the owner's click) or a newer server starts over.
- `busy` is not a failure.
- The state is kept in @pm's memory only, so a restart (a deploy) tries once more.

### The panel
- **Auto-update on:**
  - No Update button.
  - Up to date: "Version … · updates automatically".
  - Behind: **Updating…** while asked, or "Updates automatically once it has nothing running" / "…when
    it's back online".
  - After a failure: **Retry update**, with the reason in ADR 0020's words.
- **Auto-update off:** ADR 0020's Update button, unchanged.

### Telling the owner
- One line in the owner's private chat with @pm per update batch, e.g. "Your agents **main** and **codex**
  updated automatically to v1.1.0 · abc1234: <what changed>".
- It is sent once none of their agents is still to update, or `BATCH_S` (15 minutes) after the first
  finished. Agents that finish later for the same version are not announced (My agents shows them).
- "What changed" is the commit's subject, or for GitHub's merge commit the pull request's title
  (`version.summary`). Markup characters are removed, and it is cut at 100 characters.
- `deploy.sh` now writes the commit message after REVISION's two lines. `version.of` still reads only the
  first two, so an older REVISION reads the same and just has no summary.

### Tasks newer than an agent's code
- `code_report.TASK_SINCE` maps a task kind to the master commit time from which agents run it
  (`solo_ruleset`: 0afcfbb). `code_report.runs_task` decides.
- `pm_solo_rules.ruleset_agent` picks an owner's agent that runs the task.
- If the only owner agents here are older, @pm says "<agent> needs an update first", asks it to update
  (`console_auto_update.update_for_task`), and says how to finish.
- An "unknown task kind" answer gets the same words and the same update, never the raw error.

## Why this shape
- **On by default is safe enough:** an automatic update can do no more than a click could. That is a move
  forward along the official master, to the commit the server itself runs.
- **Waiting on the agent's side,** because only the agent knows what it is running. @pm can't see a build
  in flight on someone's computer.
- **Capability, not hope:** only code that says it waits gets automatic asks. An older agent would update
  at once, and could change the code under a running turn.
- **Stagger plus a waiting lock:** the stagger keeps one owner's agents from racing. The lock also covers
  a click during an automatic update, and agents of different owners on one computer.

## Consequences
- An agent busy for hours is asked about every `BUSY_AGAIN_S`. Each ask is one to-device message, and
  its wait is cheap.
- Turning Auto-update off doesn't recall an ask already sent. That agent may still update once.
- A task kind that later grows newer fields needs its `TASK_SINCE` time moved forward.
