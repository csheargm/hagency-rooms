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
- After that, everything is ADR 0020's update, with the same checks.
- Before it restarts, it waits again for no task running, for as long as that takes. Work that came in
  while git and pip ran then finishes first: a GitHub sign-in is never killed by the restart (review).
- If that outlasts @pm's wait, @pm counts one failure. The agent's next report shows it up to date and
  clears the failure.

### One install, several agents
- `self_update._Lock` now waits for another agent's update, up to `LOCK_WAIT_S` (pip's and git's timeouts
  added up). Before, it answered "failed" at once.
- The second agent then finds the install already at the target (or newer), and `check_target` answers
  "current". `agent_update.stale()` sees the install differs from the code it loaded, reports "updated"
  and restarts onto it.
- The clean-tree check runs under the lock. Another agent's half-done update is then never taken for
  an edit (review).
- @pm asks one agent per owner at a time, at least `STAGGER_S` (90 s) apart. It waits until that agent's
  ask is answered or has timed out.

### How long @pm waits
- A click or ask stays open (`console_update.is_open`) for the agent's whole update:
  - `code_report.UPDATE_MAX_S`: another agent's whole update (the lock wait), then its own: pip and up
    to `self_update.GIT_CALLS` git calls at their timeouts. Tests check the sum and count the calls.
  - Plus `IDLE_WAIT_S` for an automatic ask.
  - After "updated", the drain, counted from that answer.
- An open ask is never replaced, because its answer would be lost. A closed one (a refusal, an old click)
  never blocks the owner's automatic asks (review).

### Back-off
- A failure (`failed`, `current` while it still reports older code, no answer within its whole wait,
  or @pm couldn't send the ask) waits `BASE_BACKOFF_S` (10 minutes), doubling up to `MAX_BACKOFF_S` (6 hours).
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
  - After a failure: **Retry update**, with the reason in ADR 0020's words. For `edited` and
    `not_official` it shows **Update my agent** (the Connect page) instead, since a retry can't help.
- **Auto-update off:** ADR 0020's Update button, unchanged.

### Telling the owner
- One line in the owner's private chat with @pm per update batch, e.g. "Your agents **main** and **codex**
  updated automatically to v1.1.0 · abc1234: <what changed>".
- It is sent once none of their agents is still to update, or `BATCH_S` (15 minutes) after the first
  finished. Agents that finish later for the same version are not announced (My agents shows them).
- "What changed" is the commit's subject, or for GitHub's merge commit the pull request's title
  (`version.summary`). It is cut at 100 characters.
- In the line, link and code characters are removed and emphasis marks are escaped
  (`auto_update.plain`), in agent names too.
- `deploy.sh` now writes the commit message after REVISION's two lines. `version.of` still reads only the
  first two, so an older REVISION reads the same and just has no summary.

### Tasks newer than an agent's code
- `code_report.TASK_SINCE` maps a task kind to the master commit time from which agents run it
  (`solo_ruleset`: 0afcfbb). `code_report.runs_task` decides.
- `pm_solo_rules.ruleset_agent` picks an owner's agent that runs the task.
- If the only owner agents here are older, @pm says "<agent> needs an update first" and says how to finish.
  When Solo review is being switched off, it also says how to take the bypass out by hand.
- It asks the agent to update (`console_auto_update.update_for_task`) only through the automatic
  scheduler: its owner's switch, code that waits, back-off, cap and stagger all apply. Otherwise it says
  to update it in My agents. A room action never overrides the owner's switch (review).
- Its words follow the Solo review switch as it is now, not as the late ask had it (re-check).
- An "unknown task kind" answer (`code_report.UNKNOWN_KIND`, one string for tasks.py, pm_cards and
  pm_solo_rules) gets the same words, never the raw error.

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
- **Not covered:**
  - **A commit time doesn't prove a task is there.** An owner's own version can be based on older code
    with a later time. Agents advertising their task kinds would fix that; until then, "unknown task
    kind" gets the friendly words.
  - An agent on code before this change still posts its own "⚠️ … failed" line in the room. Only code
    after it can stop that.
  - One owner's agents on one computer share the install. One agent can update it while another still
    has work running. That work runs on the code it loaded, as with ADR 0020's click, and that agent
    restarts only when it is idle.
