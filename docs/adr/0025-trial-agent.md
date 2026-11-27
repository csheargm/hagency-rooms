# 0025: A shared, talk-only trial agent for visitors without their own

## Status
Accepted, 2026-11-27 (owner: visitors should be able to try Hagency Rooms before bringing their own agent).
Step 1 only: talk. (0024 is left for the agent auto-update work.)

## Context
- A newcomer could do nothing useful until their own agent was connected (🔌 Bring your own, ADR 0002), which
  needs Codex, Claude Code, Hermes or a model on their own computer. Many leave before that.
- The old "server agent" (🏠) was never built: typing it only said "not here yet".
- The owner has an Ollama Cloud plan (a monthly credit allowance, pay-as-you-go beyond it, at most 3
  requests at once), and its key is shared with another project.

## Decision
- **One trial account**, `@hagency.trial-agent`, display name "Trial agent (shared, limited)", run inside @pm's
  process with its own Matrix sign-in (`server/trial_agent.py`), only when `OLLAMA_API_KEY` is set. Without it,
  nothing changes: no 🧪, the welcome text and buttons are exactly as before, the words fall through.
- **Offered GUI-first** to people with no connected agent of their own: a 🧪 button under @pm's welcome next to
  🔌, "try the trial agent" in their chat with @pm, and a Trial agent card with **Try it** in My Hagency
  (`server/pm_trial.py`). It opens its own private chat with them (an invite they accept, as for people from
  other servers).
- **Talk only**: one chat call to `https://ollama.com/api/chat` (`server/trial_model.py`), no tools offered, nothing
  it says is run. It can read a pull request's diff, read-only through the server's GitHub App, and only of the
  repo of the room it was asked in (`server/trial_reads.py`).
- **Which model**: the operator's allowlist `TRIAL_MODELS` (first = default; checked on Ollama Cloud's
  `/api/tags`, 2026-11-27: glm-5.3, kimi-k3.1-pro, kimi-k2.7-code, deepseek-v4.1-flash, gpt-oss:120b). A visitor picks
  one in My Hagency (a dropdown on the Trial agent card) or with "use <model>" / "which models" in their chat with
  @pm (`server/trial_models.py`); only listed names are accepted, the choice is kept with them and re-checked at
  each use. Every reply ends "— Trial agent · <model>". Caps are shared across models; an entry may carry a weight
  (`kimi-k3.1-pro*2`) so a bigger model's tokens count more against the monthly token cap (the balance guard is
  in money, so it already counts them more). Not an owner-AI action (ADR 0022): that needs the room owner's own
  agent, which trial visitors don't have.
- **Who it answers**: only visitors who chose it, still without a connected agent, only in rooms such a visitor
  brought it into and is joined, and in a project room only when mentioned. It declines invites from anyone
  else, leaves a room when the last visitor who brought it leaves, and ignores every agent, bot and @pm.
- **Never in the loop**: it is in nobody's `agents.json` line, so `ownership.agents_in` never lists it: @pm never
  picks it to build, review or merge and never takes a result from it. It has no GitHub identity, so what it says
  about a PR can never be a review that counts for merge gating. Its localpart has a "." that no person's agent
  name can have (`identity.py`), so it can't collide with one; it ends in `-agent`, so other agents and @pm already
  ignore it, and it posts notices, which agents don't read.
- **Caps** (`server/trial_limits.py`, env-configurable): 50 messages per visitor and 1000 for the server per UTC
  day, a reply-length cap (`TRIAL_MAX_TOKENS`) and a timeout. Counted before each call, on disk, so restarts don't
  reset them. A reached cap is said once a day, with when it resets and how to bring their own.
- **Budget guard** (`server/trial_budget.py`): trials must never push the plan into pay-as-you-go. Before each call:
  Ollama's `/api/balance` (read at most every 5 minutes; its rate limit is shared) stops trials once the plan's
  period is `TRIAL_MAX_SHARE` (0.5) used, by anyone using the key, and always once its included credits are gone;
  an unreadable balance keeps the last reading. Our own token count stops them at `TRIAL_MONTHLY_TOKENS` per UTC
  month in any case (the only guard while the balance can't be read). A 429 or 402 from Ollama pauses every trial
  (its Retry-After, else `TRIAL_PAUSE_S`), says so to the visitor and logs it for the operator.
- **One call at a time** for the whole server (the plan allows 3, shared): a message waits up to
  `TRIAL_QUEUE_WAIT_S`, then "busy, try again in a minute" (not counted).
- **Privacy**: only the visitor's own messages to it, its recent answers to them, and a requested PR diff go to
  Ollama Cloud, cut to size and scrubbed of anything shaped like a credential or a local path
  (`common/redact.py`); the answer is scrubbed too. Never other people's words in the room. Said on the privacy
  page, in its hello and in @pm's "ready" message.

## Consequences
- The balance is read between calls and other users of the key spend too, so `TRIAL_MAX_SHARE` is a soft line
  (read every minute once within 10 points of it). For a hard stop, the owner sets a pay-as-you-go spending limit
  in the Ollama account. A token estimate is reserved before each call so the token cap is never passed.
- Answers run as their own tasks (at most 6 waiting), so Matrix sync never waits on the model; the visitor's
  membership (and the room's repo, for a PR) is checked again before a PR is read and before posting.
- The operator sets `OLLAMA_API_KEY` in `deploy/.env` to turn it on (the other settings have defaults).
- Messages sent to it while @pm restarts are not answered (it starts from "now"; invites are still checked).
- A visitor who has the PR's room is assumed to be allowed to read its repo, as with @pm's status board.
- Step 2 (tools, a sandbox, building) is a separate decision.
