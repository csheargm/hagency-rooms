# Review log

GPT reviews of each change (R15): what was adopted, with tests, and what was deferred.

## GPT review (R15) — what changed

Codex reviewed the first commit. Found no shell/flag injection from chat (prompt on stdin).
Adopted (each verified in code, with tests): owner-only default (CLI can read the owner's disk);
Claude limited to `Read,Grep,Glob` + no MCP; nio's `room.users`/`member_count` include
*invitees* → DM and membership checks now use joined counts and exclude invitees; pending invites
at startup now pass the same check; malformed `m.mentions` no longer crash the callback;
workdir names get a hash (sanitising was lossy); saved session ids are validated before reaching
argv (my first regex let `--x` through — the new test caught it); atomic state writes; reap the
CLI after a timeout kill; global cap of 3 concurrent CLI turns; `.env` created 0600 and saved
before each registration.
Not adopted: running the CLIs under a separate OS user/container (right for production, out of
scope for a one-day prototype — owner-only is the stop-gap); sanitising markdown HTML (Element
sanitises `formatted_body` per the Matrix spec); generic error messages (CLI errors in chat are
what make the fix cycle fast here; they show to the owner's room only).

## GPT review of the loop (R15)

GPT found 10 issues. Adopted, each verified in the code and covered by a test:
- **Reviews:** the verdict is only an exact final line. A diff that's too large to review in full
  is never approved.
- **Commit pinning:** the review is pinned to the head commit (`commit_id`), and the merge uses
  `--match-head-commit` with that same commit, so a push after approval can't slip in.
- **Builder's PR:** the loop checks that the PR the builder reports is really the builder's own
  `agent/<agent>/issue-<n>` branch from the same repo.
- **Results:** each result must echo its task ID and match the issue's current stage, its
  assigned agent and its PR. A redelivered task runs once. Malformed results can't break the
  sync loop.
- **Merge timing:** the bridge re-reads the room level just before merging. The loop marks an
  issue done only once GitHub reports `MERGED`.
- **Isolation:** Claude runs with `--setting-sources user`, so it never loads project `.claude/`
  hooks or settings from the cloned repo. Host-side git runs without user or system config,
  without hooks and with fsmonitor off, so an agent-written `.gitattributes` can't trigger a
  filter.
- **Identity:** `GH_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GH_HOST` overrides are scrubbed for
  agent calls.

Not adopted, deferred to a real deployment:
- OS-level containment (a separate user or container) for the agent CLIs.
- Durable dispatch with reconciliation after a crash between "send" and "save". Today, after a
  crash a person may need to retry an issue.
- Restricting who may write the level state event to @pm only. Today any room moderator
  can, which is the same group allowed to use `!level`.

### GPT review 3 (live reload, hand-off, watchdog — 80a0a8f)

Adopted, with tests: cutoff set before resumed callbacks run (#1); one lifecycle owner per child in
the supervisor, no respawn during shutdown, SIGTERM stops children (#2, #6); a resend of a running
step is coalesced in the bridge and the original's result still counts (#3); loop transitions are
serialized by one lock and shielded from restart cancellation (#4, #8); CLI/gh children are killed
on cancellation, not only on timeout (#6); handled ids unbounded per batch (#9); the GitHub timeout
is judged after GitHub's latest state (#10). Partly: the hand-off is kept until resume succeeds and
outlives a full drain (#5); Matrix send errors are now raised and retried (#7).
Deferred: a rolling crash checkpoint and a durable outbox for dispatches (#5, #7), and reconciling
an existing branch/PR when a resent build finds one (#3) — today such a retry fails and the loop
stops for a person.

## GPT review 4 (820fb83..3d41fd0: setup-bot, Hermes MCP, status board, @pm)

Adopted, with tests:
- **1. A stray "yes" could confirm autonomous.** Confirmation now comes only from the person who
  asked (reaction or "yes"); any mode change cancels it; authority is re-checked at confirm time.
- **3. Historical tasks stayed usable over MCP.** Tools act only on the agent's current
  (newest, unanswered) task.
- **5. "Stop" didn't stop follow-ups.** A step decided while the room is off is held, and sent
  when the room resumes.
- **8. Diff/approval race.** The PR head is re-read after the diff; if it moved, the review is
  refused.
- **9. Local paths in room messages.** All error text goes through `redact.room_safe`.
- **10. Malformed input could kill @pm.** `m.mentions` shape is validated; GitHub errors in
  result handling and chat are caught.
- **12. Stale README line about setup-bot.** Fixed.

Deferred:
- **2. Maintainers can write the level state directly.** They can already do the same through @pm.
- **4. One person could act through two agent accounts.** Needs explicit agent enrollment, which
  onboarding (`agents.json`) is the start of.
- **6. A watchdog resend can replace an active checkout.** Only after the 20-minute step timeout.
- **7. Filesystem race on write_files paths.** Needs local FS access, which room agents no longer
  have.
- **11. A failed pin is cached as success.** Cosmetic.

## GPT review 6 (17b654d: @pm's understanding step)

Adopted, with tests:
- **1. "stop" could be held up behind a slow model call.** The model now runs in the
  background, so the sync loop never waits. "stop" bumps a per-room generation that cancels
  in-flight decisions. Homeserver calls have a 30 s timeout; typing is best effort.
- **2. Indirect injection through other people's messages in the context.** Mode changes and
  approvals the model proposes happen at once only when the person's own words say so (the rules
  agree, or an OK reply to the waiting question). Otherwise @pm asks for a ✅.
- **3. Model text in @pm's voice; Codex's read-only sandbox can still read files.** The
  understanding step now runs Claude Code with NO tools (verified: it can't read a file), no MCP,
  no project settings and a minimal environment. Model replies are posted as plain text with
  links removed. Action confirmations are always code-written.
- **4. Failures that skipped the fallback.** A missing CLI, an unknown PM_BRAIN, non-string
  actions and history errors all fall back to the rules.

Deferred:
- **5. Killing the CLI's descendants on timeout.** lifecycle.communicate kills the direct child.
- **6. A merge already in flight when "stop" lands may complete** (documented).

## GPT review 7 (ownership, follow-me, agent context, side panel)

Adopted, with tests:
- **Any local account could invite @pm and read the repo's issues.** Rooms must be bound to the
  repo (a moderator asked @pm for a plan or a mode) before @pm shows a board, queue or panel there.
- **project_status crossed rooms.** It answers only for the room where the agent's owner last spoke
  to it.
- **Panel and board XSS.** Issue titles and stop reasons are HTML-escaped (and brackets and stars
  neutralised) before markdown. The panel sends CSP frame-ancestors (Element only), no-store and
  nosniff, and answers only to a localhost Host.
- **Untrusted status.** Agents trust the published status only when @pm wrote it; the conversation
  shows full sender IDs.
- **Work after the owner left.** The bridge refuses chat turns and tasks without its owner in the
  room; MCP claim() checks the owner's membership before every step.
- **Anyone could invite someone else's bridge agent.** It accepts invites only from its owner or
  @pm.
- **read_project_file path handling.** The raw path is validated, every segment is a plain name and
  URL-encoded, the root endpoint is explicit, and a test proves bad paths never reach gh api.
- **Follow settings via the model, remote look-alikes, a damaged registry.** A follow change needs
  the person's own words; only local people; malformed registry entries are tolerated.

Deferred (docs/ROADMAP.md):
- Panel viewer identity through Element's OpenID hand-off.
- Per-owner pending approvals instead of one slot per room.
- Membership reconciliation at startup.
- Rejecting duplicate agent_user entries.

## GPT review 8 (tools with owner approval)

Adopted, with tests:
- **1, 2. SSRF.** safe_fetch.py resolves each hop once, requires every address to be public
  unicast (IPv4-mapped normalised; multicast, reserved and unspecified refused), connects to that
  exact IP (Host/SNI/cert keep the name), uses no proxies, and has a 25 s overall deadline.
- **4. Planted grants republished.** One record per agent (state key `agent:<id>`; "@" keys are
  reserved by Matrix). @pm reads back only records it wrote itself.
- **5. Borrowed room.** owners_room() needs an explicit @mention from the owner in the last
  15 min, and refuses when two rooms qualify.
- **6. Ambiguous consent.** A grant must be a direct instruction (not a question or quote); the
  owner must match the registry exactly; a model revoke also needs the person's words.
- **7. Approval scope.** The full request is shown (max 1000 chars, or refused), a denial wins, the
  owner can't be the agent, and approved results carry the request text.
- **8. Blocking.** The web tools and ask_owner are async (to_thread / asyncio.sleep).
- Also fixed live: "@" state keys were refused by Synapse, and background errors are now logged
  with tracebacks.

Partly adopted:
- **3. Exfiltration through web requests.** Every web request is posted in the room (visible
  audit), the grant message warns, and web is never on by default. Full fix (a research worker
  with no project or room context) is on the roadmap.

## GPT review 9 (the console)

Adopted, with tests:
- **Widget identity swap.** The page talks only to Element's exact origin (injected by the
  server) and its parent frame, only when embedded. It accepts only the reply to its own OpenID
  request, once, and never echoes credentials.
- **"off" bound an unbound room.** Stopping never binds; binding happens only through
  write_level after a moderator asked. Agents and bots can't use the console (is_person).
- **/data without a session.** Removed; the board comes from the signed-in, membership-checked
  state route.
- **The DM carrying the link.** It is reused only while @pm and the person are its sole members,
  and is created as private_chat (no elevated rights). A link's session is scoped to its room.
- **Login code in the URL query.** It is now after "#", removed before any request, and the page
  sends no-referrer and no-store.
- **Approval detached from what was seen.** The button carries the waiting event's ID, and the
  server checks it.
- **Origin boundary.** Every route checks the host; POSTs need JSON from our origin; "Bearer"
  required.
- **OpenID verification.** No redirects, an exact @name:server match, fail closed on odd
  responses.

Deferred:
- Remove inline script and unsafe-inline (board HTML is already escaped server-side).
- Encrypted DM transport for the link (@pm's Matrix client has no E2EE yet).

## GPT review 10 (one room, one repo — 9e45126)

GPT would have blocked the commit. Adopted (65892f9):
- **A room creator could bind any allowed repo.** Anyone can create and own a room, so the
  server allowlist alone exposed every allowed repo. Binding now also needs the requester's own
  agent's GitHub account to be able to write there. The answer is the same for "not allowed" and
  "no access", so it never confirms a repo name.
- **Hand-written state.** A room creator controls power levels, so anyone they choose could write
  the binding by hand. @pm and the agents now trust the binding (repo and mode) only if @pm wrote
  it.
- **A plan filed into another repo.** Plans carry their repo, and a repo change clears waiting
  plans. Results and polling only touch the room's current repo's items.
- **`None == None` passed the task guards.** A room with no repo now refuses every task.
- **Bugs.** Hermes's issue filing and both review paths lost their repo argument (TypeError), and
  "All done" crashed on the new keys. The tests' mocks had the old signatures, which is why these
  slipped through; the mocks now assert the repo.
- **Bridge.** The in-flight dedup is keyed by room and repo, and the old repo's project copy is
  removed after a switch.
- **Console.** The repo action shows @pm's answer. Readiness checks the default branch, not
  `main`, and shows no raw CLI errors. Chat takes the repo from the command's own argument.

Rejected:
- Migrating old state by historical repo. The old code only ever ran on `LOOP_REPO`, and changing
  it between runs had the same effect before this change.

Deferred (as before, GPT 8 #5):
- Hermes still infers its room from recent mentions, because the MCP call doesn't say which room
  it came from.

Found in the browser, not by any review: an apostrophe in a button label broke the console's
whole script (fbb4a41). `node --check` now runs on the page script in the tests.

## GPT review 12 (the site and the Oracle deploy kit — faf34a2)

No XSS from visitors and no exposed admin or internal ports. Adopted:
- **`.env` was shell code.** `envfile.py` now reads it as data and validates every value:
  domains, the e-mail, pinned versions (no `latest`) and a hex password. The file is created
  mode 600 and secrets are filled without a command line.
- **An interrupted first run could leave Synapse on SQLite.** The config swap is atomic and keeps
  the file's owner. A "configured" marker gates the start, the script runs safely every time, and
  a server-name mismatch is refused.
- **No admin or invite path.** `invite.sh admin` and `invite.sh token N` use the internal admin
  API only. Bootstrap checks that sign-up without a token is refused.
- **The site folder could fall outside Caddy's mount.** The layout is now fixed
  (`~/hagency/{deploy,site}`) and checked.
- **iptables vs Docker.** The host-iptables edits and their persistence are gone; OCI's security
  list is the firewall.
- **Success claimed too early.** Bootstrap now runs `compose config` and `pull`, validates and
  reloads Caddy, and checks HTTPS content on every endpoint. Postgres sits on an internal network.
- **Site claims.** The trust rules are scoped to agents registered on the server, "stop" means
  new work and merges, and the web-fetch wording is narrower. The preview is "by invitation",
  the hero room is labelled an example, there are no copy buttons for unpublished commands, and
  the unclaimed AUR package name was removed because someone else could squat it.

Self-caught while fixing: `curl -f` would have hidden the 401 body the invite-only check reads.

## GPT review 13: approvals, retry, Hermes verify (70db407..4a3377f)

Adopted:
- **Talk read as consent.** "good for you", "let us approve", "yes?" and "ok 👎" no longer count.
  A question is never a yes, and a bare yes may only end in spaces, "." or "!".
- **The model path** now needs the person's own approving words, even in a reply to the question.
- **"do not go autonomous"** no longer classifies as autonomous (or assist).
- **A stale mode question** is cancelled by any explicit mode change (the "mode" kind was missed).
- **Retry** refuses while the paused item's PR is still open, so there is never a second PR for
  one issue. "retry 9" is read as #9, and a number it can't read gets a question instead of
  a guess. It reads the room's repo when the cache is empty.
- **Verify is bounded.** File sizes are checked before reading, headers and placeholders count,
  and there are at most 500 files. Symlinks were already fixed in 4a3377f.
- **The last direct stop** (GitHub not counting an approval) goes through `stop`, with retry help.

Deferred: a close task whose dispatch @pm already timed out can still run, because liveness comes
from recent events. Close also doesn't re-check that the issue text is unchanged since the
verdict. Both need task expiry in mcp_core; see ROADMAP.

## GPT review 14: voice and settings phrasing (2cd9d2b..d91d271)

Adopted:
- **Spoken words never act by themselves.** A transcript may hold someone else's voice or a
  misheard word, so a spoken "ok" to a waiting question gets "click 👍", and spoken mode or
  settings changes are asked with a 👍. Spoken repo names and issue numbers must be typed, and
  spoken "!" commands are ignored.
- **Loose wording only asks.** "allow Betty to quote 'let my agent search the web'" or a
  question matched the order-free web rules. Only the plain forms apply at once ("let my agent
  search the web here", "enable web search for my agent"). Anything else gets a 👍 question.
- **One question at a time.** A settings question no longer replaces a waiting plan or mode
  question.
- **Voice resources.** One transcription at a time and at most 3 queued. The download times out
  after 30 s, and decoding stops after 5 min whatever the event claims. The worker gets a
  minimal environment and its own process group, killed on timeout or cancel. Transcripts are
  capped, and every failure is said in the room.
- **Slow input.** Rules read at most 1000 characters, and the grant rule no longer backtracks on
  trailing spaces.

Deferred: idempotency of a voice job across a crash mid-transcription (it is then lost, not
repeated). A confirmed question is marked "approved" before its change runs, so a failed write
leaves it used up: ask again.

## GPT review 15: plain answers, voice per listener, owner-only note (2f5bcb4..7113832)

GPT found no owner-check bypass: typed and spoken requests both reach `should_ignore` with the
real Matrix sender. Adopted:
- **Transcribing before checking.** A bridge now transcribes a voice message only if it could
  act on it: from its owner, or a reply to it or a DM. Other people's clips are never
  transcribed on the owner's computer.
- **Names count only at the start.** "PM, tell Betty's agent to stop" goes to @pm only.
  "PM-related" isn't "PM,", and a typed `betty-agent` needs a left boundary and is ignored
  inside the quoted part of a reply. Edits are never new questions.
- **Thread fallbacks** don't count as replies, and malformed reply data no longer crashes the job.
- **Thread questions are answered in their thread.**
- **The refusal note** is "I only take requests from Betty." (no file-access detail, no display
  name). A failed send can't stop the sync loop, and the rate-limit table stays small. Voice
  errors show only our own wording.
- **test_e2e** expects the refusal note instead of silence.

Deferred: web notices and grants still infer the room and question from the owner's recent
@mention (mcp_context.owners_room). Binding them to the event that started the turn needs
Hermes to tell the MCP server which message it is answering. Room history still enters the
model prompt as context; that is a prompt-injection risk the prompt only labels. Event
de-duplication lasts one sync batch.

### GPT reviews 16–18 (2026-11-20): the public repo, and changing your agent's own code

**16, the repository as published.** No secrets in the files or history, and the test workflow is
safe for pull requests from forks. Fixed: CI actions pinned to commit SHAs, README commands relative
to a clone, CONTRIBUTING's clone line, LICENSE name, agent names escaped on the status board, board
text can't carry raw HTML into the console, and #N links to the room's repo there. Left for the
owner to decide: the docs name the real GitHub accounts behind each agent and a demo run log.

**17, the customize feature (blocked rollout).** "Use it myself" ran agent-written code as this
computer's account, which on a shared computer holds every owner's passwords and GitHub logins.
Fixed: it is offered only where the owner is the only person whose agent runs here (operator
opt-in HAGENCY_CUSTOM_SHARED_OK=1); one folder per version; tests with no environment, killed as a
process group, zero tests is a failure; git names its own metadata, links/.gitattributes/.pth are
refused; buttons work once, in their own private room only; ↩️ and auto-revert undo only their
own version; HAGENCY_HOME for custom bridges; remote users can't use the console's go-back.

**18, re-check.** Confirmed fixed or partly fixed; also fixed: a PR must be the owner's own change
(not an official commit), restricted rooms aren't private, old-layout copies are refused clearly.

Deferred: real isolation (a separate OS user or container) is what would make "Use it myself"
safe on a shared computer; until then it's off there. Rebase/discard still run git in the
editable tree; existing trial/live checkouts find metadata through their own .git. Shared JSON
state has no cross-process lock. A failed confirmation message can leave a used button active.
Pruning keeps the chosen and previous version; a third quick switch could remove one still draining.
Test stderr is buffered in memory.

### GPT review 20 (2026-11-20): Hermes changing its own code, and the Hermes invite patch

Range origin/master..HEAD (eb7fa8c Hermes code-change tools, 0ceaec5 invite allowlist patch).

Fixed (each with a test):
- **The invite allowlist had a second way in (blocking).** Hermes also joins rooms left in
  `rooms.invite` after each sync (`_join_pending_invites`), never through `_on_invite`. Both paths
  now share one check, and the reconciler reads the inviter from our own `m.room.member` invite in
  `invite_state`; an unknown inviter fails a restrictive list. The patch file was regenerated.
- **A withdrawn opt-in kept working.** Unsetting HAGENCY_CUSTOM_SHARED_OK left the old value in the
  Hermes profile, and a version chosen while it was set kept launching. `set_repos` now removes it,
  and `code_dir` re-checks `why_not_use` at every launch (official code until it's given again).
- **The button helper joined invites too.** Logged in as the Hermes agent, it could join a DM before
  Hermes saw the invite, so Hermes never recorded the DM. In `--buttons-only` mode it no longer
  handles invites.
- **Paths:** reserved names (.git, custom/, .gitattributes, .pth) are checked again after resolving,
  so a symlink in the copy can't reach .git. Reads stop at MAX_READ instead of loading the whole file.
  An empty request is refused instead of crashing the commit message.
- The Hermes tool and profile modules (hagency_mcp.py, mcp_core.py, mcp_custom.py,
  hermes_profile.py) now get the sensitive-file red flag.
- Re-check of those fixes: Hermes re-saves a profile's `args` as a block list, which
  `set_code_dir` didn't match, so a withdrawn opt-in or "go back" could leave Hermes on the custom
  tools. Both forms are matched now, and a profile with no hagency args line is reported by the
  supervisor instead of passing silently. Reserved names are compared lower-cased (this Mac's disk
  ignores case: `.GIT` is `.git`).

Deferred: the MCP tools trust that the turn came from the owner (Hermes answers only
MATRIX_ALLOWED_USERS, and nothing goes live without the owner's 👍 in the private room; a change
ID bound to the owner's event would be stronger). Isolation for running a chosen version on a shared
computer (as in 17). TOCTOU/hard-link races from another local process, cross-process locking of
the offer and `_change` state, a commit stranded when publishing the offer fails, unchecked
reaction sends, and private_room ignoring pending invites. A DM race inside Hermes (from our DM
patch, not the invite patch): its sync reconciler can join a DM invite before `_on_invite` records
`m.direct`, so that DM needs an @mention until Hermes refreshes its DM cache.

### GPT review 21 (2026-11-20): a newcomer gets an agent from chat (welcome, bring your own)

Range origin/master..HEAD (c25d5d2 welcome + pairing + connect.py). 15 findings; each verified in the code.

Fixed (each with a test):
- **A same-named person from another server could get our agent's password (high).** "@pm I want
  an agent" from `@newbie1:elsewhere` matched our remote `newbie1` by localpart. Offering and
  provisioning now require a person on this server (full Matrix ID), checked in both places.
- **The code went to the remembered chat (high).** It is now sent to `private_room()` re-checked at
  that moment (still just @pm and the person, or a fresh private chat).
- **The console URL went into a shell line unchecked.** HAGENCY_PUBLIC_CONSOLE must be
  `https://host[:port]` (http only for localhost) before it's put in the install line or connect.sh.
- **Concurrent set-ups.** One bring-your-own job per person at a time; `.env` and agents.json temp
  files are unique per process and thread.
- **Cross-site POSTs could burn the attempt budget.** /connect refuses non-JSON and any request
  with an Origin header (browsers) before the limiter counts anything.
- **The code was used up before the client was ready.** connect.py picks the CLI first; a missing
  CLI no longer costs the code.
- **Test output.** The live test no longer prints the install line or /connect's answer.
- **Joins while @pm was down were never welcomed; a failed greeting was remembered anyway.** @pm
  welcomes current Welcome members without a welcome at each start, and records one only after it
  was sent.
- **A saved password that never registered poisoned retries.** `ensure_user(verify=True)` (used for
  agents @pm creates) signs in to check and registers again if it can't.
- **People with a server-run default agent (a registry entry without "agent") could be switched to
  remote.** Refused now.
- **Welcome text could swallow project requests.** "@pm I want an agent" must be the whole
  message ("@pm I need an agent to review #7" goes on to the usual chat), and "I don't want to
  bring my own" isn't a choice.
- **Credential file permissions.** ~/.hagency is chmod 700 whatever the umask; the file is written
  via a new mkstemp file (0600) and swapped in; connect.sh sets umask 077.
- **Python version.** Markdown 3.11.3 needs Python 3.11: connect.sh now looks for python3.13 … 3.11
  before python3 (macOS's own python3 is often older).

Deferred, with reasons:
- **The public deploy can't serve it yet (high).** True: deploy/ runs no @pm or console and Caddy has
  no /connect route; the house agent and the public server are the next step. configure_synapse.py
  already sets the Welcome room (@pm must exist before the first sign-up).
- **Installer runs an unpinned clone.** curl over https from the server, then a git clone of the
  public repo: pinning a signed release comes with the public server.
- **Limiter identity behind a proxy / counters reset on restart.** Local dev isn't proxied; a code has
  ~49 bits and lives 30 minutes, so the limiter is defence in depth. Trusted X-Forwarded-For comes
  with the deploy.
- **Re-pairing returns the same password; the code is in chat history and shell history.** Rotating
  the agent's password on every redeem needs an admin token in @pm; next step, with revocation.
- **Remote agents take @pm's loop tasks.** By design, as for agents run here: only @pm's tasks,
  only with the owner in the room. The supervisor reads agents.json at start; a person never moves
  from a server-run agent to remote (refused), so there's no transition to reconcile.
- **Multiple console workers.** There is one process (@pm's); codes are atomic within it.

Re-check of those fixes (GPT, HEAD~1..HEAD), fixed with tests:
- The code's chat was approved from @pm's cached membership: `only_us()` now asks the server for the
  room's members (joined or invited) right before the code is sent; anyone else there and no code.
- Set-ups for different people raced on .env/agents.json: one set-up at a time in @pm (a lock).
  (onboard.py run by hand at the same moment is still a separate process: deferred.)
- `--agent` or a saved choice skipped the "is it installed" check; a damaged credentials file blocked
  a fresh code; connect.py no longer chmods an existing folder (it warns), connect.sh sets umask 077
  before creating anything and rebuilds a venv made with an older Python.
- URL check uses fullmatch (no trailing newline); an empty Origin header counts as a browser.

### GPT review 22 (2026-11-20): the public VM runs the shared side (and Synapse Admin)

Range origin/master..HEAD (0023615 deploy: @pm, console, /connect behind Caddy; pinned images;
deploy.sh from GitHub only; guard.py). Findings, each checked in the code; fixed with tests:
- **Root wrote through a path @pm controls (high).** bootstrap opened a predictable `.tmp` as root
  inside @pm's writable tree. The secret now goes to a root-owned `deploy/pm-secret/` mounted
  read-only, written by `hostfiles.replace` (mkstemp, O_EXCL, rename). configure_synapse.py's config
  and marker in Synapse's writable folder go through the same function (a planted link is never
  followed: test_host_tools).
- **Element's config was 0600 under umask 077 (high).** Element reads it as nginx: configs are
  chmod 0644 explicitly, and element/synapse-admin are recreated so the new file is mounted.
- **A compromised @pm could persist across deploys (high).** `release.py` clears ~/hagency/pm to the
  commit's files plus @pm's named state before unpacking: dropped files, planted modules (e.g. a
  `json.py`), links at tracked paths and the virtualenv all go (rebuilt from requirements.txt).
- **The VM checked `origin/master` (medium).** It now fetches the GitHub URL itself and checks
  FETCH_HEAD; `git status` failing is a failure; a deploy lock (flock); `.deployed` only after
  bootstrap succeeds; @pm's stop failing stops the deploy.
- **Synapse kept its old config; Caddy validated after `up` (medium).** Synapse restarts when
  homeserver.yaml's hash changed; the Caddyfile is validated in a throwaway container first.
- **Any container on `edge` could spoof X-Forwarded-For (medium).** The header counts only on a
  connection from Caddy's own address (HAGENCY_TRUST_PROXY=caddy, resolved).
- **Missing traffic figures cleared an active pause (medium).** The flag records its month; no
  figure keeps this month's pause (and warns); an interface vnstat doesn't watch is an error.
- **Origin compared by hostname only (low).** Exact origins now (scheme, host, port).
- **Shell:** `apt-get update && install` split (set -e), values read before use, `--end-of-options`,
  `ssh --`. **Tests:** deploy.sh refuses a real commit that isn't on master (a dangling commit).
- **Oracle's free A1 allowance (high, verified on Oracle's page):** now 1,500 OCPU-h / 9,000 GB-h a
  month = 2 OCPU / 12 GB. The VM is 4 / 24: the owner's call (resize to 2 / 12); documented.

Added at the owner's request: Synapse Admin at admin.<domain> behind Cloudflare Access. Caddy takes
only Cloudflare's addresses with a Cf-Access-Jwt-Assertion header (403 otherwise); /_synapse/admin is
404 on matrix.* and rooms.*. `@owner` is made with a random password kept nowhere; the owner sets
one with `invite.sh owner-password` (typed in their own ssh terminal).

Deferred, with reasons:
- **Verifying the Access JWT's signature.** Not in stock Caddy; it needs a plugin build or a
  forward_auth verifier, plus the Access team domain and AUD tag. Access checks it; the IP filter
  stops bypasses. Next step once the owner shares the AUD tag.
- **@pm holds the registration secret and admin-bot's password.** It creates people's agents and
  invite tokens; moving those to a separate admin service is the next hardening step.
- **The invite token and an agent's password leave the VM.** By design: the operator hands tokens
  out, and /connect gives a person their own agent's sign-in (GPT review 21).
- **Guard is daily and advisory;** media retention bounds age, not total size. Enough at today's
  scale (42 GB free, nothing near 1 TB); a tighter loop comes with real traffic.
- **The global wrong-code limit can block real redemptions under attack.** Unchanged from review 21.
- **Migration from the old ~/hagency/deploy layout.** The VM was fresh; nothing to migrate.

Re-check of those fixes (GPT, HEAD~1..HEAD), fixed with tests:
- Mode and owner are set on the open file (fchmod/fchown), and a linked homeserver.yaml is refused,
  so root never reads or writes through a name a container swapped.
- `.deployed` lives in ~/hagency, outside @pm's folder; settings are checked (and new ones get their
  defaults, e.g. ADMIN_DOMAIN) before @pm stops; commits older than release.py are refused.
- release.py drops links or wrong types at state names and refuses a commit with files there.
- Synapse restarts when its config differs from the one it last started with (a failed run too).
- `ensure owner` fails if @owner exists but isn't an admin. An empty Origin is refused. Last
  month's media pause is cleared before measuring. The direct-to-VM probe sends an Access header to
  the admin API path, so a 403 proves the address filter.
Deferred: JWT signature checking (as above); the owner's password is set through @pm's container
(a compromised @pm could see it; a separate admin tool is the next step); the admin host's media is
not paused (it's behind Access, owner only).

Live deploy and the whole-batch review (GPT, 707dd84..HEAD):
- First live deploy: Oracle's `ubuntu` is uid 1001, so @pm (hardcoded 1000) couldn't read its
  0700 folder. PM_UID is now recorded in .env (envfile.py) and used by compose.
- GPT on that fix, adopted with tests: bootstrap refuses to run as root and refuses a PM_UID that
  isn't the deploy user's; compose reads PM_UID only from .env (`env -u PM_UID`); PM_UID 0 is
  invalid; deploy.sh refuses commits older than the PM_UID layout (a rollback that couldn't start @pm).
- Live: certificates for rooms and matrix issued; the admin host's HTTP-01 is blocked by Cloudflare
  Access until the owner bypasses /.well-known/acme-challenge/ (see DEPLOY_ORACLE.md).
- Live: the second deploy's `ensure owner` got 429 on admin-bot's sign-in. @pm, admin-bot and new
  agents all sign in from @pm's container address, and Synapse's default per-address login limit
  (burst 5, then one per ~5 min) ran out. `rc_login.address` is now burst 20, then one per 50 s (GPT: 0.1/s would allow 360 sprayed guesses an hour per address; 0.02/s is 72); the per-account
  and failed-attempt limits stay Synapse's defaults.
- Live: `.deployed` was never written: bootstrap's `docker compose exec` inherited the ssh session's
  stdin, which is deploy.sh's remote script, and read the rest of it. bootstrap now gets /dev/null.

### GPT review 23 (2026-11-20): the Welcome pointer, and people from other servers

Welcome UX (GPT, origin/master..HEAD), adopted with tests:
- The Welcome pointer ("accept it under Invites") was marked sent before sending: a failed send or
  a restart in between lost it for good. Now saved only once sent; retried at the person's next
  join or @pm's next start; never for someone who already clicked a button.
- A reused private chat kept its old "Hagency (private)" name: renamed on reuse.
- Re-check: "I want an agent" re-greets, which dropped "pointed" and could post the line twice: kept.
Rejected or deferred:
- The pointer is an m.notice, which Matrix's default rules don't notify for: the newcomer is looking
  at Welcome (their only room) when it arrives, and the pill still shows; not worth a new send path.
- A crash after the server accepts the pointer but before "pointed" is saved can repeat it once:
  one repeated line is better than a lost one.

People from other servers, ownership by full Matrix ID (GPT, the feature commit), adopted with tests:
- Codes and console links go to @pm's private chats, where the person could invite someone later
  who'd read them back: @pm's new private chats are invite-by-@pm-only and history "joined".
- An agent claimed by two records (an operator's agent_user) went to the first match: now it is
  nobody's. /connect, follow-me removal (kick) and planning check the agent is the asker's own.
- The hourly cap counted only finished set-ups, so a server refusing the agent's chat invite made
  unlimited accounts: a remote person's entry (and its "created") is saved before registering.
- "is_direct" is only the sender's word: after joining, @pm leaves a chat that isn't just the two
  of them. One person can't use the hour's joins (3 each, 30 in all).
- HAGENCY_ALLOW_REMOTE=0 now also stops new codes and sign-ins for other servers' people.
- /connect refuses a remote record whose account isn't the derived name (connect.py would refuse it
  after the code was spent). Half-migrated registry entries are merged, not replaced.
Rejected or deferred:
- The person's own homeserver can read the code sent to them: it is their server, their trust.
- Registry read-modify-write races between @pm and onboard.py (the CLI): pre-existing, needs a
  file lock across processes; deferred.
- "@pm I want an agent" in a shared room isn't counted by the join limit: that person is already in
  a room with @pm, and @pm reuses its chat with them.
- A pre-upgrade code stays valid for its 30 minutes after a new one is issued; checking the saved
  homeserver URL again when reading one's own 0600 credentials file; an owner whose derived name
  collides with another's (needs a 40-bit hash collision; refused at set-up anyway).
- Console sign-in inside Element for other servers' people (their server's OpenID token): it means
  fetching a URL their server names; they use "@pm send me my console link" (ADR 0003).

Whole batch (GPT, origin/master..HEAD), adopted with tests:
- History "joined" hid @pm's own offer from a newcomer (sent while they were invited): "invited".
- Older @pm chats (no safeguards) are no longer reused for codes and links: a new chat is made.
- @pm leaves a chat someone from another server opened, after answering: it could grow later.
- Follow-me invited an owner's agent into @pm's private chat: @pm's chats are skipped.
- /connect returns a bare name for people here, as an older connect.py expects.
- Set-up refuses an agent name claimed twice or overridden (one /connect would then refuse).
Rejected or deferred:
- A reservation that failed registration is exempt from later counts: it was counted once, and
  it can only ever make that one person's account.
- Registry races with follow-setting writes; the 30-minute pre-upgrade code: as above.
- Re-check: follow-me skipped only the current chat, so an old one replaced by a new chat could
  still pull the agent in: any two-person chat named as @pm's chats (now or before) is skipped.
  A failed leave is logged; a failing membership check still ends in leaving.

@pm moderates new rooms, the panel comes first, no welcome for admins (GPT, the two commits),
adopted with tests:
- Co-creators are in creation_content.additional_creators, not a top-level field: read there
  (Synapse refuses a creator in "users"). Requests the module doesn't expect (a null or odd
  override, non-list invites, power levels in initial_state) are left exactly as they came.
- The dev server's Synapse is recreated (`compose up`) so it gets the module mount before restart.
- The panel's moderator check is room_state.is_moderator (v12 creators count).
- Admin status is remembered for a day, not forever.
Rejected or deferred:
- Rolling back past this commit leaves the `modules:` line with no /modules mount: a rollback
  must drop that line (noted here; rollbacks are by commit and rare).
- The widget layout write replaces other widgets' placements and isn't retried on failure;
  replacing a pre-existing multi-line `modules:` block; `.synapse-loaded` written before Synapse
  is healthy: all pre-existing or not present on this server's generated config.

Background agent, offline notice, Disconnect (GPT, the feature commit), adopted with tests:
- A code redeemed during a Disconnect could pick up the new password: codes are revoked first (on
  @pm's own thread, no SavedDict race), and /connect reads .env under the set-up lock.
- Disconnect when admin-bot can't sign in left .env ahead of Synapse: treated as a refusal, and
  only that one line is put back, on a fresh read.
- systemd expands % and $ inside quotes: doubled in the unit, so a federated owner ID arrives as sent.
- daemon-reload / restart failures are errors, not silence. A presence timeout or network error
  never stops @pm's tick. The panel says when Disconnect failed.
Rejected or deferred:
- `connect.py --stop` reports removal even if launchctl/systemctl couldn't stop a running process
  (it then stops at the next login); the 🔁 button isn't retried if its reaction send fails (the
  message still says what to run, and "bring my own" works).

Outage (2026-11-21, ~00:13–00:33 UTC) and its fix (GPT, 22b502e..HEAD), adopted with tests:
- Several deploys in an hour used Synapse's per-account sign-in burst (3) for @pm, which signed in
  three times per start. setup_accounts read the 429 as a wrong password, tried to re-create @pm
  (M_USER_IN_USE) and exited; Docker restarted it, and it signed in again: a loop. Now 429s are
  waited out (retry_after_ms, up to 15 minutes), ensure_pm needs no sign-in when @pm exists with
  its name, and a limit that outlasts the wait is an error, never "wrong password" (GPT: the
  first fix still fell through to re-registering after the wait).
Deferred: matrix-nio's own unlimited 429 retry inside client.login makes lifecycle.login's cap
  soft; set-up holds WRITING while a remote agent's sign-in waits, delaying /connect meanwhile.

Connect page and the Windows installer (GPT, the feature commit), adopted with tests:
- Two keepers could run one agent (a Startup entry beside a task, a task left under an old name):
  connect_win.py holds agent.lock, ties the bridge and its CLI to itself with a Windows job, and
  connect.ps1 removes earlier tasks and Startup entries of this installation first.
- Stopping the old agent could hit an unrelated process with a reused PID: only a command line
  naming this installation's connect_win.py or bridge.py, killed with its children.
- The ACL could protect another folder than the one holding the sign-in: one absolute folder
  (HAGENCY_DIR honoured), passed to connect.py and the task; `icacls /reset` first, so no other
  grants survive.
- An npm `codex.cmd` passed the CLI check but the bridge (no shell) can't start it: only a real
  `.exe` counts, with a plain hint. A clean bridge exit (Disconnect) now stays stopped, as with
  launchd/systemd; logs go to ~/.hagency/logs/ like agent_service.py.
- `irm -UseBasicParsing` in the line shown (Windows PowerShell 5.1); native stderr no longer
  stops the installer before its exit code is checked; a changed fragment refreshes the code box.
Rejected or deferred:
- @pm still sends the raw curl line: pm_welcome.py/remote_agent.py belong to the other change;
  `connect_page.page_link(code)` is ready for it.
- The code in shell history / process arguments: it works once, for 30 minutes, and is used
  within seconds, as with connect.sh. TLS 1.2 before the first `irm`: Windows 10/11 default to it.
- X11/BSD shown as Linux, and the installer reporting failure only on screen (it must not close
  the person's window): the tabs and the red message cover them.

Connect page and the Windows installer (GPT re-check), adopted with tests:
- connect.py's check mark crashed Python's piped output on a cp1252 PC after the code was used:
  PYTHONIOENCODING=utf-8 for the installer's Python calls (restored after) and for the bridge.
- `icacls /reset` briefly let the folder's contents inherit the parent's access: the folder's
  whole access list is replaced first (only you), then its contents reset to inherit just that.
- -RestartCount 255 (Task Scheduler's maximum). Old tasks and Startup entries are matched by
  this installation's keeper AND sign-in folder, case-insensitively; a PID is stopped only if it
  is a Python running our keeper or bridge. A refused Windows job is logged, not fatal.
- (third pass) Set-Acl also writes the audit list, which needs admin on a rerun: the folder's
  DACL is set with SetAccessControl. `--home "<folder>"` is matched whole, so ~/.hagency never
  matches ~/.hagency-test. A log that can't be written never stops the agent from starting.
Rejected or deferred:
- A CLI reachable only through a PowerShell profile's PATH: the hint says it must be on the PATH.
- README describes the link @pm will send: it lands with the @pm change (PR notes).

Connect page, Windows service case and @pm's link (GPT, whole branch after the rebase), adopted with tests:
- `connect.py --stop` on Windows ended any Python with a stale PID: only a command line naming this
  installation's connect_win.py or bridge.py counts, and the keeper removes agent.pid when it ends.
- A failed rerun (expired code, no network) left the old agent stopped: its tasks start again.
- A bridge log that can't be opened no longer keeps the agent from running.
Rejected or deferred:
- Two sign-in folders with the same agent name on one PC share a task name (not a real setup).
- Pairing with connect.py directly on Windows (not through connect.ps1) relies on the profile
  folder's own private ACL; Startup resolved from %APPDATA% (a redirected Startup folder is rare);
  stop doesn't report schtasks/taskkill failures.
- (re-check) The PID check now reads the process's own program (a Python) and its command line
  separately, so text in another program's arguments can't pass; a vanished agent.pid is no error;
  it is kept while one of ours survived. A failed rerun also relaunches a Startup-folder agent and
  says honestly whether anything came back; that state lives in a local hashtable, never in the
  person's own session scope.

### GPT review 24 (2026-11-20): several agents per person (step 4)
Adopted, with tests (test_more_agents, test_more_agents_flows, test_routing):
- A failed reservation could authorize a password reset: if the derived name already existed with
  someone else's password, the claim stayed and "disconnect" asked Synapse to reset that account,
  whose new password the claimant could then fetch. Disconnect now only resets an account whose
  password this server already holds (one it created or verified).
- A code taken just before a disconnect waited for the lock and then returned the NEW password:
  each disconnect is recorded per agent and a code issued before it is refused.
- Damaged agents.json fields (a list as a label, an object as agent_user) could break ownership
  lookups for everyone: only well-formed strings count; labels use fullmatch ("codex\n" was
  accepted by `match`); a pairing code with a damaged agent field is refused, never read as the
  first agent's; `registry.update` with an unknown agent raises instead of writing the first's.
- The cross-review rule held only when the checker was picked: review and verify results are now
  accepted only from an agent of a DIFFERENT person than the builder's (owner full IDs).
- @pm's own read-modify-writes of agents.json (its loop and set-up threads) share one lock; the
  hourly cap for other servers no longer counts this server's people's further agents.
- ➕ under a message in a project room asked for the name there, but answers are only taken in
  the private chat: the question goes to the private chat. A set-up that failed can be finished
  by asking for the same name again, even at the cap; the cap message no longer suggests
  Disconnect (it doesn't free a slot). A typed name must be exactly a valid name ("co-dex" is
  refused, never silently "codex").
- The console's agent field: an unknown agent fell back to the first agent for web research, and
  refused changes said "ok": a named agent must be theirs, and refusals are reported.
- Caddy didn't route /panel-agents.js to @pm (the panel would have had no "My agents").
GPT re-check of those fixes, adopted with tests:
- ensure_user saves a password BEFORE registering, so "we hold a password" wasn't proof: an agent is
  marked ready once its account was made or signed into with our password; an older record without
  the mark is checked by signing in with the saved password before any reset.
- A recorded agent_user must be a localpart; `registry.update(person, "")` is no longer "the first".
- A further agent that exists already ("add another agent called codex" again): its Connect page is
  sent again (it may have been made but its message failed), never refused.
- ➕ in our existing private chat asks there, never in a second chat.
- connect.py's fallback for a missing saved CLI is reverted: re-pairing the first agent silently
  changed its backend. `--agent` chooses.
Live test finding, fixed with a test and GPT-reviewed: after Disconnect the bridge re-synced its
dead sign-in ~45 times a second (pre-existing): it now stops cleanly on M_UNKNOWN_TOKEN, except a
soft logout (GPT). Deferred from that review: a dead token on the very first sync after a restart
drops the hand-off (each start signs in afresh, so it isn't reached); a hand-off that can't be
written makes the exit non-zero (Windows' keeper then retries); @pm's container restarts after any
exit and signs in again with its password (wanted).
Rejected after checking: "codex-" being taken as "codex" (the regexes capture the hyphen and the
exact-name check refuses it; tested).
Rejected or deferred:
- A cross-process lock with onboard.py (an operator's command, run by hand while @pm runs): deferred.
- An older connect.py refuses a further agent's sign-in after using the code: connect.sh always
  updates the app first, so only a hand-run old copy is affected (it says so; ask for a new code).
- Re-checking a stored checker before each re-dispatch: results are what count, and they are
  checked; assignments made before this change had no further agents.

Owner CLI powers, work folders, answers without @ (GPT, two passes over the branch), adopted with tests:
- Hermes gives one toolset to every turn, so @pm's loop tasks (other people's issue text) would
  have had the owner's terminal/file/web tools: the default profile obeys only its owner;
  a loop profile kept the contained tools and @pm (later, by the owner's decision: both run
  side by side on one account, see ADR 0006). Leftover
  GATEWAY_ALLOWED_USERS / *_ALLOW_ALL_USERS lines are removed from the profile's .env.
- A member's locked turn (--allow-members) resumed the owner's conversation: separate session key.
- The refusal note broke task output (final `VERDICT:` line, plan JSON): only owner turns get it.
- Locked Codex builds inherited the owner's `sandbox_workspace_write` network/extra roots: forced off.
- Owner Codex turns: an own permission profile (`profile`, `default_permissions`) is never mixed
  with our sandbox/network defaults.
- Migration: copy only on a cross-disk error, via a unique staging folder; symlinked old folders
  never adopted; absolute work root; the Claude session's own folder copied; odd state dropped.
- Hermes YAML: an empty or commented toolset list, or `{matrix: [...]}` inline, is never widened;
  inline approvals/terminal kept; the cwd is quoted.
- Errors are scrubbed before they are cut short; a truncated private key's body is cut too.
- Follow-ups: queued candidates are re-checked under the room lock; a failed or NO_REPLY turn
  (also with a progress line or refusal note) posts nothing; a member's answer never closes the
  owner's question.
Rejected or deferred:
- A follow-up turns an owner's unrelated message into a full-power turn: by design (the owner's
  own words, the owner's direction); the agent decides and says NO_REPLY.
- Under --allow-members a locked turn can still read the owner's work folder and disk read-only
  (as before; documented in README). A legacy shared session may hold member turns from an old
  --allow-members run (rare; drop .bridge-session.json to reset).
- Locked turns still load the owner's Claude user hooks and Codex project layers (pre-existing).
- Follow-up state is per room, not per thread; a voice answer without the agent's name isn't a
  follow-up; a code change leaves an open question open; Hermes replies aren't scrubbed by the
  bridge (it posts directly); the hagency MCP web tools still follow the per-room grant
  (retiring grants touches console.py/panel.py, under parallel edit).
- A symlinked destination parent, and a crash between the move and the transcript copy
  (needs local write access / a crash in a millisecond window; a missing transcript just starts fresh).

Two Hermes profiles per agent, after the rebase onto several agents per person (GPT, whole branch), adopted with tests:
- Hermes also admits users paired earlier: each profile's Matrix pairing store is cleared when it
  is written, so its allowlist alone decides (and @pm can never reach the personal profile).
- The loop profile's hagency MCP server still offered web_search/fetch_page (gated on the owner's
  room grant, not on who drove the turn): refused there (HAGENCY_LOOP_ONLY).
- A follow-up was offered when the owner named another agent in plain text ("anna-codex-agent: …"):
  not any more. A queued follow-up carries the question it was offered for and runs only while
  that same question is still open and in its window.
Rejected or deferred:
- A running supervisor started before re-onboarding doesn't start the new loop profile until it
  restarts: re-run onboard, then restart ./start.sh (in the PR notes).
- Hermes onboarding still uses `<person>-agent`, not a registry `agent_user` override (pre-existing).
- An owner's project-level Codex config disabling web search is overridden by our `-c` default;
  inherited GATEWAY_ALLOW_ALL_USERS in the supervisor's own environment (not set by us); locked
  Claude turns load user hooks; an old shared session from an --allow-members run (as before).

Onboarding: Element home page and @pm's chat opening joined (GPT, feat/onboarding), adopted with tests:
- The auto-join trusted `@<person>-agent` as "their own agent", but sign-up doesn't reserve -agent
  names: whoever registered "bob-agent" first could pull bob into rooms. Only @pm's direct invites
  are accepted now; the agent's chat stays an invite (README, test_welcome_e2e).
- Retries could join on a stale invite, and m.direct was written before the join: each try first
  checks that this very invite is still the person's membership, and m.direct is written only
  after a successful join (re-read just before writing).
- bootstrap restarted Synapse after @pm had started, so chats @pm opened in between missed the new
  module: Synapse restarts (when its config or modules changed) before @pm starts.
- Caddy served the whole generated folder: only home.html, logo.svg and background.svg now.
Rejected or deferred:
- A click on "Get your agent" before @pm's chat exists makes Element start a new chat with @pm
  (no offer in it): rare (the greeting comes seconds after sign-up); PHASE 2 makes @pm answer a
  person's own new chat with the offer. The Welcome pointer still says "accept it under Invites"
  (pm_welcome.py, another agent's file now): PHASE 2.
- Step ③ "Invite people" is guidance, not a button (no room exists yet at that point).
- m.direct can still race a client's own write (the built-in module has the same limit).
- Element's new config can reach a browser a moment before Caddy's new route (one deploy, once).
- (re-check) Adopted: a test where the invite is withdrawn between two join tries. Deferred: the
  check-then-join gap inside Synapse (a replacement invite in that instant is joined; only @pm can
  invite in its chats, so it would be @pm's own); Caddy's path matcher is case-insensitive (the
  folder only ever holds the three files hostfiles.py writes); the e2e's invite helper is the
  existing one, restored unchanged for the agent's chat.

Onboarding phase 2: lobby, project rooms, one room per repo, who can join (GPT, feat/onboarding), adopted with tests:
- One room per repo raced: the primary lookup ran before the lock and only saw rooms the poll had
  cached. The lookup, the private-repo check and the bind are now one step under the binding lock,
  and the lookup reads every room I'm in (so right after a start too). write_level reads the repo
  and runs_in in one read, so a stop can't drop runs_in.
- A private repo's room could stay open: before binding, the room's real join rule (not my record)
  is checked and the room made listed; if that fails, the repo isn't set. Visibility changes take
  the same lock.
- Join decisions could outlive the moderator and the request: the clicker must be a joined
  moderator now (asked of the server), a knock must still be that knock, someone already in or
  invited isn't acted on, and a failed invite or kick leaves the request open.
- Leaks: the conflict answer and a discussion room's messages no longer name the other room; the
  panel shows its name and link only to that room's own members. "Ask to join owner/name" answers
  first, then looks in the background (no timing difference). Listed rooms say their name and
  topic are public.
- 🙋/➕ buttons are keyed by their own message, expire after a day and are dropped once the room
  binds; ➕ that meets a new conflict gets new buttons.
- Room names are shown as text (markdown escaped, one line) in every message.
- Cooldowns are kept apart from requests (a settled request still counts), expired requests are
  pruned, all requests together are capped per hour, "find a project" reads at most 300 rooms.
- A Lobby click is used up only once the offer was sent.
- New rooms are created as room version 12 explicitly (the power override relies on it).
Rejected or deferred:
- A room set to public by hand in Element, or a repo that turns private later, isn't reconciled
  until the next bind; people already in an open room stay when it closes (that's Matrix).
- A stop racing a rebind outside the lock: one read narrows it; the loop also treats a runs_in room
  as off whatever its level says (room_state.level_of).
- A federated knock round trip and directory publication need a live stack (not run here).
- (re-check) Adopted, with tests: an unreadable room never counts as "no repo" or "no primary"
  (room_state.StateUnreadable; set_visibility and the one-room-per-repo check refuse instead); a
  join click reads the server's state once for the clicker's membership and power and the
  requester's membership and the knock's own event id (a withdrawn-and-renewed knock, a demoted
  moderator, or an unreadable room never decide); the 🙋/➕ question travels with its own reply
  (no shared slot) and isn't attached once the room bound; running work refuses before the room is
  closed; "ask to join" lookups are a few per person an hour and catch their own errors; the Lobby
  button stays usable when my offer couldn't be sent (offer_again says so); "#" is no longer
  escaped (github.linkify links issue numbers) and the console-link message escapes the room name.
  Deferred: a stop racing a rebind outside the lock (narrowed to one read; level_of keeps a
  runs_in room off regardless).

Seen live (owner): in @pm's own chat, "resend me the code" and "send me my console link" without
"@pm" got no answer. Every message in a one-to-one chat with @pm (not a project room with a repo)
is now for @pm; "resend me the code" / "new code" / "connect my agent" sends the same Connect
message and fresh code 🔌 sends (a second click on 🔌 only removes the reaction), a few an hour.
Tests: test_joining.MyPrivateChat; test_welcome_e2e types it without "@pm" and waits for a new code.
- (whole branch, GPT) Adopted, with tests: "everything is for @pm" now holds only in my private
  chats (ones I made, or a direct chat a person opened with me, recorded when I join it) with
  exactly that person in it; a fresh project room with two people never counts (plain "stop" there
  stays theirs). A discussion room's state holds only a "discussion" flag; which room runs its repo
  is kept by @pm (data/discussions.json), so its members can't read the other room's id. Every
  binding write reads and writes under one short lock (a stop can no longer write back a repo a
  rebind replaced; the poll's lock isn't used, so "stop" never waits behind GitHub). A private
  repo's room gets history readable by members only (world_readable → shared) before binding and on
  every visibility change. Visibility changes re-read the requester's membership and role under
  the lock; closing sets the join rule first, opening last. A renewed knock is a new request.
  "Find a project" runs in the background, a few per person an hour. pm_chat.py's addressing moved
  to pm_address.py (R4: pm_chat back under 300).
  Deferred: a decline kick can't be conditioned on the knock (Matrix has no compare-and-set; the
  knock's event id is re-read right before); members already in an open room stay when it closes;
  a repo turning private later and native join-rule edits aren't reconciled until the next bind or
  visibility change; the hourly code limit covers "resend" (🔌 and "bring my own" use the
  provisioning path's own limits); the pointer says the chat is open even if Synapse's auto-join
  gave up (rare; the invite is still there).
- (re-check of the whole-branch fixes, GPT) Adopted, with tests: the level a same-repo rebind keeps
  is read under the binding lock, and "stop" on an unbound room is decided there too (a rebind can't
  bring back a mode a stop just turned off); "my private chat" is decided by my own records (my
  chats, Welcome's offer, chats a person opened with me, each for that one person), never a room's
  name; a stale click settles only its own knock, never the renewed one; an opening that fails at
  the join rule takes the listing back down and records nothing.
  Rejected: migrating rooms that stored "runs_in" in state: that format never left this branch
  (nothing was deployed with it).
- (last re-check) Adopted: a network error while opening is handled like a refusal, and a listing
  that couldn't be taken back down is said ("set it to private again"). Not adopted: follow-me's
  room-name check for my chats (pre-existing, not part of this branch's addressing).

## GPT review of Connect GitHub (R15, ADR 0007)

Codex reviewed b21f5e0..46baf92 (11 points). Adopted, each verified in the code, with tests in
test_github_link_more.py:
- The code went to whatever chat the person typed in (a direct chat they made, where others could
  be invited and read back): it now goes only to @pm's own private chat (`private_room`: only @pm
  invites, history from the invite) and their own GitHub card.
- The install wait after "App not installed" replied only for listed repos (a timeout naming the
  repo, or a second answer once installed), which confirmed the name to someone without access: it
  is now quiet unless it can bind.
- Disconnect while GitHub was handing out a code didn't stop it: that code is now dropped unseen.
- A Disconnect while the access check was in flight still let it pass: the link must be unchanged
  after GitHub answers.
- The permission check skipped the App token's renew-on-401; it now goes through `as_app`.
- GitHub's own error text (stripped, but still free-form) could reach chat: only documented error
  codes are named, anything else is "unexpected".
- `slow_down` now takes GitHub's new interval when it gives one.
- A link that couldn't be written to disk stayed in memory (and a failed unlink left memory and disk
  apart): memory now matches the disk, and the person is told.
- The panel's message line held the code and outlived the sign-in; repeat clicks re-sent it to the
  chat: the line never holds the code now, and repeat clicks don't re-send it.
Rejected or deferred:
- Device-code phishing (someone else's code entered by the victim links the victim's GitHub to the
  attacker): inherent to the device flow, which this change was asked to use. Documented in ADR 0007
  with its real impact; the web flow (callback tied to the console session) is on the ROADMAP.
- Install waits keep the first requester when a second moderator picks the same repo, and a retried
  "discuss separately" choice loses `separate_from`: pre-existing install-wait behaviour (ADR 0004),
  not changed here.
- A monotonic deadline bounding the last token request: GitHub stops issuing tokens at expiry, and
  each request is bounded by the 20 s HTTP timeout; a token that arrives is valid proof.
- (re-check of the fixes, GPT) Adopted, with tests: each Connect click has its own pending marker,
  so a click cancelled by Disconnect can't take over (or clear) a newer click's; a code isn't sent if
  Disconnect came while the private chat was being opened; the bind re-checks, under the lock and
  right before it writes, that the GitHub link the write check used is still the person's; a GitHub
  error that isn't text counts as "unexpected". Rejected: the reply-timing difference between an
  unlisted repo (no GitHub call) and a listed one (pre-existing since ADR 0004; padding the reply
  time is out of scope).
## GPT review of fix/small-bundle (names, retired web grants, Windows .cmd, Remove, quiet log)

Codex reviewed master..fix/small-bundle (5 commits). Adopted, each verified in the code, with tests
in test_remove_agent, test_cli_path and test_matrix_calls:
- **1. A stale record could close a live account.** remove() now also requires the agent's
  localpart to be the derived name (as credentials_for does), so a stale `agent_user` never reaches
  the deactivate call.
- **2. An inconclusive login dropped a live agent's entry and password.** A saved password we can't
  confirm (wrong, or Synapse erroring) now changes nothing and asks to try again; only an agent with
  no password and no "ready" (never set up) is forgotten without a deactivation.
- **3. Prune raced removal.** agent_removal.prune runs registry.prune under the same WRITING lock as
  set-ups and removals.
- **4. Removing the last person brought the demo anna/betty back.** A saved `{}` stays empty; only a
  missing or damaged file means the demo people.
- **5 (part). A failed .env clean-up after the deactivation reported failure.** It is logged and the
  removal counts (the password of a closed account is harmless).
- **6. cmd-shim parsing.** Parts with a drive (":"), "." or empty are refused, the target must
  resolve inside the wrapper's folder (no link out), the wrapper's last command line counts (not a
  decoy above it), and node must be a node.exe (PATHEXT could find a node.cmd).
- **7 (part). A stale armed Remove button.** The second click counts only for the same agent within
  the 8 s window.
- **8 (part). A proxy's 404 page made "the alias doesn't exist".** Only 404 M_NOT_FOUND does; any
  other 404 is an error and no second Lobby/Welcome is created.
- **9. Unanswered removal questions piled up.** Expired ones are evicted, and a new question for the
  same agent replaces the old one.
Rejected or deferred, with reasons:
- **5 (rest). onboard.py writes agents.json/.env from another process.** Pre-existing (WRITING is
  process-local everywhere); onboard runs by hand on the server. Deferred to a file lock.
- **7 (rest). A server-side, single-use confirm token for the panel's Remove.** The console session
  is a bearer token (no cookies, so no CSRF), so a request saying "sure" is the person's own; the
  two clicks guard against a misclick, which the page does. The chat accepts every "OK" emoji
  (intents.is_confirm_reaction), as every other @pm confirmation does.
- **8 (rest). A 200 presence answer without "presence" is read as "can't say".** That is Synapse's
  answer for a deactivated account (the case seen live); the agent is skipped once the
  deactivated list is read. Its last Online/Offline stays shown until then.
- **3 (rest). An account reactivated after the snapshot.** Pruning needs the person AND every agent
  deactivated in one complete list; reactivation is a manual operator step (within 6 h).
- (re-check, GPT) Adopted, with tests: a set-up whose registration failed (password saved, no
  account) can still be removed: Synapse's admin API must say 404 M_NOT_FOUND, anything unclear keeps
  everything; pairing codes are revoked only once the removal succeeded (a refused one keeps them);
  a cmd-shim comment line, above or below, never counts: only the last non-comment line is read.

## GPT review: batch 5+6 combined (Connect GitHub + small fixes, R15)

Codex reviewed origin/master..integ/batch-5-6 (PR #5 and PR #6 merged together). Six findings, each
verified in the code and fixed with a regression test:
- **1 (P1). The public panel stopped rendering.** deploy/Caddyfile's @pm route lacked
  /panel-github.js, so Element answered it and `githubCard()` was undefined. panel.SCRIPTS is now the
  one list (script tags and routes come from it); test_panel.CardScripts requires the Caddyfile to
  route each one, and a card whose script didn't load shows a note while the rest renders.
- **2 (P1). A repo bind could commit after Disconnect GitHub or a demotion.** write_level re-checks
  the requester (joined AND a moderator, read now: room_state.joined_moderator; and the same GitHub
  link) as the last await before the state write; the install wait goes through it.
  test_github_link_more.BindRace.
- **3 (P2). Removing the last agent stranded the person.** The line stays as a tombstone
  ("first_removed", agent "remote"); a new agent gets a new (named) account name, and "bring my own"
  asks for it. test_remove_then_add.RemoveLastThenAdd.
- **4 (P2). Pruning left a deactivated person's GitHub link and sign-in.** forget_github_of drops the
  link and cancels the sign-in; finish_github saves only while its sign-in is current. An active
  person removing an agent keeps theirs. test_remove_then_add.PruneDropsGitHub.
- **5 (P2). A registry write failure.** A closed account whose record can't be saved is reported
  closed (NotForgotten) and its codes revoked; a prune that can't save is logged and retried in 10
  minutes, never crashing the poll. test_remove_then_add.SaveFails.
- **6 (P2). cli_path returned the unresolved path.** It returns the absolute path it checked.
  test_cli_path (relative PATH entry).

Re-check (GPT, the same range, focused on these fixes): seven P2 points and two lower ones. Adopted,
verified in the code, with tests in test_batch_recheck (each fails without its fix):
- the requester is checked before close_for_private too (no privacy writes for someone who left);
- Codes.revoke drops every matching code in memory before its one save; a failed save is logged;
- an older record (no "ready") closed before NotForgotten is forgotten on retry: Synapse's answer
  is read as missing / deactivated / live;
- the federation hourly cap counts removed agents' creation times (kept 24 h);
- a deactivated account's link leaves memory at once; one the disk refused is retried within 10
  minutes even after a reactivation;
- after the first agent was removed, registry.agent_of is the first live agent, and a code for the
  removed first agent never redeems another one;
- the panel shows "Add an agent" with no agents, and clicks work without panel-agents.js (Remove
  refused then); cli_path resolves the target once and makes node's path absolute.
Rejected, with reasons:
- **onboard.py on a tombstoned person.** Operator-only, by hand; it fails loudly when Synapse refuses
  the closed name. Restoring a removed account is a manual migration.
- **customize.why_not_use counting tombstone lines as "other people".** It fails closed (no "Use it
  myself" offered), as it already does for every remote person's line.
- **Refusing new GitHub sign-ins for deactivated accounts.** A deactivated account can't sign in to
  Matrix or get a console session, so it can't start one; one in flight is cancelled by the prune.
- **send_code issuing a code after a NotForgotten removal.** The account is closed, so the sign-in it
  would give can't log in; the record goes on the retry.

## GPT review of fix/home-wording (new-user screen test fixes, R15)

Codex CLI on `origin/master..HEAD` (the room-directory rule, the house-agent offer, ➕ only after a
connection, "invited" vs "came along", Who can join, My agents, the GitHub code once, Element's
widget pre-approval, and Element's E2EE well-known default). One blocking point, five should-fix.
Adopted, verified in the code, with tests:
- **Blocking. `io.element.e2ee` default false is homeserver-wide.** It would also start person-to-person
  DMs unencrypted, not just hide Element's notice in @pm's chat. Dropped: no Element setting hides the
  notice for one room only (NewRoomIntro shows it when privateShouldBeEncrypted() and the room is
  private and unencrypted; @pm's chat must stay both). The notice stays; reported to the owner.
- **GitHub code: a second ask while the first send is in flight posted the code twice.** The check
  and the send run under one lock per sign-in. test_github_code_once (also: a failed first send
  gets the code next time).
- **The panel's "Add an agent" showed before any agent connected, unlike the chat's ➕.** can_add
  now needs a connected (or server-run) agent, or a removed last one. test_console
  (test_add_an_agent_waits_until_the_first_one_connected); README says when each appears.
- **An online agent whose join is pending read "invited — not connected yet".** It reads "invited —
  joining". test_panel (test_an_agent_only_invited_is_marked_so).
- **A never-connected named agent was sent to 🔌, which only sets up the first one.** Its hint says to
  type "add another agent called <label>" (start_add resends that agent's code); the first agent's
  says "resend me the code" (a code lasts 30 minutes). test_panel_cards.MyAgents.
Rejected, with reasons:
- **"came along" before the agent's join is confirmed.** The task kept that text for a connected
  agent: an online bridge joins on invite within seconds; presence is at most CHECK_S (60 s) old.
- **Post ➕ when the first connection is observed.** A new message nobody asked for; "add another
  agent" works any time, and the next Connect message and the panel offer it.
- **Pre-approval also applies where another room's moderator adds the same panel URL.** That is the
  intent: it is our panel either way, it gets no capabilities, and the console checks room
  membership on every request. A browser check of the deployed Element is left for the deploy (no
  VM access in this session).
Re-check (GPT, the fix commit): one low point, adopted: a named agent's hint still began "follow the
🔌 steps"; it now says "follow its Connect message", and only the first agent's hint names 🔌.
test_panel_cards.MyAgents.

## GPT review of fix/ask-to-join (screen test: Ask to join, find a project, addresses, Lobby, R15)

Codex CLI on `origin/master..HEAD` (Element's feature_ask_to_join, "find a project" with 🙋, readable
addresses for listed and open rooms, the Lobby's directory listing, "Your agent comes along").
Three blocking, six should-fix, one nit. Adopted, verified in the code, with tests:
- **Blocking (part). An address made before a private repo was bound could keep the repo's name.**
  close_for_private now runs room_address.keep_secret: my address is replaced (#project) when it
  holds the private repo's name, and the repo isn't set when that can't be checked.
  test_room_address (test_binding_a_private_repo_replaces_an_address_that_names_it).
- **Blocking. A record that outlived its address could delete another room's.** take() resolves the
  alias first and deletes it only while it still points to this room; the record goes either way.
  test_room_address (test_a_record_that_outlived_its_address_never_removes_another_rooms).
- **Blocking. #lobby resolving was the only proof it is the Lobby.** It is listed only if @pm created
  the room and its join rule is public. test_joining
  (test_a_lobby_address_on_someone_elses_or_a_closed_room_is_never_listed).
- **A room made private while "find a project" was listing could still be posted; an old 🙋 echoed
  the room's current name.** Each line is re-checked just before it is posted; the reply to a 🙋 on
  a room no longer listed names no room. test_find_projects
  (test_a_room_made_private_while_listing_is_left_out, test_a_room_made_private_since_…).
- **A knock and 🙋 were two decisions: 👎 on the knock, then 👍 on the 🙋 still invited.** A decision
  (👍 or 👎 that went through) settles every request of that person for that room. test_find_projects
  (test_a_declined_knock_also_settles_their_click_so_no_later_thumbs_up_invites).
- **A PUT whose answer was lost made the next try use #name-2; canonical updates dropped
  alt_aliases.** A 409 on an alias that already points here is used; alt_aliases are kept on give and
  take. test_room_address (test_an_address_made_by_a_lost_try_is_used_and_other_addresses_are_kept).
- **An unreadable room read as private and used up the click; a failed moderator read used up the
  cooldown.** The 🙋 path reads visibility strictly (StateUnreadable keeps the button), and
  ask_to_join reads the moderators before taking a try. test_find_projects
  (test_an_unreadable_room_keeps_the_button_and_never_reads_as_unlisted).
- **Expired 🙋 offers were never pruned.** Pruned at each "find a project".
- **Nit. "Comes along" ignored the person's own follow setting.** "never" says nothing, "ask" says
  I'll ask. test_projects (test_a_new_room_says_comes_along_only_for_an_agent_that_can_join).
Rejected, with reasons:
- **Room names and topics that mention a private repo.** The owners choose them, and the room
  directory publishes them for every listed room (WORDS: "its name and topic are public"); @pm only
  ever adds the repo's name itself for a public repo, and never puts it in an address.
- **Link to the room ID instead of the cached alias.** Element turns the link into a pill showing
  its target, so a room ID link is the raw-ID pill the screen test reported. The alias is only
  removed when the room goes private, and then the 🙋 path refuses anyway.
- **Shared work limits / pagination for Find.** Unchanged pre-existing bounds (FINDS_PER_HOUR per
  person, MAX_SCANNED); the per-room messages are now capped lower (10). A directory query is a
  separate change.
- **Check offer_thumb's send result.** Pre-existing helper used by every button; out of scope (R8).
- **"comes along" for an agent that connected once but is offline now.** The task asked for the
  ever_connected rule; the agent is invited and joins when its computer is back.
Re-check (GPT, the fix commit): three blocking, four should-fix, one nit. Adopted, with tests:
- **A repo-named address that couldn't be removed still let the private repo be bound.** keep_secret
  raises, so close_for_private refuses. test_room_address (test_a_repo_named_address_that_cant_be_removed_stops_the_bind).
- **The 🙋 reply read the room's name after the ask (a rename meanwhile could leak).** The name as shown
  is kept with the offer. test_find_projects (test_the_reply_names_the_room_as_it_was_shown_…).
- **A 409 resolving here could be the owners' alias, then recorded as mine and later deleted.** It is
  used as the main address but never recorded as mine, so I never remove it. test_room_address
  (test_an_address_already_pointing_here_is_used_but_never_removed_and_others_are_kept).
- **A failed moderator read (is_moderator → False) still used up a try.** No moderator to ask: no try
  taken. **Nit: a header with no rooms under it**: the header goes out with the first room only.
Rejected: **lookup-then-DELETE race** (Matrix has no compare-and-delete; the window is one request,
and Synapse only lets @pm delete aliases it made or in rooms it moderates); **👎 on a 🙋 leaves the
Matrix knock pending** (the knock stays visible to moderators in Element; only my buttons go, as
the person was answered); **"ask" + never-connected agent** (the reply already says it joins once
connected; pm_follow asks when they join).

## GPT review of fix/screen-test-3 (panel in the Lobby, readable addresses, Element pop-ups, R15)
GPT (Codex) on origin/master..HEAD: three high, four medium. Adopted, with tests:
- **High: a front door was known only by its main address.** Also by the room `#lobby`/`#welcome`
  resolve to at each start (project_rooms.FRONT_DOOR_IDS, filled by pm_welcome/pm_lobby).
  test_front_doors (test_known_by_the_room_its_alias_points_to_…).
- **High: HTTP waits could still sit at "Signing in…".** The watchdog now runs from the first step
  (a link, a cached session, a token after Element's prompt) until the first view arrives.
  test_panel_signin (test_a_view_that_never_comes_still_ends_in_a_clear_line).
- **High: the address backfill ran before serving, and a non-StateUnreadable failure could stop the
  start.** It runs as a background task once polling starts, each room isolated (logged in full).
  test_room_address (test_at_start_one_rooms_failure_never_stops_the_others).
- **Medium: overlapping polls; a late 401 of an old session cleared a new one.** One view request at
  a time, and a 401 only counts for the session it was sent with.
- **Medium: an expired link session replayed the used one-time code.** The code is used once.
  test_panel_signin (test_a_one_time_link_is_never_sent_twice).
- **Medium: front doors refused reads but not actions** ("mode off" has no moderator check).
  console.act refuses too. test_front_doors (test_and_changes_nothing_there).
- **Medium: a failed layout write left the Hagency place behind for good.** Both state events are
  reconciled each time and a failed write is retried. test_front_doors (test_a_leftover_layout_place_is_cleaned_too).
Rejected: **re-check a widget re-added after a clean start** (adding a widget to the Lobby or Welcome
takes a moderator there, i.e. @pm or the server's admins; the next start removes it again).
Noted: the Element setting test checks the generated config.json, not Element's rendering (no
browser test harness for Element here); the setting's config level was read from v1.12.35's source.
R4: panel.py would have gone over 300 lines; its page template moved verbatim to static/panel.html
(panel.PAGE byte-identical, checked).
Re-check (GPT, the fix commit): six points. Adopted, with tests:
- **A stalled poll held the one-at-a-time lock and froze the panel.** Every request has a deadline
  (AbortSignal.timeout) and ends in the clear line. test_panel_signin (test_a_stalled_poll_gives_up_…).
- **The watchdog blamed Element on the standalone page.** Outside Element it names the server.
- **Rooms @pm just created weren't registered by ID.** They are now. test_front_doors (test_the_lobby_found_or_made_…).
- **The backfill task was outside the shutdown lifecycle.** It runs under guarded (in busy), so a
  restart lets it finish instead of cutting an address update in half.
Rejected: **a failed Welcome lookup skips the Lobby lookup** and **is_welcome_room checks only the
main address** (both pre-existing; the address check still guards, the next start retries; out of
scope, R8).

## GPT review of feat/remove-repo (remove repo, GitHub banner + Copy, panel on top, rename agent, R15)
GPT (Codex) on origin/master..HEAD: two blocking, five medium, one low. Adopted, each verified in
the code, with tests:
- **Blocking: rename trusted a registry reservation as proof of the account.** A remote agent's name
  is only reserved before its account is made, and Synapse's admin PUT creates a missing account.
  Rename now needs an account @pm made or holds ("ready", or one this server runs) under the exact
  name it must have, reads the account first (update only), under the set-up lock (remote_agent.
  WRITING), which also keeps Synapse and agents.json in one order. test_rename_agent
  (test_a_name_only_reserved_here_…, test_a_missing_account_is_never_created_…).
- **Blocking: a confirmation removed whatever repo the room had then.** The ✅ question and the
  panel's second click carry the repo the person saw; a different binding is refused (CHANGED).
  test_remove_repo (test_a_confirmation_never_removes_a_repo_bound_since).
- **Medium: cancelling released the poll lock before the removal finished.** As choose_repo: the
  lock is held until the shielded job ends. test_cancelling_keeps_the_poll_lock_…
- **Medium: the moderator check came before waiting for the binding lock.** It is read inside it,
  right before the write. test_a_demotion_while_waiting_for_the_binding_lock_counts.
- **Medium: rename held up the sync loop** (an admin sign-in can wait) **and AccountError escaped.**
  The chat path runs in the background; AccountError is a refusal.
- **Medium: a failed board edit stayed for good** (unbound rooms were skipped). The board stays
  marked "unbound" and each poll retries; a reposted board is pinned. test_a_board_edit_that_failed_…
- **Medium: any page under the panel's widget key was promoted.** Only our exact widget URL.
  test_panel_layout (test_another_page_under_the_panels_key_is_never_promoted).
- **Low: a focused code field froze the card on a stale code; a sign-in finished before the first
  view kept the line.** The field is redrawn once its code changes; connected clears the line.
  test_panel_github_card (test_connected_before_any_view_showed_the_code_clears_it_too).
Rejected: **a binding revision for "removed then rebound to the same repo"** (the person confirmed
removing that repo, within 10 minutes; the check above covers a different one) and **the layout
read/write gap** (Element changes a room's layout only on a moderator's drag; the next start
re-checks).
Re-check (GPT, the fix commit and the chat links): one blocking, three medium, one low. Adopted,
with tests:
- **Blocking: the panel's armed Remove repo followed a redraw onto a newer repo.** The first click
  stores the repo it armed; another repo shows unarmed and its click only arms. test_panel_cards
  (test_a_repo_bound_after_the_first_click_is_never_armed).
- **Medium: a reposted board whose pin failed lost its retry mark** (and the mark was saved only
  after the network calls). Saved first; dropped only once edited or reposted, pinned in the old
  board's place, and published. test_remove_repo (test_a_reposted_board_that_couldnt_be_pinned_…).
- **Medium: the fallback opened a tab from Element's reply, which browsers block as a pop-up, and
  no reply left nothing.** A refusal or no answer within 4 s shows a link to click.
  test_panel_chat_link (test_a_refused_navigation_offers_a_link_to_click).
- **Medium: a rename used two admin sign-ins while holding the set-up lock.** One admin session
  (local_signup.admin_session) for the read and the write; renames go one at a time on their own
  lock, and the set-up lock is held only around agents.json. test_rename_agent (one session).
Rejected: **mark the Connect line only when a code was issued** (Connect shows only without a
login, so "a login and no code" after it means the person connected meanwhile and the line is stale
either way; a refusal is still kept while there is no login).
Noted: navigation inside a running Element v1.12.35 was not exercised here (node stand-in for the
widget API); the action name, data.uri and the lifecycle module's capabilities_approved were read
from upstream, and the one screen check at the end covers it.

## Panel framing: Element Desktop and Element's own apps (seen live, before its GPT review)
The panel was blank in Element Desktop: its frame comes from `vector://vector`, and the panel allowed
only `'self'` and our Element (frame-ancestors), with Caddy adding `X-Frame-Options SAMEORIGIN` to
everything on the rooms name. Now one list (bridge_config.panel_embedders: ours, Element Desktop,
app/develop/staging.element.io; HAGENCY_PANEL_EMBEDDERS / PANEL_EMBEDDERS replaces it, never `*`)
sets the panel's frame-ancestors, and the page talks only to the first of those origins that speaks
to it. X-Frame-Options stays on Element's own pages only (it can't name several origins; CSP
frame-ancestors supersedes it); the Connect page keeps frame-ancestors 'none'.
Why framing by more apps is safe (clickjacking): the panel does nothing until it is signed in, and
inside a frame it signs in only with an OpenID token the framing Element app gives for its own
signed-in person, which @pm verifies with our homeserver (so only our own people, and only as
themselves); a page that isn't on the list gets no answer at all. Destructive clicks ask twice
(Remove an agent, Remove repo). Element Desktop and other servers' Element apps never read our
config.json, so they ask once (Continue / Allow) before showing the panel: a new room's first
message says so and links the same panel in a browser (/console?room=…, signed in by link).
Tests: tests.test_panel_framing (header values, the configurable list without a wildcard, Caddy,
Element Desktop signs in, a page not on the list is never answered, the room message).
GPT (Codex) on the commit: nothing blocking (frame-ancestors checks every ancestor; origin pinning,
`vector://vector` as Element Desktop's origin, dropping X-Frame-Options from @pm's other responses
and Caddy's header order all checked against upstream). Adopted: **pin the Element origin only on
its capabilities request** (not any first message); **ELEMENT_ORIGIN is validated like the list**
(it goes into the CSP and the page's script; test_a_malformed_element_origin_never_reaches_the_policy);
**an empty PANEL_EMBEDDERS means ours only** (Compose's `-`, not `:-`). Noted, not changed: a framing
app on the list can overlay the signed-in panel and induce clicks (that is trusting Element's own
apps, which hold the person's Matrix session anyway); Element's nginx sends no X-Frame-Options today,
so Caddy's one value is final.

## GPT review of feat/agent-picker (AI picker, reusable names, My Hagency, home button, R15)
GPT (Codex) on feat/remove-repo..HEAD: two blocking, three medium. Adopted, with tests:
- **Blocking: a key without a known prefix (or in a URL path) passed as a model or URL**, and an
  extra `api_key` field in the panel's JSON was silently dropped. A long mixed letters-and-digits
  piece counts as a key too (model names and paths are short words), and any extra field refuses.
  test_agent_picker ReviewFixes (test_keys_without_a_prefix_…).
- **Blocking: another agent's slot name shadowed names.** After codex came back as slot codex2,
  adding "codex2" resent codex's code; chat remove / disconnect / rename looked up slots only. One
  rule now (registry.called: account, slot or given name), used by every typed lookup, and no two
  of a person's agents share any of those names (add and rename refuse). test_another_agents_slot_…,
  test_rename_agent (test_a_name_another_of_their_agents_has_is_refused).
- **Medium: a personal chat that stopped being private fell through to project handling.** @pm's
  own chats with a person are always My Hagency or refused (NOT_PRIVATE), never a project room.
  test_my_hagency (test_a_chat_that_stopped_being_private_…).
- **Medium: the picker took URLs ADR 0008's client refuses (a malformed port), and finishing an
  unfinished agent kept its old choice.** The port is checked; the choice made now is recorded.
  test_a_malformed_port_…, test_finishing_an_unfinished_agent_takes_the_ai_chosen_now.
- R4: console.act was at 52 lines; Add an agent moved into console.add_agent.
Deferred: **the home button may open a DM the person made themselves** (Element's #/user/…?action=chat
reuses the direct chat in their m.direct; @pm creates its own with is_direct, so that is the usual
one; a person-opened DM gets no My Hagency, and the panel's chat links point to @pm's own chat).
Re-check (GPT, the fix commit): two blocking, two medium. Adopted, with tests:
- **@pm's replaced chat lost its protection** (a newer chat overwrote the record). Every chat My
  Hagency was put in is kept on disk (console_personal.had_panel) and stays refused if it changes.
  test_my_hagency (test_a_chat_pm_replaced_since_is_still_refused).
- **A new slot could take another agent's given name** (codex → codex2 while another agent is
  called codex2). Slots skip given names too. test_a_new_slot_never_takes_another_agents_given_name.
- **Resending a code could change a finished agent's AI.** Only an unfinished one takes the new
  choice, after the account check. test_a_finished_agent_keeps_its_ai_when_its_code_is_resent.
- **Keys of letters (or digits) only.** A word of 32+ characters counts too.
  test_a_long_word_of_letters_only_is_a_key_too.
Rejected: **catching every possible key** (a short or split key can't be told from a model name;
@pm never asks for one, says where it goes, and refuses what looks like one; a model ID with a
40-character hash in it is refused as the price); **two renames of one person racing past the
name check** (5 per hour per person, one at a time on the server: the window is a person against
themselves); **multi-word names in "remove my agent NAME"** (the panel's buttons cover them).

## GPT review of feat/agent-backends (agent kinds on your own computer, Connect page, R15)

The review focused on secret handling and command injection. GPT found no shell or flag injection:
the model and address are separate argv items, and connect.ps1 splats an array. It raised 11
points. Adopted, each verified in the code and covered by a test:
- **Key bound to its address:** `remote.env` now holds the key together with the address it was
  typed for. A turn whose `base_url` differs gets no key. Before, during a reconnect the running
  bridge (old address) could send the newly typed key to the old address.
- **A reply that echoes the key:** the exact key is now cut out of successful replies too, not
  only out of errors.
- **Sessions per address:** saved sessions are keyed by the address (`remote:<hash>`), so a
  conversation never resumes at another provider.
- **Hermes `.env` and YAML:**
  - A line break in a server-sent value (the password) is refused rather than written into `.env`,
    where it could have added `MATRIX_ALLOWED_USERS=*`.
  - An older `.env` is chmod'ed to 0600 and opened without following symlinks.
  - YAML values have their quotes doubled.
- **Validators:** they used `match` with `$`, which allows a trailing newline. They now use
  `fullmatch`, and PowerShell uses `\z`. A malformed IPv6 address no longer raises from
  `urlsplit`.
- **Status limiter:**
  - The wrong-code budget is per client only, so many guessers can't stop everyone's status.
  - Refused asks aren't stored.
  - The page polls only a whole code, never one still being typed.
- **Odd offer values:** a kind, model or address @pm sent that this computer won't use now still
  leaves the sign-in saved (the code is used up), with "rerun with `--agent`". Before, nothing was
  saved.
- **Settings stay with their kind:** a model or address is kept only for the kind it was chosen
  for, and @pm's only when @pm named that kind. It is saved before the tool check, so a rerun
  keeps @pm's model.

Not adopted:
- **A credential broker that hides the key from the agent's own tools:** same-user files the
  sandbox can read stay readable, as for `credentials.json` and the CLIs' own sign-ins (ADR 0006's
  accepted risk, now named in ADR 0008).
- **A fixed public error for remote failures:** errors are scrubbed and cut short, and they help
  the owner fix a wrong address or model.
- **Retryable redemption with a client-held claim secret:** a server protocol change, outside this
  PR. A used code's sign-in is now always saved first.
- **A separate status token and origin:** the status reveals only waiting/connected/expired, by
  POST body, rate-limited.
- **"@pm never supplies the kind":** that is the @pm side's part of the contract (another
  change). The `#kind=` on the page is only a pre-check hint.

GPT re-check of the fixes (9 points). Adopted, with tests:
- **JSON-escaped key:** the key is also cut out as a JSON error would spell it.
- **Every kind of line break** (` `, `\x1c`... as `splitlines` sees them) is refused, and
  checked on the whole saved sign-in before any profile file is written.
- **Status limiter:**
  - It keeps no shared `"*"` list, and quiet clients are forgotten.
  - An ask refused for wrong codes isn't counted against the minute's budget.
- **Polling:** the page polls a code with its dashes anywhere, as redemption accepts it.
- **@pm's settings:** a model or address from @pm applies only with the kind @pm named.

Not adopted:
- **`#`/`${}` in a dotenv password:** the server generates these passwords (no such characters).
  The Hermes dotenv quoting is unchanged from the server set-up.
- **A hard-link swap of a profile's `.env`:** it needs another local account to write in the
  owner's own `~/.hermes`. Symlinks are already refused (`O_NOFOLLOW`) and the mode forced to 0600.

## GPT review: batch 12–14 combined (remove repo, rename, picker, agent kinds, Connect page, R15)
GPT (Codex) on the whole diff origin/master..integ/batch-12-14, after the wiring (pm_kinds on
agent_kinds, the kind in credentials_for, page_link(code, kind) and `--agent` in every Connect
message, the picker from agent_kinds.kinds()). One blocking, ten medium, four low. It found no
shell, flag, TOML or HTML injection in the new paths, and the navigate handler's checks held.
Each point was checked in the code. Adopted, each with a test in tests/test_batch_review.py:
- **Blocking: a stale ✅ could remove another agent.** remove_for_good matched by any name, so once
  the asked-about account was gone, another agent *displayed* as that account matched. It now
  takes an account name only (localpart or slot), never a display name.
- **"expired" while connecting.** POST /connect used the code before looking up the sign-in, so a
  status ask in that gap said expired and the page stopped. The code is marked "redeeming" (no
  await in between) and reads "waiting" until the answer.
- **Concurrent status asks passed the wrong-code budget.** The check now runs after the body is
  read, with no await between check, state and charge.
- **"remove the repo" couldn't end a pending GitHub App wait** in a room with no repo yet: it ends
  the wait now and says so.
- **A private repo's room, unbound, could later be made open** and show its past to anyone who
  joins: it stays refused (see the re-check below for how).
- **"@pm …" typed in our private chat** wasn't understood by Add, Rename, GitHub or the welcome
  commands (the address was stripped only in rooms): pm_address.unaddressed for all four. (Found by
  the flow test for Add first; GPT named Rename.)
- **Hermes on the Windows tab still showed a line** to copy by hand, which would use the code up:
  no line there now (connect.js shownCommand).
- **The panel's resend hint named the slot** ("add another agent called codex2"), which @pm refuses
  for an agent called "codex": it names the agent's own name, or says to remove and add it again
  when that name can't be typed in one word.

Not adopted:
- **A provider echoing the key into Codex's local history:** the provider already holds the key,
  and the file is the person's own (ADR 0008's accepted same-user risk).
- **The key check runs on @pm's side:** the panel never asks for a key; a key pasted into the model
  field is refused, and catching every short key was rejected before (#13).
- **Panel framing by an allowlisted Element:** by design (#12); every listed origin is an Element
  the owner chose, and PANEL_EMBEDDERS is one list, never `*`.
- **Two renames racing past the name check:** a person against themselves, 5 per hour (#13).
- **"Connected" should say "code redeemed":** the page already says the agent is starting and to
  wait for the Terminal.

Deferred (the on-screen check covers them):
- **Windows ACL when connect.py runs directly with its own `--home`:** connect.ps1 sets it; a
  direct run on Windows with an existing folder keeps that folder's ACL.
- **Ollama's /api/tags may list no capabilities,** and then every model counts as able to use
  tools (a non-tool model fails at its first turn, not at Connect).
- **The home button can open a DM the person made** (no My Hagency there), as deferred in #13.

GPT re-check of these fixes (one blocking, two medium, four low). Adopted, with tests:
- **Blocking: setting history to "joined" at unbind hides nothing.** Matrix shows each message
  under the rule it was sent with. Instead, unbinding a private repo leaves a marker
  (`xyz.hagencylabs.private_past`), and Open stays refused for that room (Listed is still allowed,
  as for a private repo; an unreadable marker counts as set). If the marker can't be written, the
  room is told to stay private or listed.
- **Ending a quiet install wait named the repo.** A quiet wait exists before write access is known,
  so naming it would confirm the repo. The answer is now the same words with or without a wait.
- **A bind finishing as the wait ended was reported as "no repo".** stop_waiting waits for the
  cancelled watch (its bind is shielded) and reads the room again, then asks the usual ✅ question.
- **An already-blocked status client still had its body read:** a non-charging check comes first.
- **The resend hint for a name of several words** said to remove the agent; it now says to rename
  it to one word, which keeps the account.
- **Synapse refuses `restrict_public_rooms_to_local_users`** next to the two directory keys: the
  older key is removed from homeserver.yaml.
- Also fixed: a watchdog test leaked status_board.at_start (from #12's panel lift), which failed
  a later test when garbage-collected.

Not adopted: **"pm-connect" or "@pm:server" prefixes** in the private chat. The same ADDRESS_RE
already reads room messages this way, and Element sends a mention as the display name.

### Owner decision in batch 12–14: any repo the asker can write to
- **What changed:** `HAGENCY_REPOS` is optional. Empty means any repo that has the Hagency Rooms App
  and that the asker's connected GitHub account can write to (github.any_repo / allowed_repo). When
  set, it still restricts.
- **Without the App** (local dev, the host's own `gh` login), only `LOOP_REPO` and the list count,
  as before. A free choice there would expose whatever that login can read.
- **Reading a room's binding** (room_state.repo_in, also in agents' processes, which have no App
  settings) accepts any owner/name @pm wrote while no list is set. Hermes profiles get only the
  operator's list.
- **The refusal names the one missing step** (repo_refusals.py):
  - the install link when the App isn't on the repo;
  - "Your GitHub account @login can't write to owner/name";
  - "This server only allows certain repos" when a list is set.
- **Kept shared, and why:** a *private* repo with the App installed but no write access (or one
  GitHub can't tell us about) gets the install step's words, and its wait ends quietly. Any other
  answer would confirm to anyone in any room that a private repo of that name exists and has our
  App. The install step itself confirms nothing, because GitHub answers "not installed" the same way
  for a missing repo and for a private one without the App. (GPT's review below found the first
  version, which had its own shared sentence, was still an oracle.)

GPT review of this change (three blocking, two medium, two low). Adopted, with tests
(test_any_repo, test_github_link_more, test_github_app_flows, test_batch_review):
- **Blocking: install words vs a separate shared sentence was an oracle.** A private repo with the
  App and no access now reads exactly like a repo without the App, and both waits end quietly: no
  closing "still not installed" line, no denial. A public repo still names the account that can't
  write.
- **Blocking: switching from a private repo to another skipped the never-open marker.** bind() sets
  it when the old repo was private, as removing does.
- **Medium: case.** With no list to spell it, the bound name is GitHub's own `full_name`, so one repo
  is one key in every record.

Deferred:
- **Blocking (pre-existing): a renamed or deleted repo whose name is reused** by another repo that
  also installs our App. A binding stores only owner/name; storing GitHub's repo ID at bind and
  checking it on every read is its own change. The new owner must install our App on the new repo,
  so this needs that owner's own action.
- **Medium: a list set later** hides unlisted bindings from @pm but not from agents on people's
  computers (they have no list). Operator guidance: remove such rooms' repos first. A Matrix-side
  revocation is a later change.
- **Low: an install wait drops ➕'s "discuss here" choice;** the person is asked again.

### GPT review of the panel order change (repo before Mode)
Adopted, with tests in test_panel_cards:
- **Medium: with panel_project.js missing, the panel lost its Off button.** Mode now lives in that
  script, so a fallback inline card keeps Off.
- **Low: the disabled-state test passed even with buttons missing.** It now asserts all three are
  present and reads each one's disabled attribute.

## GPT review of fix/screen-test-4 (federated panel sign-in, first agent's AI, panel wording, R15)
Codex CLI, whole branch, focus on the federated OpenID check's SSRF and token handling. Twelve
points (three blocking). Adopted, with tests in test_fed_openid_review, test_first_agent_ai,
test_agent_picker and test_kind_flow:
- **Blocking: "public" alone isn't the whole egress rule.** This server's own addresses (its server
  name, its public console) and `HAGENCY_FED_DENY` CIDRs are refused too (safe_fetch.resolve's new
  `deny`); every address of every hop, as before.
- **Blocking: a slow peer could outlive the deadline** (headers dripped under a per-read timeout, a
  hanging getaddrinfo, SRV timeouts per resolver). A watchdog cuts the TCP connection at the
  deadline, the lookup is given up at the deadline, and SRV lookups share it.
- **Blocking (partly): limits.** Our own homeserver's branch now counts against a per-client budget
  too; the proxy-aware client address is computed in the worker thread; the concurrency slot is
  taken before quotas are charged.
- **Medium: case variants** of a server name are one quota and cache key (lower case); expired
  console sessions are pruned.
- **Medium: SRV parsing:** the echoed question and each record's owner must be our name, the target
  must end inside its record, a label with "." is refused, an over-long name is never asked.
- **Medium: odd input:** a non-object body, a non-ASCII Content-Length, deeply nested JSON, a
  malformed redirect, a trailing newline (`$` with match → fullmatch) and scoped IPv6 literals all
  end as "not verified", never a 500. The local check ignores environment proxies.
- **Medium: "the one on my computer" couldn't undo a first agent's choice:** the picker always sends
  its JSON, and for a first agent an empty kind clears the earlier one.
- **Low: a second click while setting up** says so instead of promising a Connect page.
- **Low: Hermes's Connect message** said "Mac, Windows or Linux" and didn't mention the invite patch;
  the home page now points to My Hagency's picker.

Deferred / rejected:
- **Medium (6): the token in the userinfo query string** can be logged by the issuer's proxies:
  that is the spec's own API (the token is meant for that server, and is short-lived).
- **Medium (5, part): trying further SRV targets** after the first fails: the first by priority
  is used; a server whose first target is broken falls back to the one-time console link.
- **Medium (10): a kind change racing a code redeemed at the same moment:** the window is the
  seconds between redeeming and the first presence; a person re-choosing then gets a fresh code
  for the new kind anyway. A per-agent configuration generation is its own change.
- **Low (8): IPv6-literal server names** can't sign in (identity.USER_RE): fails closed; such
  servers use the one-time link.
- A full cancellable transport and a maintained DNS library: the watchdog and the strict parser
  cover the cases found without a new dependency.

### GPT re-check of the review fixes
Six points; adopted, with tests in test_fed_openid_review (ReCheck, SrvChecks):
- **High: cache eviction reused the variable `key`**, so at capacity one server's discovery result
  was filed under another's name. Fixed (its own loop variable).
- **High: the watchdog couldn't cut a TLS read:** wrapping detaches the TCP socket. The TLS socket
  is set before the handshake (do_handshake_on_connect=False) and abort() shuts its descriptor
  down; tested over a real socket with a silent server (the old abort fails that test).
- **Medium: lookups a deadline gave up on** each hold one of 8 resolver slots until they really end.
- **Medium: an own-name lookup failure** keeps that name's last known addresses on the deny list.
- **Low: NXDOMAIN** counts only after the echoed question matches ours.
Rejected: charging the per-client "any sign-in" budget before a federation slot is free (60 per
10 minutes is generous, and charging it up front is what bounds the local branch).
- **Found by CI, after the re-check:** the deny list's "never looked up" (0) read as fresh while
  `time.monotonic()` was under 10 minutes, as on a freshly booted server: no own addresses refused
  for its first ten minutes. Fixed, with a test.
