# Hagency Rooms — chat where each person brings their own agent (Matrix + Element)

One-day prototype: a stock Matrix homeserver (Synapse) and Element as the chat app, plus a
Python agent adapter that lets each person's **own** agent (Codex, Claude Code, a local Ollama model
or a remote model, on their own computer) join rooms as a Matrix user (`@anna-agent`, `@betty-agent`);
Hermes joins through its own Matrix gateway, with our adapter handling only its code-change buttons.

## Get started (no setup)

1. Ask the owner for an invite token, then sign up at **https://rooms.hagencylabs.xyz/#/register**
   (or in any Element app, with homeserver `https://matrix.hagencylabs.xyz`). Your ID is
   `@you:matrix.hagencylabs.xyz`.
2. You land on the Hagency home page: three numbered steps, each one click. **① Get your agent**
   opens a page in a new tab (`/connect/new`): sign in with the same account, choose the AI, and
   click **Get my command**. The home page is always one click away: the Home button at the top left.
3. Or click 🔌 **Bring your own** in your private chat with **Product Manager** (@pm): the same, in the chat.
4. Either way you get a one-time code and the **Connect page**
   (`https://rooms.hagencylabs.xyz/connect/start#ABCDE-FGHJK`). Open it on the computer where Codex
   or Claude Code is installed: it sees whether that's a Mac, Windows or Linux and shows numbered
   steps with a picture (open Terminal or PowerShell, one big **Copy command** button, paste, press
   Return), a "Before you start" checklist for your kind of agent, and tabs for the other systems.
   It then says "Waiting for your computer…" and turns to "✓ Connected!" by itself once the command
   has used the code (or "expired", with a link back to @pm). The code is after the `#`, so it's
   never in a link the server sees. The page sends it only in the body of that status check, which
   answers just waiting / connected / expired.

   ```bash
   curl -fsSL https://rooms.hagencylabs.xyz/connect.sh | sh -s -- ABCDE-FGHJK                  # Mac, Linux
   ```
   ```powershell
   & ([scriptblock]::Create((irm -UseBasicParsing https://rooms.hagencylabs.xyz/connect.ps1))) -Code ABCDE-FGHJK  # Windows
   ```

   It installs Hagency Rooms in `~/.hagency/app` (on Windows `%LOCALAPPDATA%\Hagency\app`), keeps
   your agent's sign-in in `~/.hagency/credentials.json` (only you can read it; if another agent's
   sign-in is already there, in `~/.hagency/<your agent's name>/` instead) and starts your
   agent, `<you>-agent`, in the background (launchd on macOS, systemd on Linux, a Scheduled Task
   "Hagency agent <you>" on Windows): you can close the window, and it starts again when you log
   in. Accept its chat invite (top left, under Invites) and talk to it; it works on your computer and obeys only you. If it
   goes offline, @pm tells you and sends the line again (running it again is safe). To stop it:
   `~/.hagency/app/.venv/bin/python ~/.hagency/app/bin/connect.py --stop`, or Disconnect under My agent
   in the Hagency panel ("@pm disconnect my agent" works too). On Windows the same `--stop` works
   with `%LOCALAPPDATA%\Hagency\app\.venv\Scripts\python.exe %LOCALAPPDATA%\Hagency\app\bin\connect.py --stop`
   (it ends and removes the Scheduled Task).
   On Windows, Claude Code from its installer (`claude.exe`) and npm's `codex`/`claude` both work:
   the agent never starts a CLI through a shell, so for npm's `.cmd` wrapper it starts the program
   the wrapper names (`cli_path.py`).
   **Which agent** (docs/adr/0008-agent-kinds.md): the kind you chose with @pm (its Connect link
   and line then name it, `--agent ollama`), or
   `--agent codex|claude|hermes|ollama|remote` (Windows: `-Agent`, no Hermes). **Hermes** needs
   Hermes Agent with a model and Hagency's invite patch (`docs/hermes-matrix-invite-allowlist.patch`),
   on macOS or Linux; it runs as its two profiles, like on the server. **Ollama** runs a model on your
   computer through Codex (no OpenAI sign-in needed): the line lists your models that can use tools
   and you pick one (or `--model qwen3:8b`). **Remote** is any OpenAI-compatible service that speaks
   the Responses API, also through Codex (`--base-url https://…/v1 --model …`): your API key is
   asked for only on your computer, never shown, kept in `~/.hagency/remote.env` (only you can read
   it) and never sent to @pm; `--new-key` replaces it.
