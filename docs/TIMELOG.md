# TIMELOG — hagency-bridge prototype (named element-bridge until 2026-11-19 11:45) (2026-11-19, times from `date`, local)

| Time | Event | Elapsed from start |
|---|---|---|
| 08:47:32 | Start (env check: Docker 28.1, Python 3.12, codex-cli 0.169, Claude signed out) | 0:00 |
| 08:48:38 | Synapse + Element Web containers up (`/_matrix/client/versions` OK, :8649 200) | 1:06 |
| 08:48:59 | anna, betty, anna-agent, betty-agent + `#lounge` created (`setup_accounts.py`) | 1:27 |
| 08:50:00 | Both bridges online (first run of `bridge.py`, no edits needed) | 2:28 |
| 08:50:28 | **First agent reply** — anna-agent answered via Codex (e2e test, 6.2 s turn); whole e2e suite passed on first run | 2:56 |
| ~08:50:50 | **First Element login** — anna in Element Web (in-app browser), Lounge history rendered | ~3:20 |
| 08:51–08:52 | First reply to a real Element pill @mention (typing indicator visible; ~40 s Codex turn) | ~4:30 |

Wall-clock for the session includes writing ~270 lines of bridge code; the clock times above
are what `date` reported.

## Fix cycles (edit → rerun → verified)

| # | Fix | Edit start | Bridge rerun | Verified | Cycle |
|---|---|---|---|---|---|
| 1 | Replies showed raw `**markdown**` in Element → render to `formatted_body` (adds `markdown` dep) | 08:52:16 | 08:52:22 (restart < 1 s) | 08:53:00 in Element UI | **6 s** edit→running; 44 s incl. a Codex turn and UI driving |
| 2 | Claude CLI failure showed empty error (`claude exited 1:`) → parse JSON error from stdout | 08:53:05 | n/a (direct call) | 08:53:15 (`OAuth session expired…` surfaced) | **10 s** incl. a CLI call |

No compile step: Python process restart is < 1 s; per-room Codex sessions survive bridge
restarts (stored in `rooms/<owner>/<room>/.bridge-session.json`), verified by the follow-up
"same list again please" after fix 1.

## Later checks
| Time | Event |
|---|---|
| 08:54:53 | Encrypted-room path verified (agent posts "can't read encrypted rooms"), added to `test_e2e.py` |
| 08:56:01 | `./start.sh` verified end to end (idempotent re-run, both bridges online) |
| 08:56:35 | Full `test_e2e.py` (6 checks) + `test_rules` (8 tests) pass against `start.sh` bridges |

## Review fixes (GPT review, R15)
| Time | Event |
|---|---|
| 08:57 | GPT (Codex CLI) review of commit abc4cb4: 11 findings, none an injection |
| 08:59:31 | Hardened bridge restarted via `./start.sh` (owner-only default, Claude tool limits, invitee/DM checks, session-id validation, hash workdirs, concurrency cap) |
| 09:00:40 | 17 unit tests + 7 e2e checks pass (new: anna-agent ignores betty) |

## Step 2: autonomous GitHub loop

| Time | Event | From step-2 start |
|---|---|---|
| 09:02:13 | Step 2 start (read sandbox repo: 14 open `agent-ok` issues, `main` **unprotected**, your-account is ADMIN) | 0:00 |
| 09:03 | Setup steps for the owner sent (second-account login command, collaborator, ruleset) | ~1 |
| 09:06:44 | Code done: `loop.py` 203 + `tasks.py` 112 + `github.py` 81 lines, bridge +35, fake gh + tests | 4:31 |
| 09:07 | Run 1: `!level` refused — matrix-nio ignores room v12 creator power (creators aren't in `power_levels.users`) | |
| 09:08:56 | Fix: read the create event for v12 creators (+4 tests); rerun | 6:43 |
| 09:09:21 | Run 2: build → PR → review requested all pass (PR opened in 14 s); test matcher bug | |
| 09:09:29 | Run 3: approve passes; merge step hit a duplicate build from a **replayed old task** | |
| 09:12:11 | Fix: drop events older than process start + accept results only for the current stage/agent (+4 tests); rerun | 9:58 |
| **09:13:12** | **Loop e2e ALL PASS**: issue → claim → PR → other agent reviews → GitHub-approved → merged in **20 s** (Autonomous); Assist stops at "ready — a person merges" | **11:00** |
| 09:13:30 | `start.sh` now also runs loop-bot; step-1 e2e (7 checks) still passes | |
| 09:14:16 | Real read against your-name/hagency-demo-sandbox OK; anna-agent identity OK, betty-agent awaiting the owner's login | |

Fix cycles in step 2: edit→rerun of the full loop test was about 1 minute each (49 s test). Most of
the time went to diagnosis, not waiting on builds.
| 09:15 | GPT review of the step-2 commit: 10 findings (7 high) |
| 09:17:19 | Fixes in place (+10 tests, 40 unit tests); loop e2e rerun, now with the fake gh also enforcing commit-pinned reviews/merges |
| **09:18:17** | **Loop e2e ALL PASS again** (issue → merged in 20 s) |
| 09:19:12 | `start.sh` restarted (bridges + loop-bot); step-1 e2e 7/7 pass; 40 unit tests pass |
| 10:50 | Real identities ready: betty-agent → second-account (the owner signed in via device flow; token in keychain, not in hosts.yml), Write collaborator on the sandbox; ruleset <ruleset id> on main (PR + 1 approval, dismiss stale, last-push approval, no bypass) |
| 10:52 | First live Autonomous run on the real sandbox: #96 already satisfied → builder made no change → loop stopped (correct, but needed a person) |
| ~11:00–11:10 | the owner's live Autonomous run: #102, #103, #108, #112, #113, #114 → PRs #149–#154, each built by one agent (your-account and second-account), approved by the other, merged by the loop |
| 11:13:33 | Already-done auto-close + clickable PR/issue links: loop e2e ALL PASS, 12 checks (already-done issue verified by the other agent and closed in 26 s) |
