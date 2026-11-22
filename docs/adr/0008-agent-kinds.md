# ADR 0008: Agent kinds on a person's own computer

Date: 2026-11-20 · Status: accepted

## Context
`connect.py` could run only Codex or Claude Code on a person's own computer. People also want
Hermes (only `onboard.py` set it up, on the server), a model running in Ollama on their machine,
and any OpenAI-compatible service with their own API key. Each kind must keep ADR 0006: the owner's
turns get the owner's powers, everyone else's turns are locked down, and no secret ever reaches
chat, @pm or a log.

## Decision

### The kinds and the contract (`agent_kinds.py`)
`agent_kinds.KINDS = ("codex", "claude", "hermes", "ollama", "remote")`. `agent_kinds.kinds()`
returns, per kind, `{"kind", "name", "program", "needs": [...], "windows": bool}`: what @pm's picker
and the Connect page show. That module is pure data and checks, so the @pm side imports it without
pulling in the bridge.

**Connect info** (the JSON answer of `POST /connect`, built by `remote_agent.credentials_for`) may
carry, besides `homeserver`, `user_id`, `password`, `owner` and `label`:

| key          | value                                                         | used by          |
|--------------|---------------------------------------------------------------|------------------|
| `agent_kind` | one of `KINDS`                                                | all              |
| `model`      | matches `agent_kinds.MODEL_RE` (no `..`)                      | ollama, remote   |
| `base_url`   | https, or http only to localhost/127.0.0.1/::1; no `user:pass@`, query, fragment, quotes, spaces or backslashes; ≤ 300 chars | remote |

- **Never an API key.** A remote service's key is typed only on the person's computer.
- **No `agent_kind`** (every older server): today's behaviour, Codex or Claude Code, whichever is
  installed (asked when both are).
- **A value that is present but wrong** (unknown kind, odd model, plain-http address) is refused by
  `agent_kinds.from_connect_info`. Only the sign-in is saved (the code is used up), never these
  values: the person would otherwise run a different agent than the one they chose.