5. **More agents** (up to 3 each, e.g. Codex as well as Claude Code): type "add another agent
   called codex" in @pm's chat. Once your first one has connected, @pm's Connect messages also
   carry a ➕ and the panel shows **Add an agent** under My agents. @pm asks for a one-word name if
   you gave none, then sends that agent's own Connect page and code. It runs beside the first one (`<you>-codex-agent`, its files in
   `~/.hagency/<its name>/`; stop it with `connect.py --name <its name> --stop`). The panel's My
   agents shows each one Online or Offline with its own Disconnect, "comes along" setting (only the
   first comes along by default) and Remove. Disconnect only signs an agent out (connect it again
   any time); **Remove** (click twice, or "@pm remove my agent codex" and ✅) closes its account for
   good and frees its place, and the first agent goes last. Its name can be used again: the new
   agent gets a new account (`<you>-codex2-agent`). **Add an agent** also picks the AI it runs
   (Codex, Claude Code, Hermes, a local Ollama model, or a remote OpenAI-compatible model by base URL
   and model; "@pm add another agent called pi using claude"); a remote model's API key is entered
   only on your computer when it connects, never in a chat. All of this is also in **My Hagency**,
   the panel at the top of your chat with Product Manager (the home page's **⚙ My agents &
   settings**). **Rename** changes only the name people see (its ID stays; "@pm call my agent NAME" or
   "@pm rename my agent codex to NAME" works too), even while its computer is off. @pm never lets two of your agents review each other: a review always needs a different
   person's agent.
6. Back on the home page, **② Start or join a project**: **Create a project room** opens Element's
   new-room dialog; then invite **Product Manager** to the room, and the Hagency panel at the top
   lets you pick the project's GitHub repo. **Find a project to join** opens the room directory.
   **③ Invite people**: Room info → Invite (people on other Matrix servers too, by their full ID).

**Already on matrix.org (or another Matrix server)?** No sign-up needed: join the public Lobby,
`#lobby:matrix.hagencylabs.xyz` (the site links it: "Already on Matrix? Join the lobby"), and click 👍
under @pm's greeting; or start a private chat with `@pm:matrix.hagencylabs.xyz` in your own Element. @pm answers in your chat and sends you its own
private chat with the buttons: accept it under Invites, then steps 3 and 4 as above. Your agent
gets an account on this server (e.g. `@alice-at-matrix-org-…-agent:matrix.hagencylabs.xyz`) and
obeys only your full ID. Your Hagency panel in Element signs you in with your own server (checked over
federation); elsewhere, say "@pm send me my console link" in a room you share with @pm.

The site is https://www.hagencylabs.xyz. How the public server is run and deployed:
[docs/DEPLOY_ORACLE.md](docs/DEPLOY_ORACLE.md). On a local server (below), `python bin/local_signup.py setup`
turns on sign-up by token (and the Welcome room), and `python bin/local_signup.py token` makes a token
for http://localhost:8649/#/register.

## Developers

Code lives by where it runs (docs/adr/0011-repo-layout.md): `server/` (@pm, console, panel),
`client/` (connect and the background service on people's computers), `adapters/agents/` (the agent
adapter that links an AI to the rooms), `common/` (shared), `mcp_tools/`. `bin/` holds the small
launchers that people, background services and @pm's container run by path (`python bin/bridge.py`,
`bin/connect.py`…, docs/adr/0012-launchers-in-bin.md); `local/` the local test server (`local/start.sh`,
its Docker Compose file and settings); `deploy/` the public server; `docs/` the guides, ADRs,
`REVIEWS.md` and `TIMELOG.md`. The root keeps only the README, licence, security and contributing
notes, `requirements.txt` and these folders.

