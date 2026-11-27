# 0027: Trial agents that build

## Status
Accepted, 2026-11-27. Builds on ADR 0025 (the talk-only trial agent). Drafted as "0024 (draft)"; renumbered, since
0024 is the agent auto-update work and 0026 the panel restore.

Owner decisions (answers to the draft's open questions):
1. **Sandbox vendor:** E2B, on a dedicated account with **prepaid credit and no card on file** (the hard spend cap).
2. **Ollama sharing:** all trial traffic, talk and builds together, uses **at most 1** of Ollama Cloud's 3 concurrent
   slots, and **talk goes first**.
3. **Demo repos:** **private**, Hagency-owned, in a `hagency-trials` org, **leased per visitor and recycled to the template**.
4. **Gate:** a **GitHub-linked** account (ADR 0007) **at least 30 days old**, of a **local** (not federated) user.
5. **Caps:** **3 builds a day per visitor** (per GitHub account), **20 a day for the server**.
6. **Merge stays manual:** "ready, a person merges". The trial never merges.
7. **Concurrency:** **one active build server-wide**, a **queue of at most 5**.

## Context
Visitors without an agent of their own can talk to the shared trial agent (ADR 0025), but cannot see the BUILD flow:
claim an issue, write code, run tests, open a PR, get a review, reach "ready, a person merges". The owner wants that flow
shown by the server itself, without overloading the VM and safely enough to run untrusted code.

Constraints: one Oracle A1 VM (2 OCPU, 11 GB RAM, 45 GB disk; 18 GB of it is an unrelated project in `~/sandbox`, which
must not be affected). Docker; @pm is on `edge`, Synapse on `edge` and `db`. @pm holds the Synapse registration secret
and the read-only App key (ADR 0004). Ollama Cloud Pro: 3 concurrent requests, shared with another project, with usage
caps. Sign-up is open to any email (ADR 0013), so per-account quotas are cheap to get around.

Threats: arbitrary code from visitors or model output; the server `.env`, the App keys and Matrix data; other tenants;
mining and other abuse; GitHub token misuse; the visitor's own CI secrets and caches; leaking private repos; cost.

## Options
- **A. GitHub Actions in the visitor's own repo.** Rejected: a same-repo branch runs model-written code with the repo's
  secrets, and `ACTIONS_RUNTIME_TOKEN` can poison default-branch caches. Too much onboarding for a demo, too.
- **B. A gVisor container per job on our VM.** Rejected: untrusted code next to the Synapse secret, the App PEM,
  Postgres and `~/sandbox`; a root runner running `git` on a hostile tree; unmetered disk I/O.
- **C. An E2B Firecracker microVM per build; our server only orchestrates.** Adopted, with the fixes below. About $0.02
  a build; the VM's added load is a few HTTPS calls.

## Decision
Option C, Phase 1: **Hagency-owned demo repos only**.

- **Demo repos.** A fixed pool (`TRIAL_REPOS`, default `demo-1,demo-2,demo-3`) in `TRIAL_ORG` (`hagency-trials`), made
  by the owner from a template with seeded, known-solvable `agent-ok` issues. Private. Actions disabled org-wide, no
  secrets. A visitor (by GitHub account id) leases one; the lease ends after 7 days or once every PR it opened is
  closed, and the next visitor gets it recycled: trial PRs closed, `trial/` branches deleted, the default branch reset to
  the template commit (the one the pool first saw). No repo is created at runtime, so no Administration permission.
- **Untrusted execution only in E2B** (`common/sandbox.py`, SDK `e2b==2.66.0`, worker-only `deploy/requirements-trial.txt`).
  - BUILD sandbox: our pinned template (`TRIAL_E2B_TEMPLATE`), 2 vCPU, 1 GiB, E2B timeout 15 min, `envs={}`. Egress
    `deny_out` all traffic, `allow_out` only `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` (E2B: allow
    wins over deny; with a domain allowed, its default DNS server 8.8.8.8 is reachable too). Before anything is
    uploaded: a blocked host (`example.com`) is probed and must not answer, and `env` must hold only plain settings
    (no credential-like names); otherwise the build is refused ("stopped for safety").
  - TEST sandbox: fresh, `allow_internet_access=False`, the pristine tree plus the validated patch, the template's
    fixed test command (`TRIAL_TEST_CMD`, never one taken from the repo; `python -P` so nothing in the repo stands in
    for pytest). The verdict: exit 0 AND its JUnit report shows at least one passed test and none failed.
  - Every sandbox is killed in `finally`; a reaper kills any of ours older than 20 min (metadata `hagency=trial-build`),
    and all of them when the worker starts or is switched off.
- **The tool loop** (`server/trial_tools.py`) runs in the worker: tools `run`, `read_file`, `write_file`, `list_dir`,
  `finish`, each executed inside the BUILD sandbox. Caps: 40 tool calls, 10 min, 150k tokens a build, 8 KB of each
  output kept. Not finishing within them is a failure (R14).
- **The patch is built on our side** (`server/trial_patch.py`): the worker keeps the pristine tarball in memory, reads
  back only files whose hash changed, and builds the patch itself; every change is a plain blob with the pristine mode
  (else 100644), so no symlink, gitlink or mode change can come out. Refused: `.github/`, `.devcontainer/`,
  `.vscode/`, `.git` paths, `.gitattributes`, `.gitmodules`, `.pre-commit-config.yaml`; symlinks or other non-files;
  binary content; test files or test config (incl. `conftest.py`, `pyproject.toml`, `setup.cfg`, `package.json`); over
  200 KB or 50 files; bad paths (absolute, `..`, control or markup characters, not NFC, case/NFKC collisions); no change.
- **GitHub:** a separate App, "Hagency Trial Builder" (`server/trial_github.py`): Contents RW, Pull requests RW, Issues
  RW, Metadata R; never Workflows, Administration, Actions or Secrets; installed only on `hagency-trials`. Each step mints
  a fresh token for one demo repo with only what it needs (read: tarball, issue; write: branch, PR, label, review) and
  revokes it at once. Writes pass an allowlist: tree/commit, `trial/` refs only, the PR, its label, a COMMENT review,
  closing its own PRs, and the recycle. No merge call exists. The PR is labelled "written by AI in a sandbox" and
  carries an opaque job id, never the Matrix ID.
- **The reviewer** (`server/trial_review.py`) is talk-only: the issue, the diff and the test result go to the model as
  text; its notes go on the PR as a COMMENT review headed "AI self-check", with a fixed part listing the files and any
  outside `src/` and `tests/`. It never approves. @pm then says "Ready: a person merges". Model words reach GitHub and
  Matrix only as inert text (`common/redact.inert`: no links, mentions, markup or HTML).
- **The worker is its own compose service, `trial-worker`** (`server/trial_worker.py`): `read_only`, tmpfs `/tmp`,
  `cap_drop: ALL`, `no-new-privileges`, `pids_limit: 128`, `cpus: 0.5`, `mem_limit: 512m`; its own outbound network
  `trial-out` (never `db` or `edge`); only `deploy/trial.env` (E2B key, Trial App id, its Ollama key, caps) and
  `deploy/trial-secrets/trial-app.pem`; this commit's code read-only. It never gets @pm's `.env`, the registration secret
  or @pm's App key. **No Matrix credentials either**: @pm tells visitors the news (below), so the draft's "trial bots'
  Matrix tokens" are not needed.
- **@pm and the worker share one folder** (`~/hagency/trial-shared`, nothing secret in it; `server/trial_jobs.py`):
  the queue (flock'd read-modify-write), the worker's heartbeat with the caps, the leases and the Ollama slot's lock.
  @pm offers builds only while the heartbeat is fresh, so **the feature is OFF unless E2B_API_KEY, TRIAL_APP_ID (and its
  key file), TRIAL_ORG and OLLAMA_API_KEY are configured**: without them the worker exits at once, writes no heartbeat,
  and nothing changes for anyone (the trial card, the welcome and @pm's words are exactly as before).
- **The visitor flow, GUI first** (`server/pm_build.py`): a **Try a build** part on the Trial agent card in My Hagency,
  or "try a build" in their chat with @pm. The gate (`server/trial_gate.py`), then "queued: you're #N" with where the
  code goes; "started: issue #N in <repo>"; "done: pull request #N … Ready: a person merges" with an escaped,
  length-capped excerpt of the self-check (no links, no mentions, no markup); or "didn't finish: <fixed reason>".
- **Ollama:** one cross-process slot (`common/model_slot.py`): a file lock in the shared folder, used by the talk trial
  (replacing its in-process semaphore) and by every build model call; a waiting talk answer leaves a marker and builds
  never take the slot while one is fresh. The worker counts its tokens in its own month (`trial_budget.Budget`,
  `build-budget.json`), pausing at `TRIAL_MAX_SHARE` (0.7) of the plan or `TRIAL_MONTHLY_TOKENS`; a 429/402 pauses it.
- **Caps** (counted by @pm when a job is queued, never given back): per GitHub account 3 a UTC day and 10 ever; 20 a day
  for the server, bypassed by the operator's `TRIAL_BUILD_DEMO_USERS`; the queue holds 5; one job at a time per visitor;
  2 sandbox-hours a day (`TRIAL_SANDBOX_S_PER_DAY`), after which builds wait for 00:00 UTC.
- **Kill switch:** `TRIAL_BUILD=off` in `trial.env`, then recreate `trial-worker` (`up -d --force-recreate`; a plain
  restart keeps the old settings): our sandboxes are killed, waiting and
  running builds fail ("switched off"), the heartbeat goes, the offer disappears; the talk-only trial keeps working.

## Consequences
- Nothing generated runs on our VM; the worker does only HTTPS and holds at most a 50 MB tarball in memory (100 MB
  unpacked); sandbox output is capped inside the sandbox, and changed files are read only once their sizes fit.
- One worker at a time (a lock held for its life), so an overlapping restart can't fail a live build.
- "Talk first" means a waiting talk answer gets the slot before the next build call; a build call already underway
  (up to 3 minutes) is not interrupted, so a talk message sent then may be told "busy, try again in a minute".
- **Private demo repos are not visible to visitors**: adding them as collaborators would need Administration, which the
  owner ruled out. They see the outcome in chat (PR number, files, tests, the self-check excerpt); the owner can add a
  visitor by hand. Phase 2 (their own public repo via a `hagency-trials` fork, cross-repo PR) is a separate ADR.
- **Deviations from the draft, on purpose:** no Matrix accounts for `@trial-builder` / `@trial-reviewer` and no
  `checkers_for` routing: the build and its review are self-contained in the worker and relayed by @pm, so `loop.py`
  is untouched and the worker holds fewer secrets. Per-IP and per-email-domain caps are not possible from Matrix events
  (no IP), so the per-GitHub-account caps and the 30-day age carry the abuse limit.
- Prompt injection's worst case: a bad diff in a demo repo, shown as a PR, tested from the pristine copy with the fixed
  command. Changed source can still make that command pass dishonestly (e.g. exiting 0 at import): the reviewer and the
  person who merges are the last check, and the PR says it was written by AI in a sandbox.
- Before launch (R6): an end-to-end run on a real demo repo (claim, PR, review, ready), a live check that the egress probe
  fails closed on E2B, and measuring the chosen model's tool calling on the seeded issues (`TRIAL_BUILD_MODEL`), so a
  weak model isn't mistaken for a broken flow.
- What the owner creates is listed in `docs/TRIAL_BUILDER_SETUP.md`.
