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
- **A per-room switch, off by default**: `xyz.hagencylabs.solo_review` room state, written only by @pm and
  trusted only when @pm wrote it (`common/solo_review.py`), like the room's repo and mode.
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
  `gh pr merge --admin` (`solo_github.admin_merge`): only at the reviewed commit, only when every check passed
  (an unfinished check waits, a red one pauses), only when GitHub says this account may bypass every review
  rule there, and only while the agent adapter itself reads the switch as on. Assist tells the person to merge as
  the repo's admin.
- **The ruleset is changed from Hagency, never by hand**: switching on asks the room owner's agent (it runs
  `gh` as the owner, the repo's admin) to add `{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode":
  "pull_request"}` to each repository ruleset that requires a review on the default branch (GET/PUT
  `repos/{repo}/rulesets/{id}`, sending only `bypass_actors`: existing entries and every rule stay). An equal entry
  already there counts as done and is not recorded. The rulesets Hagency changed are recorded in the room's
  switch; switching off takes out only those entries. Anything that fails (not admin, an organization ruleset,
  no owner agent here) is said plainly in the room, with exactly what to change on GitHub.
- **Paused items go on**: an item paused for want of a checker is marked `no_checker`; its pause message offers
  solo review (chat and panel), and `@pm retry #N` returns it to the review/verify step with its PR kept, instead
  of requeueing a fresh build that would race the open PR.

## Consequences
- With the switch on, one person's agents can build, check and merge their own work in that room. That is the
  owner's explicit choice for testing; the default and every other room keep the two-person rule.
- While the bypass is in a ruleset, any repo admin may merge a pull request without an approval (never push
  directly). `docs/github-ruleset.json` itself keeps no bypass.
- Hermes agents (MCP tools) can't run the ruleset task yet; @pm says the agent couldn't, with the manual steps.
- Supersedes ADR 0016's "always another person's agent" for rooms that switched solo review on.