| What | Where |
|---|---|
| Synapse homeserver | http://localhost:8648 (server name `bridge.localhost`, Docker, 127.0.0.1 only) |
| Element Web | http://localhost:8649 |
| Accounts | `anna`, `betty`, `anna-agent`, `betty-agent` — passwords generated into `.env` (git-ignored) |
| Room | `#lounge:bridge.localhost` (anna, betty, both agents; unencrypted) |

## Run it locally

1. Start everything — server, accounts (first run only), @pm and each person's agent:

   ```bash
   cd hagency-rooms && local/start.sh      # in your clone
   ```

   Leave it running (Ctrl-C stops the agent adapters; the server keeps running in Docker).
   To use Claude Code instead of Codex: `local/start.sh claude` (run `claude` → `/login` first).

2. Look up anna's password (it is never printed by the scripts):

   ```bash
   grep PW_ANNA .env      # in your clone
   ```

3. Open **http://localhost:8649** in your browser (or Element desktop: on the sign-in screen
   click **Edit** next to the homeserver and enter `http://localhost:8648`).
   Sign in with username `anna` and that password. Dismiss the "back up your chats" prompt.

4. Open the **Lounge** room. Type `@anna-ag`, wait a second, press **Tab** to turn it into a
   mention pill, then type your question and press Enter, e.g.
   `@anna-agent what files are in your working folder?`
   You'll see "anna-agent is typing…" and then the reply (Codex, ~5–40 s).

5. Optional: sign in as `betty` in a private window (`grep PW_BETTY .env`) and mention
   `@betty-agent` — each agent runs its owner's CLI, keeps its own per-room session, and stays
   quiet when it isn't mentioned.

   **An agent only obeys its owner** (betty mentioning `@anna-agent` is ignored), because for
   its owner it runs their CLI exactly as they configured it, with their tools and access
   (docs/adr/0006-owner-cli-powers.md). With `ALLOW_MEMBERS=1 local/start.sh` other joined members
   can ask it too, but their questions run locked down (read-only, no web, no MCP).

**Invite the agent somewhere else:** create a room, *turn encryption off* in the create dialog,
and invite `@anna-agent:bridge.localhost` — it joins automatically. A DM with the agent works
without a mention. In an encrypted room the agent says it can't read it (no E2EE in v1).

## How it works

- `bin/bridge.py <owner> [--agent codex|claude]` logs in as `<owner>-agent`, auto-joins invites from
  local users, and reacts only to messages that **@mention it** (pill or plain `anna-agent`) or
  arrive in a 2-person DM, sent by its **owner** (or, with `--allow-members`, any joined room
  member) that is not itself an agent (loop guard). Pending invites at startup get the same
  local-server check as live ones; at most 3 CLI turns run at once per agent adapter. Earlier un-addressed room chatter (last 20 lines) is passed as context.
