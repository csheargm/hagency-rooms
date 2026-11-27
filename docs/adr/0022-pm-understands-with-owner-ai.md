# 0022: @pm understands requests with the room owner's agent

## Status
Accepted, 2026-11-26 (owner: "PM is supposed to use the room owner's AI to understand the commands.
Also PM should know I'm talking to him rather than requiring @pm all the time." PM uses the ROOM
OWNER's agent, not the speaker's).

## Context
- `pm_brain.py` already had an understanding step: the recent conversation, the message replied to,
  what waits for approval, the plan and the board go to a model with no tools, which picks ONE action
  from a fixed list plus a short reply; code then checks who may do what.
- The public server has no model keys, so it ran `PM_BRAIN=off`: @pm only matched phrases
  (`intents.py`) and often said "I didn't catch that".
- Every person already brings an agent that runs on their own model and account. In a project room
  @pm only listened to messages starting with "@pm" (or mentioning it).

## Decision
### Whose AI, and how it is asked
- `PM_BRAIN=owner` (now the default in `deploy/docker-compose.yml`; `claude|codex|off` still work).
  @pm asks the **room owner's** own agent: the first owner (`room_state.owners`: the v12 creators, then
  anyone at power level 100; never @pm, an agent or a bot) whose first agent is in the room, online,
  not stopped and not Hermes.
- The ask is one **to-device message** (`xyz.hagencylabs.pm.understand`) with the same context
  `pm_understand.think` builds, bounded (`pm_actions.clean_context`). Not a room message like the card
  task (ADR 0019): every message to @pm would otherwise add three lines to the chat and copy the
  conversation into the room's history again.
- The agent (`adapters/agents/understand_task.py`) answers `ack` at once, then `{action, reply}` or
  `{error}` (`xyz.hagencylabs.pm.understood`). The prompt and the parser are `common/pm_actions.py`,
  the one copy both sides use (R3); `pm_brain.py` imports them too.
- @pm waits 8 s for the ack and 45 s for the answer (`PM_OWNER_TIMEOUT_S`). No owner's agent here,
  stopped, Hermes (each said as such), offline, over its cap (below), no ack (older code, asleep: then not asked again for 10 minutes), slow, failed, or an action
  outside the list: @pm uses its phrase rules, and where those don't catch it, says plainly that the
  room owner's agent is offline or not answering, so it is using simple phrases.
- Only a named message the phrase rules don't recognize goes to a model. "@pm stop" stays instant;
  help, status and every other recognized command keep their fixed answers and never wait for a model
  (so an older adapter never adds its 8 s wait to them). Whatever the model picks that CHANGES anything
  (stop, a mode, a plan or a redraft, approve, a follow setting) is only its reading of the words, so
  @pm asks the usual 👍 first, even in a named message; read-only answers (status, the plan, the
  console link, a plain reply) come at once. An approval stays a click (or a typed OK on the question).
- Every stop, whatever triggered it ("@pm stop", a 👍 on "stop?", `!level off`, the console), goes
  through one place (`pm_understand.invalidate`): it advances the room's generation and drops the
  question waiting. A 👍 question is installed only if no stop came and the waiting question is still
  the one seen before it was sent (`ask_once`); a 👍 or typed OK is consumed under the same check. A
  plan question is never replaced: a change to it is asked on the side (`ask_redraft`).

### Without "@pm" (`server/pm_implicit.py`)
In a project room a message that doesn't name @pm counts as for @pm when it **replies to @pm**, comes
from **the person @pm was just talking with** (a question of mine waits for them, or I answered them in
the last 3 minutes), or comes from **the only person in the room**. Any other message goes to the
understanding step only when one is available, only when the phrase rules read an intent in it or it
looks like a request, and at most 6 a minute per room; the prompt then says the message did not name
@pm and asks for `none` with an empty reply when it isn't for @pm. A message plainly for someone else
(a mention, "anna: …", "anna-agent …", an edit) never counts. Nor does anything in the Lobby or the
Welcome room (`project_rooms.is_front_door`): only "@pm" there. A reply to @pm that nothing understood
gets the same "didn't catch that" (with why) as a named message, never silence; a reply to the plan
waiting for ✅ asks "redraft the plan with that?" and a 👍 from that person redrafts it.

