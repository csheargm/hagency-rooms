# Setting up trial builds

Builds let anyone signed in here watch the whole build flow from the **Server agent** card: it fixes a seeded issue in a
Hagency demo repo, inside an E2B sandbox, runs the tests in a second sandbox with no network, opens a pull request
with an "AI self-check" comment, and @pm says **Ready: a person merges**. Why it is built this way:
[ADR 0027](adr/0027-trial-builds.md).

**It stays off until you finish every step below.** Until then the `trial-worker` container exits at once and
nothing changes for anyone. The server agent (ADR 0025, `OLLAMA_API_KEY` in `deploy/.env`) must be on too:
the **Try a build** button sits on its card in My agents (My Hagency).

You create five things: an E2B account with prepaid credit, an E2B template, a GitHub organisation with demo repos,
a GitHub App, and one settings file on the server.

## 1. E2B: an account with prepaid credit, no card

1. Go to <https://e2b.dev> and click **Sign up**. Use an address only for this, so the account holds nothing else.
2. In the dashboard, open **Billing**. Buy prepaid credit (about $10 covers hundreds of builds; a build costs about $0.02).
3. On the same page, make sure no card stays on file and nothing tops up by itself: remove the card after paying if
   E2B asked for one, and turn off any automatic top-up or pay-as-you-go. With no card on file, the credit is the
   hard spending limit.
4. Open **API Keys** and click **Create key**. Name it `hagency-trial`. Copy the key (it starts `e2b_`); it goes in
   `trial.env` in step 5. Never paste it in chat or in `deploy/.env`.

## 2. E2B: the sandbox template

The template is the computer each sandbox starts as: Python 3.12, pytest and curl
(`deploy/trial-template/e2b.Dockerfile`). E2B builds templates with its command-line tool, from your own computer:

```bash
npm install -g @e2b/cli          # once
e2b auth login                   # opens the browser: sign in with the E2B account from step 1
cd deploy/trial-template
e2b template build --name hagency-trial-python
```

The name you build it as goes in `TRIAL_E2B_TEMPLATE` (default `hagency-trial-python`). You can see it in the E2B
dashboard under **Templates**.

## 3. GitHub: the `hagency-trials` organisation and its demo repos

1. On github.com, click your picture, **Your organizations**, **New organization**, the **Free** plan. Name it
   `hagency-trials` (or your own name; it goes in `TRIAL_ORG`).
2. In the organisation, open **Settings**:
   - **Actions** → **General** → **Policies**: choose **Disable actions** for all repositories. Nothing a build writes
     can ever run on GitHub.
   - **Member privileges**: **Base permissions** = **No permission**; under **Repository creation**, untick both.
   - **Secrets and variables**: leave empty. The organisation holds no secrets.
3. Make the template repo: **Repositories** → **New repository**, name `trial-template`, **Private**, tick **Add a README**.
   Add a small project whose tests fail until a seeded issue is fixed, for example:
   - `src/__init__.py` (empty) and `src/calc.py`: `def add(a, b): return a - b`
   - `tests/test_calc.py`: `from src.calc import add` and `def test_add(): assert add(2, 3) == 5`
   - The template's tests live in `tests/` and must pass once the issue is fixed with the fixed command (pytest on
     `tests/` with no config read from the repo: no `pytest.ini`, `pyproject.toml` settings or plugins), needing nothing
     beyond pytest (test sandboxes have no network).
     A build passes only if at least one test passed and none failed.
   Then **Settings** → tick **Template repository**.
4. Make the demo repos from it: on `trial-template`, click **Use this template** → **Create a new repository**, owner
   `hagency-trials`, name `demo-1`, **Private**. Repeat for `demo-2` and `demo-3` (their names go in `TRIAL_REPOS`).
5. Seed each demo repo with issues a model can solve: **Issues** → **New issue**, for example "add() subtracts:
   add(2, 3) gives -1; it must give 5", and add the label **`agent-ok`** (create it under **Labels** the first time).
   Only open issues labelled `agent-ok` are worked on. A few per repo is plenty.
6. Optional: a **ruleset** on each demo repo's default branch (Settings → Rules) that requires a pull request, so even
   a person merges only through one.

## 4. GitHub: the "Hagency Trial Builder" App