- `agents.py` runs the CLI with the prompt on **stdin**, no shell, in
  `~/.hagency/work/<owner>/<room>/` (outside the app; `workdirs.py` moves an old
  `rooms/<owner>/<room>/` folder there with its sessions), and resumes that room's saved session
  (`codex exec resume <id>` / `claude --resume <id>`).
  - **The owner's own message:** the owner's CLI with all their settings, MCP servers and deny
    rules, plus what a chat room needs to work without an approval prompt (`owner_cli.py`): Claude
    Code gets `--permission-mode acceptEdits --allowedTools WebSearch,WebFetch,Edit,Write,Bash`;
    Codex gets `approval_policy="never"` and, unless its `config.toml` says otherwise,
    `workspace-write` with network and live web search. Their deny rules and explicit Codex
    settings still win; whatever is refused, the reply names it and the setting that blocked it.
  - **A question to the owner** ("Tell me a city or zip code?") stays open for 30 min: the owner's
    next un-addressed messages there are offered to the agent, which answers or stays silent
    (`bridge_followup.py`).
  - **Everything else** (a member's question under `--allow-members`, loop builds/reviews/plans,
    @pm's brain, code changes): Codex `sandbox_mode=read-only` (`workspace-write` for builds),
    `approval_policy=never`, connectors/plugins/MCP/web off; Claude `--tools Read,Grep,Glob`
    (`+Edit,Write` for builds) `--strict-mcp-config --setting-sources user`.
  - Replies are scrubbed of anything shaped like a credential (`redact.secret_safe`). Saved
    session ids are validated before they reach the CLI's argv.
- Typing indicator is refreshed every 20 s while the CLI runs; the reply is posted as a Matrix
  reply with markdown rendered to HTML.

## Live code changes

`local/start.sh` runs the agent adapters and @pm under `bin/supervise.py`. Save any `.py` file and they
restart by themselves within a second; you never restart the server. A file with a syntax error
is not loaded; the running version stays up and the error is printed. On restart, each process
finishes the answer or loop step it is in the middle of, and the new one picks up messages sent
in between, so nothing is lost or answered twice (`lifecycle.py`). A process that crashes is
restarted with a growing delay.

The loop never waits forever either. A step that gets no answer is sent again once, then the
loop stops on that issue. An approved PR with merge conflicts, or one GitHub keeps blocking for
15 minutes, also stops the issue. A stopped issue always posts "a person takes over" and the loop
moves on to the next one.

## Tests

```bash
.venv/bin/python -m unittest tests.test_rules tests.test_agents tests.test_loop_rules tests.test_loop_watchdog tests.test_lifecycle tests.test_github_setup tests.test_pm_chat tests.test_hagency_mcp tests.test_status_board
```

```bash
.venv/bin/python -m tests.test_e2e
```

`test_rules`/`test_agents` cover who gets answered (owner-only, mentions, DMs, invitees,
non-members, other agents, malformed mentions) and CLI argv/session-state safety. `test_e2e`
drives the real flow over the Matrix API with the agent adapters running: anna and betty @mention their
agents, checks replies, typing, per-room memory across turns, that the other agent stays quiet,
that anna's agent ignores betty, and the encrypted-room warning.

`tests/test_pm_e2e.py` (stop `local/start.sh` first) talks to @pm in plain words: anyone can stop, a
contributor can't switch to autonomous, and the owner can, just by saying so.

`tests/test_restart_e2e.py` (stop `local/start.sh` first) saves a code file between two questions and checks
both are answered exactly once, then kills the builder's agent adapter mid-step and checks the loop
resends the step and the issue is still merged.

## Talking to @pm

Each room has one bot, `@pm` ("Product Manager"). People talk to it in plain words; the commands
`!help`, `!status` and `!level` still work as shortcuts. In your private chat with @pm you don't need
to write "@pm": everything you say there is for it (in project rooms, start with "@pm").

| Say | What happens | Who |
|---|---|---|
| "@pm take it all the way" | agents build, review and merge on their own, at once | owner or maintainer |
| "@pm let us approve each change" | each change waits for a person to merge | owner or maintainer |
| "@pm stop" / "turn it all off" | no new work and no merges, at once | anyone |
| "@pm where are we?" | mode, agents and a link to the pinned Project status | anyone |
| "resend me the code" / "new code" / "connect my agent" | a fresh Connect page and one-time code for your agent, in @pm's chat | you |
| "@pm make me a project room [called X]" | a new room you own, with @pm as moderator and the Hagency panel; your agent comes along when you join | anyone |
| "@pm find a project" | one message per listed or open project room: click 🙋 under a listed one to ask its owners, or join an open one by its link (also Element's Explore rooms) | anyone |
| "@pm ask to join owner/name" | the room working on that exact repo asks its owners (👍 let in · 👎 decline) | anyone |
| "@pm remove the repo" / "this room has no repo" | after your ✅: no repo, mode off, the repo free for another room; also **Remove repo** in the panel (click twice). Nothing on GitHub changes | owner or maintainer |
| "@pm make this room listed" (or private, open) | who can find and join it; also "Who can join" in the panel | owner or maintainer |

**One project room per repo.** @pm's build loop runs per room, so a room that picks a repo another
room already runs gets two buttons: 🙋 ask to join that room, or ➕ make a separate room anyway, which
may discuss the repo but never runs its loop (its mode stays off; its panel says where the repo is
run from).

**Who can join a project room** (owners and maintainers choose; joining never gives a say in how it runs):
- **Private** (default): not listed; by invite, or by asking @pm with the exact repo name.
- **Listed**: in Find a project and the server's room directory (other Matrix servers see it too).
  Element shows "Ask to join" (and 🙋 under "@pm find a project" does the same); @pm asks the room's
  owners and invites on 👍. A private repo's name is
  never shown in a listing.
- **Open**: listed, and anyone joins directly. Not for a room on a private GitHub repo.

A listed or open room gets a readable address made from its name (`#final-project:<server>`,
numbered if taken, never a private repo's name), unless its owners already gave it one; @pm removes
the address it made when the room goes back to private.

`@pm` understands from context. Every message addressed to it goes to an understanding step
(`pm_brain.py`): the recent conversation, the message being replied to, what's waiting for
approval, the plan and the board. The model picks one action from a fixed list and writes a
short reply.
- Code still checks who may do what, and code confirms every action, so the model can't claim
  something happened.
- Phrases it knows ("stop", "help", "where are we?") are plain rules (`intents.py`): never a model.
- If the model fails or is slow, @pm uses the rules.
- The model is the **room owner's own agent** (`PM_BRAIN=owner`, the public server's setting;
  [ADR 0022](docs/adr/0022-pm-understands-with-owner-ai.md)): @pm sends it the same context in one
  to-device message (nothing shows in the chat), and it answers from one turn with no tools at all.
  Offline, older code or slow: the rules, and @pm says the owner's agent is offline when they don't
  catch it. `PM_BRAIN=codex|claude` uses the host's CLI instead, run in the agents' lockdown;
  `off`, only the rules.
- In a project room you don't always need "@pm": a reply to @pm, your next words while @pm is talking
  with you, or anything when you're the only person there counts. Other requests are read (a few a
  minute) only to see whether they're for @pm. When only the model read it (or you didn't name @pm), it
  asks a 👍 before stopping, a mode, planning, a repo or a retry. Not in the Lobby or the Welcome room.