- **Which kind runs:** `connect.py --agent KIND` (the person's own choice here wins, and a
  difference from @pm's is said). Else `agent_kind`. Else the kind saved last time. Else a further
  agent's label when it is `codex` or `claude`. Else the old automatic pick.
- **Name clash:** in `agents.json` the value `"remote"` already means "runs on the person's own
  computer". The kind called `remote` (an OpenAI-compatible service) is a different thing, so the
  @pm side keeps the kind in its own field and never in `"agent"`.
- `connect.py` saves the kind as `"agent"`, plus `"model"` and `"base_url"`, in `credentials.json`
  (mode 600). The bridge runs `bridge.py <owner> --agent <kind>`.

### The agent reports its kind to @pm (`kind_report.py`, `pm_kind_reports.py`)
An agent added with "AI: the one on my computer" has no `agent_kind` at @pm, so "My agents" showed
no AI even though its computer ran, say, Claude Code. Now the computer tells @pm what it runs.
- **When:** each time the agent adapter (`bridge.py`, also Hermes's buttons-only one) signs in on the
  person's computer. connect.py starts it, so a first connect, re-running the connect line (no code
  needed: connect.sh also updates the app) and an ordinary restart all report. The send has a
  15-second deadline and never stops the agent from starting.
- **What:** one Matrix to-device message, type `xyz.hagencylabs.agent.kind`, to `@pm` (all its devices),
  with `{"agent_kind", "model"?, "host"?}` (`pm_kinds.REPORT_KEYS`) built from `credentials.json`'s
  `"agent"`, `"model"` (ollama, remote) and, for remote, only the **host** of `"base_url"`: a path may
  hold a token, and the card shows only the host. Nothing else is read from that file:
  **never an API key**.
- **Why to-device, not an HTTP endpoint or presence:** Synapse sets the sender, so only the agent's own
  sign-in can report for itself, with no new authentication. It is private, unlike presence's
  `status_msg`, which everyone sharing a room sees. It waits for @pm's fixed device (`PM_BOT`) while
  @pm restarts; @pm listens before its first sync. A credentials file names the homeserver, not the
  console, so an HTTP report would need a new saved address on every computer.
- **@pm's checks (`pm_kinds.reported`):** the sender must be one of this server's agents recorded as
  `"agent": "remote"`; only that agent's own entry changes. The report is refused whole when it has
  any field but `REPORT_KEYS` (an `api_key`), a non-string or key-like value (`pm_kinds.looks_like_key`;
  for a model only when remote, since an Ollama model comes from the computer's installed list), a host
  with a scheme, port, user, path or query, or a kind, model or host that `pm_kinds.choice` refuses
  (the `KINDS`, `MODEL_RE` and base_url rules above). A refused report's content is never logged.
- **Stored:** as `"reported"` on that agent's own entry in agents.json (`registry.REPORTED`), whole each
  time, so a switch from Ollama to Claude Code drops the old model. `pm_kinds.describe` shows it over
  what was picked. It is **display only**: a new code still offers the picked `agent_kind` / `model` /
  `base_url` (the computer keeps its own settings anyway), and Stop / Start still go by the picked
  kind, so a sign-in claiming "hermes" can't hide them (GPT review).
- **Not known yet:** "My agents" shows `pm_kinds.NOT_REPORTED` ("AI: not reported yet") for an agent on
  a person's computer with no kind; a server-run agent shows no badge, as before.
- **Older installs:** an agent whose app predates this reports nothing until its connect line is run
  again (that updates the app); after that, every start reports.

### How each kind runs
- **codex, claude:** unchanged.
- **ollama** and **remote** run through the **Codex CLI with another model provider**
  (`codex_provider.py`). Each turn is the same `codex exec` as the codex kind: ADR 0006's owner
  arguments for the owner's turns and the full lockdown for locked turns. On top come only `-c`
  overrides and `-m <model>`:
  - ollama: `-c model_provider="ollama"`, Codex's built-in provider (`http://localhost:11434/v1`).
  - remote: `model_provider="hagency_remote"` and
    `model_providers.hagency_remote.{name, base_url, env_key="HAGENCY_REMOTE_API_KEY", wire_api="responses"}`.
  - **Why Codex:** it is already a supported CLI here, with a real sandbox (workspace-write, network
    switch) and `approval_policy`, so ADR 0006's owner powers and lockdown hold unchanged. It speaks
    to any Responses-API provider through `model_providers`, and it can resume sessions.
    - We rejected a small agent loop of our own: no sandbox, so a new attack surface.
    - We rejected Hermes as the driver: one toolset for every sender, which needs ADR 0006's
      two-profile workaround.
    - We rejected Claude Code: its custom endpoints are Anthropic-API only.
  - **`-c` overrides, not `--oss`:** `codex exec resume` has no `--oss` flag. The `-c` overrides
    also beat the owner's profile layers.
  - **What it needs:** Codex removed `wire_api="chat"` in February 2026, so a remote service must
    serve the Responses API (`/v1/responses`): OpenAI, OpenRouter, vLLM, LM Studio, Ollama (0.13+)
    and others do. An Ollama model must support tool calling, and connect.py offers only those
    (`capabilities` from `/api/tags`).
  - **Web search:** the hosted web-search tool is OpenAI's. So for these kinds, an owner turn gets
    `web_search="disabled"` where the owner's config sets none (owner_cli); the shell's network
    stays on as in ADR 0006.
- **hermes** runs on the person's computer as ADR 0006 runs it on the server: two profiles (personal
  `<name>room`, owner only; loop `<name>loop`, @pm only), logged in as the same agent account.
  - **Set-up:** `hermes_local.py --setup` writes them from the saved sign-in. It runs as its own
    process, with `HAGENCY_CREDENTIALS` set, because the bridge's settings are read at import. The
    homeserver comes from the credentials file. The personal profile obeys the owner's full Matrix
    ID. The hagency MCP server signs in with the same file (`mcp_core` uses
    `bridge_config.agent_password`).
  - **Running:** the background service runs `hermes_local.py`. It keeps both gateways and the
    code-change buttons bridge running, using `supervise.hermes_children`, which the server uses
    too. When the buttons bridge exits cleanly (the server disconnected the agent), everything
    stops, so the service stays stopped.
  - **Checks first:** connect.py checks that `hermes` is installed, that a model is set, and that
    Hagency's invite patch (`docs/hermes-matrix-invite-allowlist.patch`) is in Hermes's Matrix
    adapter. Without the patch, anyone could pull the agent into their room, so connect.py refuses.

### Settings, prompts and the key (`connect_kinds.py`, `ollama_local.py`)
- **Code safety:** before the one-time code is used, the chosen kind's tools must be there (or,
  with no `--agent`, at least one agent CLI). After the code is used, the sign-in is saved at once.
  If a tool is still missing, the person is told what to install and to rerun `connect.py`, which
  needs no code.
- **ollama:** connect.py reads only `GET http://localhost:11434/api/tags`. It never pulls a model.
  The model must be installed and able to call tools. `--model`, else @pm's, else the last one is
  checked against the list. If none is named, the person picks a number at the terminal. With no
  terminal, it refuses and lists `--model` choices.
