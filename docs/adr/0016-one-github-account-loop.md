# 0016: One GitHub account must not block the loop

## Status
Accepted, 2026-11-23 (owner's decision after the full-loop live test).

## Context
The loop merged only when GitHub itself reported `reviewDecision: APPROVED` (`github.merge_ready`), so every
repo needed a required-review rule, and the reviewing agent needed a GitHub account other than the PR's author.
In the live test both agents (two people) were signed in to one GitHub account: GitHub refuses (HTTP 422) an
approval of a PR its own account opened, so every review failed, and nothing could merge. The owner: needing a
second account is an operational choice; it must not block the workflow.

## Decision
- A review GitHub refuses because the reviewing agent's account opened the PR (`own_account.opened_it`) is
  posted as a COMMENT that carries the verdict. The verdict still goes to @pm.
- `merge_ready` accepts `APPROVED`, or `""` (the repo requires no review). Then the loop merges on its own
  reviewer's verdict: always another person's agent (`routing.checkers_for`), at the reviewed commit.
- A repo whose rules require an approval (`REVIEW_REQUIRED`, `CHANGES_REQUESTED`) still blocks the merge; the
  issue pauses with a link to approve and merge it on GitHub.

## Consequences
- In a repo without a review rule the only check is the loop's own second agent. Any agent with write access
  could already merge there by itself, so the old rule added little there; repos that want GitHub's own gate
  keep it with a branch rule.
- Supersedes the "no required-review protection → never merge-ready" rule (tests/test_loop_rules.py).