Acted on without being named, @pm only answers low-impact things directly (status, the plan, help,
about, first steps, the console link). Stop, a mode change, a plan, a repo, a retry are **asked first**
with the usual 👍 question (pending kind `implicit`), confirmed only by the person asked; the action's
own permission checks run again on the 👍, or a typed OK that replies to the question or names @pm (a
bare "yes" said to someone else never confirms it; from the person asked, @pm says how to confirm).
Such a question never replaces a waiting plan, plan request, mode or setting question. Any explicit mode change drops such a question. A
repo name or an issue number is never left to a model, there as everywhere. An approval stays a click
on the question. All of this runs in the background, so the sync loop (and a "@pm stop") never waits.

## Security
- The model never gets tools: Claude Code runs with `--tools ""`; Codex (and the ollama and remote
  kinds) run locked down with the shell switched off too (`features.shell_tool`, `features.unified_exec`;
  checked live with Codex 0.169: it reported no shell). A fresh turn, in an empty folder.
- The agent takes the task only from @pm's account (Synapse sets a to-device sender), only for a room
  that it, its owner and @pm are in, only when its owner is one of that room's owners (read now), only
  when it is recent (2 minutes) and the agent isn't stopped, at most 2 at once.
- @pm takes an answer only from the agent it asked, for that task id, and checks it again: one of the
  fixed actions, a reply without links or local paths, at most 500 characters. Permissions stay code's.
- The context is the room's recent conversation as @pm reads it. Accepted risk (GPT review): in a room
  whose history is "joined only", @pm can read lines from before the owner joined, and their agent then
  gets them; planning (pm_plan.py) already sends the same discussion to the requester's agent.
- Accepted risk (GPT review; the same as ADR 0021): the ask is a to-device message to device `*`, so
  every device logged into the owner's agent's account receives the room context. Matrix to-device
  carries no sender device, and agents use a fixed device id, so @pm can't bind it to one device.
  Mitigation: right before sending, @pm checks again that the agent is still a joined member and that
  its owner is still one of the room's owners (read now); @pm's check of the answer (full sender id
  plus the task id) stays as it is.
- Anyone can send @pm a to-device message: an answer counts only with a string task id @pm issued,
  from the agent it went to.

## Consequences
- The room owner's model and account pay for @pm's understanding in their rooms. Every ask of their
  agent, named or not, is capped (`pm_owner_brain.SENDER_CAPS`, `ROOM_CAPS`): 6 a minute and 30 an hour
  per person, 15 a minute and 100 an hour per room, and 20 a minute and 150 an hour per owner's agent
  across all its rooms (`AGENT_CAPS`); past that @pm uses the phrase rules (and says so). Admission
  is checked before any context is collected or history fetched, so a refused burst does no work, and
  at most 12 understanding jobs run at once (`pm_understand.MAX_JOBS`; past that, the phrase rules
  answer a named message and an unnamed one is skipped).
  Unaddressed reads are capped again at 6 a minute per room; ordinary chat that the rules don't read as
  anything is never sent. A plain "@pm stop" never counts against any cap.
- @pm's "about" answer tells everyone in the room that recent messages, including ones that may not
  be for @pm, go to the room owner's agent; help says when "@pm" can be skipped.
- Agents from before this change don't answer: the first message waits 8 s, then the rules answer and
  that agent isn't asked again for 10 minutes. Running its connect line again (or Update) fixes it.
- Hermes agents can't answer yet (no to-device listener there): rooms they own use the phrase rules.
- With `PM_BRAIN=claude|codex` the unaddressed path uses the host's model, with the same caps.