- **remote:** the base_url and model come from flags, @pm or the last run, and are asked at the
  terminal when missing (with no terminal, refused). The key:
  - **Typing it:** asked with `getpass` (never echoed) after showing the address it will be sent to.
    It is never accepted from argv or the environment. With no terminal, connect.py refuses.
  - **Storing it:** saved as `remote.env` (mode 600, a fresh temp file swapped in) beside the
    agent's `credentials.json`, **together with the address it was typed for**. It is not used if
    others can read the file.
  - **Asked again** whenever the address changes (a saved key never goes to a new address), and on
    `--new-key`. The file is deleted when the agent switches to another kind.
  - **At turn time** it reaches Codex only as the `HAGENCY_REMOTE_API_KEY` environment variable of
    that one process, and only when the turn's `base_url` is the address saved with the key. A
    bridge still running with the old address during a reconnect therefore gets no key.
  - **Never shown:** the key is never in argv, the service file, a log or `os.environ` of the
    bridge. Codex's own shells get it blanked
    (`shell_environment_policy.set.HAGENCY_REMOTE_API_KEY=""`), so a command the model runs can't
    print it. A reply or CLI error that echoes the exact key has it cut out before it can reach the
    room or @pm.
  - **Remaining risk (as in ADR 0006):** the agent's own tools can read files its sandbox allows,
    including `remote.env`, just as they can read `credentials.json` and the CLI's own sign-ins. The
    owner's deny rules and the CLI's sandbox are the defence. Saved sessions are kept per service
    address, so a conversation never resumes at another provider.
- **Settings stay with their kind:** `connect_kinds.pending_for` keeps a model or address only for
  the kind it was chosen for, and @pm's only when @pm named that kind. It is saved before any
  check, so a rerun after "install X first" still has @pm's model.
- **Odd values from @pm:** if @pm's connect info has a kind, model or address this computer won't
  use, the sign-in (the used code) is still saved and nothing else. The person is told to rerun
  with `--agent`.
- Questions are printed on their own line, then read from stdin (`ask_line.py`). connect.ps1 pipes
  output through `Out-Host`, which shows a line only once it ends.

### The Connect page (`connect_page.py`, `connect_status.py`, `static/connect.*`)
- **The link:** `connect_page.page_link(code, kind)` gives `…/connect/start#CODE&kind=KIND`. The page
  shows that kind's needs from `agent_kinds.kinds()` (embedded as JSON data, not script) in a
  collapsed "Before you start". The command line also names the kind (`--agent` / `-Agent`), so
  the computer's tools are checked before the code is used. On Windows, Hermes gets a plain note
  instead of a command.
- **The live status:** the page polls `POST /connect/status {"code"}` (every 3 s; slower after an
  error; for at most 35 minutes) and stops on `connected` or `expired`. The answer is only
  `{"state": "waiting"|"connected"|"expired"}`:
  - **connected** means `POST /connect` handed out the sign-in for that code (remembered in memory,
    by hash, for 30 minutes).
  - **Limits:** 30 asks a minute per client, and refused asks aren't counted. Codes that aren't
    live count as wrong codes, per client only, on a limiter of its own. So guessing here is as
    limited as at `/connect`, many guessers can't stop anyone else's status, and a page can't lock
    out connect.py. The page asks only about a whole code, never one still being typed.
  - **Content-Security-Policy:** it now allows `connect-src 'self'` and nothing more. The code
    still never appears in a URL the server sees.
- **The "expired" link:** it points to `ELEMENT_ORIGIN/#/user/@pm:<server>`, the same for everyone,
  so it reveals nothing.

### Windows
- **Supported:** `connect.ps1` takes `-Agent codex|claude|ollama|remote`, `-Model`, `-BaseUrl` and
  `-NewKey`. It passes only what was given; connect.py then asks the rest in the same window. Its
  `-Model` pattern is `MODEL_RE` (a test keeps them equal).
- **Not supported:** Hermes (macOS / Linux only; Hermes itself runs on Windows only inside WSL).
- **Further agents:** still run in a window there, as in ADR 0005.

## Consequences
- **Server side (done in batch 12–14):** @pm's picker (`pm_kinds.py`, the panel's Add an agent)
  offers exactly `agent_kinds.kinds()` and checks with `agent_kinds.valid_model` / `valid_base_url`;
  `registry` keeps the choice in `agent_kind` / `model` / `base_url` next to `"agent": "remote"`;
  `remote_agent.credentials_for` adds those fields to the answer; and every Connect message
  (`pm_agents.send_code`: first agent, ➕, resend, 🔁, the panel) links
  `connect_page.page_link(code, kind)` and a line ending in `--agent KIND`. Older connect.py
  installs ignore the new keys and behave as before, and connect.sh updates them first anyway.
- **What a person needs:** ollama and remote need Codex installed, but no OpenAI sign-in. Hermes
  needs a patched Hermes. Each kind's needs are listed in `agent_kinds.ABOUT`.
- **Model output:** a remote or local model's replies pass through the same bridge `secret_safe`
  and `room_safe` as every other reply. Its quality and its tool use are the provider's.
- **Turn sessions:** these are kept per kind (`ollama`, `remote`), so switching kinds starts a fresh
  conversation in each room.
- **The first agent too (screen test 4):** a newcomer chooses the AI of their first agent with
  "bring my own using …" or My Hagency's picker (`pm_first.py`, `console_agents.first_pick`), the
  same fields on the first agent's own record. They may change it until it has ever connected;
  after that its AI stays, and another AI is another agent.
