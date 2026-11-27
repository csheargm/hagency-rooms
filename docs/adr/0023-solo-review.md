# 0023: Solo review, for a room owner testing alone with their own agents

## Status
Accepted, 2026-11-27 (owner, testing alone with several of their own agents: "no need for a second person
at this testing stage". GUI first: everything is doable from Hagency Rooms, without a terminal).

## Context
- The checker of a build (review, verify) had to be an agent of a DIFFERENT person (`routing.checkers_for`,
  ADR 0005). Alone in a room, every finished PR paused: "there's no second person's agent here to review it".
- In the live test all of the owner's agents used ONE GitHub account, which also opened the PRs. GitHub never
  lets an account approve its own PR, so their review could only be a comment (ADR 0016).
- The repo's ruleset (`docs/github-ruleset.json`) requires 1 approving review and the last pusher's approval,
  with no bypass actors. So even with a second agent's verdict, nothing could merge.

## Decision
- **A per-room switch, on by default** (amended 2026-11-27, see below; first off by default):
  `xyz.hagencylabs.solo_review` room state, written only by @pm and trusted only when @pm wrote it
  (`common/solo_review.py`), like the room's repo and mode.
- **Turning it on is a relaxation**: only the room owner or a maintainer; in chat always asked with a 👍
  (`@pm allow solo review`, or the owner AI's `solo_review_on` action), never from words alone; in the panel's
  Mode card the moderator's click is the confirmation. **Turning it off** is anyone's, at once (like stop):
  `@pm solo review off`, the owner AI's `solo_review_off`, or the panel.
- **Who checks**: another person's agent is still preferred. Only when none is here and the switch is on, another
  agent of the builder's owner checks (`routing.own_checkers`), never the builder itself. The item records
  `solo`, and a solo checker's result counts only while the switch is still on; switched off mid-check, the
  item pauses and says so.
- **On GitHub**: a solo checker posts its verdict as a COMMENT review, never APPROVE (`tasks.submit_review`).
  Where the repo's rules then report `REVIEW_REQUIRED`, Autonomous asks the builder to merge with
  `gh pr merge --admin` (`solo_github.admin_merge`). `--admin` goes past the whole ruleset, not only the review
  (GPT review), so it runs only: at the reviewed commit, into the default branch, with a merge state of BLOCKED or
  CLEAN (never BEHIND, DIRTY, UNSTABLE); when every reported check passed (unfinished ones are waited for up to
  10 minutes, a red one pauses) and every check a ruleset requires is there and passed; when every other rule type
  on the branch is one a squash merge meets anyway (deletion, non-fast-forward, linear history, creation;
  anything else, e.g. a merge queue, leaves the merge to a person); when GitHub says this account may bypass
  every review ruleset there; and only while the agent adapter itself reads the switch as on. Assist tells the
  person to merge as the repo's admin.
- **The ruleset is changed from Hagency, never by hand**: after its own 👍 (amendment below), @pm asks the room owner's agent (it runs
  `gh` as the owner, the repo's admin) to add `{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode":
  "pull_request"}` to each repository ruleset that requires a review on the default branch (GET/PUT
  `repos/{repo}/rulesets/{id}`, sending only `bypass_actors`: existing entries and every rule stay). An equal entry
  already there counts as done and is not recorded. The rulesets Hagency changed are recorded in the room's
  switch (also after a partial failure, and for an answer that comes late: asks are kept on disk by task id);
  switching off takes out only those entries, by their ids. If the switch moved while the agent worked, @pm
  follows it with another ask. Anything that fails (not admin, an organization ruleset,
  no owner agent here) is said plainly in the room, with exactly what to change on GitHub.
- **Paused items go on**: an item paused for want of a checker is marked `no_checker`; its pause message offers
  solo review (chat and panel), and `@pm retry #N` returns it to the review/verify step with its PR kept, instead
  of requeueing a fresh build that would race the open PR.

## Consequences
- With the switch on, one person's agents can build, check and merge their own work in that room. That is the
  owner's explicit choice for testing; the default and every other room keep the two-person rule.
- While the bypass is in a ruleset, any repo admin may merge a pull request without an approval (never push
  directly). `docs/github-ruleset.json` itself keeps no bypass.
- GitHub's ruleset API has no compare-and-set: the PUT sends the bypass list read a moment before, so a bypass
  entry someone edits on GitHub in that same moment could be undone. A bypass is only added to a ruleset whose
  rules are the review plus ones a squash merge meets anyway, never required checks (a bypass covers the whole
  ruleset, and a person could then merge past red CI by hand); otherwise the
  owner is told to give the review rule a ruleset of its own. Classic branch protection's required checks are
  read too. The agent asks the room again (Autonomous, solo on) right before `--admin`, after waiting for checks.
- A ruleset answer that reaches @pm while it is restarting is lost like any agent result then; the bypass is
  then still on GitHub but not recorded, and a person takes it out there.
- **Consent is per repo**: the switch names the repo it was switched on for, and counts only while that is the
  room's repo (`solo_review.live`; also re-read by the agent adapters). It is switched on only once the room has
  a repo. When the repo changes or is removed, @pm switches it off and names the admin bypass Hagency left on the
  old repo, with how to take it out by hand (an agent only works on the room's own repo). A change made while @pm
  was down is not announced, but the switch is still off for the new repo, and "solo review off" still names it.
- An unreadable room state is never the base of a write (the fail-closed "off, nothing recorded" read would
  erase the record). A recorded ruleset that no longer exists is dropped from the record, and never blocks the
  removal of the others.
- An item checked in solo mode whose switch is off by merge time pauses for another person's check (`retry`
  goes back to the review). An admin merge GitHub refuses pauses with how to let it merge, and `retry` tries the
  merge again. Assist's "merge past the rules" advice is given only when the rule is known to let admins past.
- Classic branch protection is read too (`branches/{b}/protection`): signed commits, resolved conversations,
  push limits, a locked branch or blocked creations make the merge a person's.
- A maintainer may switch it on, like every other owner-level setting here (ADR 0002); the bypass is added by
  the room owner's agent as the owner. Reviewed and kept on purpose.
- Hermes agents (MCP tools) can't run the ruleset task yet; @pm says the agent couldn't, with the manual steps.
- Supersedes ADR 0016's "always another person's agent" for rooms that switched solo review on.

## Amendment 2026-11-27: on by default
Owner decision: solo review is **on by default** for rooms. Kept: another person's agent is always preferred;
anyone can switch it off at once; the GitHub consent stays tied to the room's repo.
- **State**: no SOLO_EVENT at all ("never set", every existing room too) reads as on, with `default: true`.
  An explicit off (anyone's) stays off, also across a repo change, until a moderator switches it on again with
  a 👍 or a click. An event by anyone but @pm, an unparsable one, or an unreadable room is off (fail closed).
  The agents' own single read (`mcp_build.require_autonomous`) counts only a 404 `M_NOT_FOUND` as never set
  (`solo_review.event_state`).
- **Per-repo consent is now the GitHub part only**: `repo`, `ok`, `bypass_added` and the new `asked` name the
  repo they are about (`solo_review.bypass_ok`). On a repo change @pm drops them and names a bypass Hagency left
  on the old repo (`pm_solo_rules.follow_repo`); on/off itself is kept. An admin merge still goes only into the
  room's own repo (`still_allowed`, `mcp_build`).
- **Safeguard: never a ruleset change just because the default is on.** @pm asks the room's owner (any
  moderator may answer) ONCE per repo with a 👍, in the owner's words: "To merge with only your own agents,
  Hagency needs to let repo admins merge past GitHub's review rule on <repo>. 👍 to allow." Only after that 👍
  does it send the `solo_ruleset` task (`pm_solo_rules.offer_bypass` / `bypass_confirmed`). Asked: the first
  time a solo merge needs the bypass (an Autonomous admin merge refused with `NO_BYPASS`, or Assist's "a person
  merges" without a known bypass), when a repo is bound (at the next poll, after the bind's answer), and when a
  moderator switches solo review on or says "allow solo review" while it is on. Not asked: when the bypass is
  known (`ok` for this repo), when the repo's default branch has no review rule, when solo review is off, or a
  second time for the same repo (only a person's own "allow solo review" asks again). A 👍 after a rebind or
  after switching off changes nothing. "Switched on again while the bypass was being taken out" now asks again
  instead of putting it back on its own.
- **Known limit**: @pm's GitHub App is read-only and can't see a ruleset's bypass list, so a bypass a person
  added by hand is "known" only once a ruleset task reported it (`already`); but an Autonomous admin merge that
  GitHub lets through never triggers the ask. Assist may ask once although the
  bypass is already there; the owner's agent then answers "already lets repo admins merge" and changes nothing.
- GPT review of this change: every ruleset ask names the repo it was consented to / recorded for and is sent
  only while that is still the room's repo (`ask_ruleset(expect=...)`); records are written only for the
  room's own repo, checked under the switch's lock (`write_solo(for_repo=...)`); the 👍 counts only from a
  joined moderator read now; a malformed single-event read is off; a record left for another repo (e.g. across
  a restart) is dropped by the next offer instead of blocking it; the switch and binding are read again after
  the owner's agent is chosen; a failed repo read in the poll never drops the current repo's record. The
  question takes a 👍, a reply to that very message, or the panel's Approve (whose card shows the question),
  never a stray unthreaded "yes". Rejected: the 👍 emoji alone — a threaded reply or the Approve click is
  equally that person's own act on that very question.
- **Agents on older code** (independent review): code from before this default reads only an explicit @pm
  event, on and for the room's repo, so "never set" looked off to it and it refused an admin merge @pm had
  sent ("solo review is off in this room"). Before a step the agent re-checks (an admin merge, the ruleset
  task) and when a moderator switches it on, @pm writes the switch out as on for the room's repo
  (`pm_solo_code.record_on`; an off is never written as on). An agent whose reported code predates solo review
  itself (`code_report.TASK_SINCE["solo_step"]`) gets no solo check or admin merge: the room is told it needs
  an update first, it is asked to update through the auto-update scheduler, and `@pm retry #N` goes on.
  Accepted (GPT whole-batch review): the gate compares the reported commit's *time*, so a fork commit made
  after `TASK_SINCE` on top of older code passes it. Gating on ancestry would not close this: the report is
  the agent's own (a fork can report any commit and time), so it is a compatibility guard for honest older
  official code, never a boundary against the owner's own code. The worst case is the owner's own fork, with
  two of the owner's own GitHub accounts, posting a real approval instead of a comment: an approval by the
  owner's own account, on a PR in a room where the owner consented to solo review.
- **Later agent-side safety checks** (GPT whole-batch review): a solo merge (admin, or a regular one marked
  `solo`) goes only to code that re-reads the switch right before the merge and refuses code-owner and
  designated-reviewer rules (`code_report.TASK_SINCE["solo_merge"]`), and adding the bypass only to code that
  refuses those rules (`"solo_bypass_add"`); older code gets "needs an update first" and `@pm retry #N`.
  Taking the bypass out again works on any code that knows the ruleset task. Another person's agent that is in
  the room but offline is waited for, never replaced by the builder's sibling, and a sibling's verdict doesn't
  count while one is here.
- Readiness ("Check readiness"), the panel's Mode card and the pinned status read the same state, so a room
  that never set it shows solo review on; the readiness check counts a bypass only for the repo it checks.