1. In the organisation: **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**.
2. **GitHub App name**: `Hagency Trial Builder` (add a suffix if taken). **Homepage URL**: your Rooms address.
3. **Webhook**: untick **Active** (it needs none).
4. **Permissions** → **Repository permissions**, exactly these, everything else **No access**:
   - **Contents**: Read and write
   - **Issues**: Read and write
   - **Pull requests**: Read and write
   - **Metadata**: Read-only (GitHub sets it)
   Never give **Workflows**, **Administration**, **Actions** or **Secrets**. **Organization permissions** and **Account
   permissions**: all **No access**.
5. **Where can this GitHub App be installed?**: **Only on this account**. Click **Create GitHub App**.
6. On the App's page, note the **App ID** (a number): it goes in `TRIAL_APP_ID`.
7. Under **Private keys**, click **Generate a private key**. A `.pem` file downloads: it goes on the server in step 5.
8. **Install App** (left) → **Install** on `hagency-trials` → **Only select repositories**: `demo-1`, `demo-2`, `demo-3`.

@pm's own read-only App does not need to be installed there.

## 5. The server: `deploy/trial.env` and the App's key

On the VM, as the deploy user, in `~/hagency/app/deploy` (bootstrap.sh made `trial-secrets/`):

```bash
cp trial.env.example trial.env && chmod 600 trial.env
nano trial.env                   # fill in the values below
nano trial-secrets/trial-app.pem # paste the .pem from step 4.7
chmod 600 trial-secrets/trial-app.pem
sudo docker compose --env-file .env up -d --force-recreate trial-worker   # after every trial.env change
sudo docker compose --env-file .env logs trial-worker   # "trial builds on: 3 demo repo(s) in hagency-trials"
```

What goes where:

| Setting | File | Value |
|---|---|---|
| `E2B_API_KEY` | `deploy/trial.env` | the key from step 1.4 |
| `TRIAL_E2B_TEMPLATE` | `deploy/trial.env` | the template name from step 2 |
| `TRIAL_APP_ID` | `deploy/trial.env` | the App ID from step 4.6 |
| the App's private key | `deploy/trial-secrets/trial-app.pem` (mode 600) | the `.pem` from step 4.7 |
| `TRIAL_ORG`, `TRIAL_REPOS` | `deploy/trial.env` | `hagency-trials`, `demo-1,demo-2,demo-3` |
| `OLLAMA_API_KEY` | `deploy/trial.env` | an Ollama Cloud key (the same as `deploy/.env`'s, or one of its own) |
| `TRIAL_BUILD_MODEL` | `deploy/trial.env` | one of `TRIAL_MODELS`; measure its tool calling on your seeded issues first |
| `TRIAL_BUILD_DEMO_USERS` | `deploy/trial.env` | your own Matrix ID, so you can always demo (bypasses the server's daily cap) |
| server caps (`TRIAL_BUILDS_PER_DAY` 20, `TRIAL_BUILD_QUEUE` 5, …) | `deploy/trial.env` | the defaults are the owner's decisions |
| a person's caps (free: 3 a day, 10 ever) | `server/trial_tiers.py` (their tier) | a paid tier can raise them later |
| nothing new | `deploy/.env` | @pm never gets any of the above |

## Checking it

1. In Element, as a test account with GitHub connected (an account at least 30 days old): open
   your chat with Product Manager → My Hagency → My agents → **Server agent** card → **Try a build**. (Owning an
   agent doesn't matter: anyone who passes the gate can try.)
2. You get "queued: you're #1", then "started", then "done: pull request #N … Ready: a person merges".
3. On github.com, the PR in the demo repo has the label "written by AI in a sandbox" and an "AI self-check" comment.
   Merge or close it yourself: the trial never merges.

## Turning it off

In `deploy/trial.env` set `TRIAL_BUILD=off`, then `sudo docker compose --env-file .env up -d --force-recreate
trial-worker` (a plain `restart` would keep the old settings: Compose reads `trial.env` only when it creates the
container). Our
sandboxes are killed, waiting builds are told they were switched off, and **Try a build** disappears; the talk-only
server agent keeps working. To pause spending at the source, revoke the E2B key in E2B's dashboard.
