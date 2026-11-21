# Demo scenario: Anna and Betty build "Hello World" with their own agents

Goal: two people, each with their own agent, agree on what to build by talking in a Matrix room.
The agents build it through GitHub. The people test-drive it and iterate.

Status: ✅ works today · 🟡 works with a manual step · ❌ missing (smallest piece needed)

| # | Step | Who | Status |
|---|------|-----|--------|
| 1 | Anna signs in to Element with her Matrix account. | Anna | ✅ |
| 2 | Anna brings her Hermes in: a room-only profile that obeys only Anna. | Anna | 🟡 works (`test_hermes_e2e.py` sets it up); needs a one-command `onboard anna` |
| 3 | Anna creates the room "Hello World" and is its owner. She adds setup-bot and pm. | Anna | 🟡 room ✅; bots are added by `setup_accounts.py`, should be `!setup` or an invite |
| 4 | Betty signs in, gets invited, and brings her own Hermes (using a Codex model). | Betty | 🟡 same onboarding as step 2; Betty currently uses the Codex agent adapter |
| 5 | Anna and Betty discuss: purpose, users, what "done" means. Each can ask their own agent (`@anna-hermes-agent what would a minimal version need?`). | both | ✅ each agent answers only its owner; DMs work after the Hermes DM fix |
| 6 | Anna: "spec is ready". One agent writes a short spec from the discussion and posts it. The people edit it, then approve it. | Anna + agent | ❌ `!spec draft` / `!spec approve` |
| 7 | The approved spec becomes GitHub issues (`agent-ok`) in the project repo. | @pm | ❌ part of `!spec approve` |
| 8 | Project repo with the merge ruleset. | Anna | 🟡 `github_setup.py ruleset` exists; a fresh repo per project is a manual `gh repo create` |
| 9 | `!level assist`: @pm hands issues to the two agents. One builds, the other person's agent reviews, and the pinned **Project status** shows it live. | @pm + agents | ✅ (Hermes via `hagency_mcp.py`, Codex via agent adapter) |
| 10 | Human review: in Assist, each PR stops at "ready — a person merges". Anna or Betty reads it and merges on GitHub. | people | ✅ |
| 11 | Test drive: they run Hello World from the repo, locally or on GitHub Pages. | people | 🟡 manual (`git pull` and open it) |
| 12 | They talk about what to change. Anna: "update the spec". The new issues go back into the loop. | all | ❌ same `!spec` as step 6; the loop then picks the new issues up ✅ |

## Smallest set to make the demo run end to end
1. **Onboarding** (`onboard.py <person>`): the Matrix account, the Hermes room profile (owner-only, quiet, hagency MCP) and the check that the GitHub identity is signed in. This moves code that already exists in the test into a command.
2. **Spec to issues** (`!spec draft`, `!spec approve`):
   - `!spec draft`: the owner's agent drafts the spec from the room conversation.
   - The spec lives in the room, pinned like the status board, and people edit it in the room.
   - `!spec approve` (owner or maintainer): the spec's tasks become `agent-ok` issues. On a later
     run, only the changes become issues.
3. **Project repo**: one guided step, "create repo and ruleset for this room", using the existing
   `github_setup.py`.

Everything else in the table already works.

## Lessons from the Hermes test runs
- **The room's tasks beat the agent's memory.** Hermes keeps one conversation per room. Asked to
  "build #210" again, it answered from memory instead of calling `next_task`. Its instructions now
  say the task tools, not the conversation, decide.
- **Self-learned skills can learn the wrong lesson.** After a run that failed because of our bug,
  Hermes wrote itself a skill around that bug. The next run followed it and gave up on a valid
  checkout. Owners need an easy way to review and prune what their agent learned from room work:
  `hermes skills`, the curator, locking a skill.
- **A shared room sees only final replies.** Hermes's tool-progress lines and in-between drafts are
  off in room profiles: they leaked local file paths.
- **DMs with your own agent** needed a small Hermes fix (`docs/hermes-matrix-dm.patch`). Hermes
  recognises a DM only through its own `m.direct` data and didn't record DM invites there.
- **Only the owner (or @pm) can pull an agent into a room.** Hermes joined every invite, so anyone
  could invite Anna's agent into their room and have it read along. With `MATRIX_ALLOWED_USERS`
  set it now ignores other inviters, as our agent adapter does (`docs/hermes-matrix-invite-allowlist.patch`;
  no list keeps upstream's join-all). Answers no longer quote the question either
  (`docs/hermes-matrix-reply-mode.patch`).