- Asks of the owner's agent are capped (per minute/hour: 6/30 per person, 15/100 per room, 20/150 per
  owner's agent across rooms, named or not), checked before any work; past that, the rules answer.

## Stop / reset

```bash
docker compose -f local/docker-compose.yml down      # in your clone
```

Delete `data/`, `.env` and `rooms/` to start from scratch.

## Step 2: the autonomous GitHub loop

`loop.py` runs a coordinator bot, `@pm`, in the room. It watches open issues labelled
`agent-ok` on **the room's own repo** and drives each issue through the room, one step per line.

**One room, one repo.** The room owner or a maintainer picks it: say `@pm this room works on
owner/name`, or use **Project repo** in the Hagency console (**Remove repo** there, or `@pm remove the repo`, takes it
off again; refused while work runs). The console's **Check readiness**
button runs the same read-only check as `bin/github_setup.py check`: each agent can write, merges
need an approving review, and the `agent-ok` label exists. The repo is stored as a list in the
room's `xyz.hagencylabs.loop` state, so a room can hold several repos later. A project that spans
repos is a **Matrix Space** with one room per repo. Create the Space in Element (＋ → Create a
space) and add the rooms to it. Each room keeps its own board, mode and agents.

**Which repos.** On a server with the Hagency Rooms GitHub App (docs/GITHUB_APP.md), a room's owner
or a maintainer may choose **any repo** that has the App installed and that their connected GitHub
account can write to. An operator may restrict this with `HAGENCY_REPOS` (empty: any repo). When a
room can't use a repo, @pm names the one missing step: the App's install link, the account that
can't write, or the server's list. **Locally, without the App**, rooms choose only from `LOOP_REPO`
plus any in `HAGENCY_REPOS="owner/a owner/b"`. Set both in `local/local.env` (copy `local/local.env.example`;
git-ignored), which `local/start.sh` loads; the code's own default is only a placeholder. There @pm reads
with the host's own `gh` login, so a free choice would let any room see any repo that login can read,
private ones included. The demo room starts on `LOOP_REPO`.

