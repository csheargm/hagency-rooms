# GitHub setup for the agent loop

Do this once per person who runs the loop. It takes about 5 minutes. Every person's agent
needs **its own GitHub account**: GitHub won't let an author approve their own PR, so this is
the rule that makes one agent review another agent's work.

You need: the `gh` CLI, owner (admin) access to the target repo, and one GitHub account per
agent. These can be your own account plus a second account you control.
Each room works on its own repo (`@pm this room works on owner/name`). Locally (no GitHub App), rooms
pick from the repos this server allows (set in `local/local.env`; the names below stand for your own
accounts; with the App, any repo the asker can write to, docs/GITHUB_APP.md): `LOOP_REPO` (default `your-name/hagency-demo-sandbox`) plus any in
`HAGENCY_REPOS="owner/a owner/b"`. Run the commands below for each repo with
`--repo owner/name`; the console's **Check it's ready** button runs step 4 for a room's repo.

> Never paste a password or token into chat, a file or this repo. Every sign-in below uses
> GitHub's browser device flow. The token stays in that agent's own `.gh/<agent>/hosts.yml` (folder mode 700,
> git-ignored), not the macOS keychain: gh's keychain entry is shared by every gh folder, so with it a
> second agent's sign-in could make the first one act as the second.

## 1. Sign in each agent's GitHub identity

On macOS and Linux, the connect line offers this as soon as the agent connects: answer yes and approve in
your browser (`client/github_signin.py`). Otherwise (Windows, or you said no), run this in your own
terminal once per agent, in `~/.hagency/app`, replacing `<agent>` with `anna-agent`, `betty-agent`, … (it's the
agent's Matrix name):

```bash
mkdir -p .gh/<agent> && chmod 700 .gh/<agent> && GH_CONFIG_DIR=$PWD/.gh/<agent> gh auth login --hostname github.com --git-protocol https --web --insecure-storage
```

The command prints a one-time code. Press Enter and open github.com/login/device in a browser
where **that agent's account** is signed in. Paste the code, then click Authorize.
If you're signed in to several accounts, use the browser profile that has the right one.

## 2. Give each account Write access

In the target repo, open **Settings → Collaborators → Add people**. Add every agent account that
isn't the owner, and give it the **Write** role. Then accept the invite from that account, either
by email or at github.com/notifications.

## 3. Make GitHub the merge gate

Run this as the repo owner. It uses your normal `gh` login, not an agent's:

```bash
.venv/bin/python bin/github_setup.py ruleset
```

The script creates the `agent-ok` label (the loop and `@pm`'s planning use it) and applies
[`github-ruleset.json`](github-ruleset.json) to the default branch.
Running it again changes nothing. The ruleset does five things:
- changes must go through a pull request with **1 approving review**;
- a new push dismisses earlier approvals;
- whoever pushed last can't be the one who approves;
- force-pushes and branch deletion are blocked;
- **nobody can bypass it**.

To do this by hand instead, open **Settings → Rules → Rulesets → New branch ruleset** and set the
same options.

Without this ruleset GitHub never reports a PR as `APPROVED`. The loop then stops and says so;
it does not merge.

## 4. Check

```bash
.venv/bin/python bin/github_setup.py check
```

To check other agents, add their names: `check anna-agent betty-agent carol-agent`.
A ready setup looks like this:

```
anna-agent   your-account             write
betty-agent  second-account           write
✓ your-name/hagency-demo-sandbox ready for the loop
```

Each `✗` line names one problem and how to fix it. The possible problems are:
- an agent isn't signed in;
- an account doesn't have Write access;
- two agents share one GitHub account;
- the review rule is missing.

## 5. Run

With `local/start.sh` running, type `!level assist` in the room. Agents build and review, and you
merge. Type `!level autonomous` to let the builder agent merge once GitHub approves, and
`!level off` to stop.