A step in the loop looks like this:

```
@pm     @betty-agent: build #136 docs: add docs/MAINTAINERS_NOTE.md (autonomous)
betty-agent  build: claimed #136
betty-agent  PR #140 opened for #136: https://github.com/…/pull/140
@pm     review requested from anna-agent
anna-agent   approved PR #140            (or: changes requested on PR #140 → loop stops)
@pm     PR #140 ready — a person merges          (Assist)
betty-agent  merged PR #140 (closes #136)               (Autonomous)
```

**Room level** is one Matrix state event (`xyz.hagencylabs.loop`) that a room moderator sets by
talking to `@pm` (see "Talking to @pm"); `!level off|assist|autonomous` is the shortcut.
It starts at `off`.
- Off: the loop does nothing.
- Assist: agents build and review, then a person merges.
- Autonomous: the builder agent merges.

**Who does what.** The builder is picked round-robin (issue number mod number of agents) and the
reviewer is always a *different* person's agent. Each step runs in that person's agent adapter with
**that agent's own GitHub identity**. @pm holds no GitHub credentials; it only reads.
- Build: the agent CLI edits a fresh clone with `.git` moved out of its sandbox. Codex runs with
  `workspace-write` and no network. The agent adapter then commits, pushes and opens the PR.
- Review: the agent checks the diff read-only against the issue's acceptance criteria. It must end
  with `VERDICT: APPROVE`; anything else becomes "request changes".
- Fix: on "changes requested", the builder's agent gets the reviewer's reasons and revises its
  own PR branch (a new commit, so earlier approvals don't count), and the same reviewer looks
  again. After 2 rounds without approval, a person takes over.
- Merge: the builder merges only when **GitHub itself** reports the PR `MERGEABLE` + `CLEAN`, and
  `APPROVED` when the repo requires reviews, re-checked in its agent adapter just before merging.

A repo with a required-review rule keeps GitHub's own gate. Without one, the loop merges on its own
reviewer's verdict, always another person's agent, so one GitHub account for everyone still works
(docs/adr/0016-one-github-account-loop.md).

### One-time GitHub setup (anyone running the loop)

Follow [docs/GITHUB_SETUP.md](docs/GITHUB_SETUP.md). In short:
1. Sign in one GitHub account per agent in `.gh/<agent>/`.
2. Give each account Write access to the repo.
3. Run `bin/github_setup.py ruleset` to make GitHub the merge gate.
4. Run `bin/github_setup.py check` and confirm you see `✓ ready`.

An agent without a signed-in identity refuses loop tasks with "no GitHub identity", and the
loop stops on that issue.

### Loop tests

```bash
.venv/bin/python -m unittest tests.test_loop_rules
```

The full loop test below runs real Codex agents against a fake GitHub (`tests/fake_gh.py` plus a
local bare repo, with the same rules: you can't approve your own PR, and reviews are required).
Stop `local/start.sh`'s agent adapters first, because the test starts its own:

```bash
.venv/bin/python -m tests.test_loop_e2e
```

## License and contributing

MIT, see [LICENSE](LICENSE). Pull requests welcome: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).
