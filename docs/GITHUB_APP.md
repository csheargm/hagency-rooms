# @pm's GitHub App ("Hagency Rooms")

On the public server @pm has no `gh` login. It reads GitHub (whether it can see a repo when a room
binds one, issues, PRs, checks, readiness, the Pages link) as a **read-only GitHub App** instead.
Each repo's owner installs it on their repo with one click. People's agents keep their own GitHub
accounts on their own computers. The App never pushes, opens PRs or merges.

Without `GITHUB_APP_ID`, @pm reads with the host's own `gh` login, as before. That is how local dev
works (`local/start.sh`).

## 1. Register the App (the owner, in the browser, once)

1. Open **https://github.com/settings/apps/new**. To make an organization own it, open
   `https://github.com/organizations/<org>/settings/apps/new` instead.
2. **GitHub App name:** `Hagency Rooms`. GitHub turns the name into the App's *slug*, the part
   after `github.com/apps/`. For this name it is `hagency-rooms`. If GitHub says the name is taken,
   pick another one and note its slug.
3. **Description:** `Read-only access for @pm, the project coordinator in Hagency Rooms.`
4. **Homepage URL:** `https://www.hagencylabs.xyz`
5. **Identifying and authorizing users:** leave **Callback URL** empty. Leave **Request user
   authorization (OAuth) during installation** unticked. Tick **Enable Device Flow**: that is how
   people connect their GitHub account (Connect GitHub, below). An App that already exists: open
   its settings page (**https://github.com/settings/apps/hagency-rooms**), tick **Enable Device
   Flow** under *Identifying and authorizing users*, and click **Save changes**.
6. **Post installation:** leave **Setup URL** empty.
7. **Webhook:** untick **Active**. @pm polls for now, so it needs no webhook. If you add webhooks
   later, the URL will be `https://rooms.hagencylabs.xyz/github/webhook` (not served yet) and the
   webhook secret goes next to the key (step 3), never in the repo.
8. **Permissions → Repository permissions.** Set each of these to **Read-only** and leave all
   others at *No access*:
   | Permission        | Why @pm reads it                                        |
   |-------------------|---------------------------------------------------------|
   | Metadata          | mandatory: the repo itself, its default branch, its rules |
   | Contents          | the repo's files and branches                           |
   | Issues            | the `agent-ok` queue, labels, and issue states          |
   | Pull requests     | PR state, review decision, mergeability                 |
   | Checks            | CI results on a PR                                      |
   | Commit statuses   | CI results reported as statuses                         |
   | Pages             | the "try it" link once the work is merged (optional)    |

   Leave **Organization permissions** and **Account permissions** at *No access*.
9. **Subscribe to events:** none (the webhook is off).
10. **Where can this GitHub App be installed?**
    - **Only on this account:** only your own repos (or your org's) can install it. This is safer
      while the server is new, but nobody else can bring their repo.
    - **Any account:** anyone can install it on their repos. It is still read-only. A room can then
      choose any repo that has the App and that the asker's connected GitHub account can write to
      (unless you restrict it with `HAGENCY_REPOS`, below). Pick this once other people bring their
      own projects.

    Start with **Only on this account**. You can change it later under *Advanced → Make public*.
11. Click **Create GitHub App**. On the App's **General** page, write down the **App ID**, a
    number near the top, and the **Client ID** (it starts with `Iv`). Neither is secret. Don't
    generate a client secret: @pm doesn't need one.
12. Under **Private keys**, click **Generate a private key**. Your browser downloads
    `hagency-rooms.<date>.private-key.pem`. This file is the App's password: never paste it into
    chat or commit it.

## 2. Install it on a repo (each repo's owner, one click)

Open `https://github.com/apps/hagency-rooms/installations/new` (with your slug), choose the
account, then pick **Only select repositories** and choose the repo. You can also skip this step
and wait: when a room picks a repo that doesn't have the App yet, @pm replies with this link, and
the console shows it too. @pm checks again every 30 seconds for 10 minutes and finishes setting up
the room once the App is installed.

## 3. Put the key and settings on the VM (the operator)

From your computer:

```sh
ssh hagency-vm 'mkdir -p -m 700 ~/hagency/app/deploy/secrets'
scp ~/Downloads/hagency-rooms.*.private-key.pem hagency-vm:hagency/app/deploy/secrets/github-app.pem
ssh hagency-vm 'chmod 600 ~/hagency/app/deploy/secrets/github-app.pem'
rm ~/Downloads/hagency-rooms.*.private-key.pem        # the VM's copy is the only one
```

Then in `~/hagency/app/deploy/.env` on the VM, set these. Use plain values: no quotes or spaces.

```
GITHUB_APP_ID=<the App ID from step 1.11>
GITHUB_APP_SLUG=hagency-rooms
GITHUB_APP_CLIENT_ID=<the Client ID from step 1.11>
HAGENCY_REPOS=
```

**Which repos.** Leave `HAGENCY_REPOS` empty to let a room's owner or a maintainer choose any repo.
Two checks still hold: the Hagency Rooms App must be installed on the repo, and their connected
GitHub account must have write (or maintain, or admin) access to it. To allow only certain repos,
list them (`HAGENCY_REPOS=owner/repo,owner/other-repo`); any other repo is then refused with "This
server only allows certain repos". When a room can't use a repo, @pm names the one missing step:
the App's install link, the GitHub account that can't write, or the server's list.

Without `GITHUB_APP_CLIENT_ID` nobody can connect a GitHub account, so nobody can choose a room's
repo on the server.

Then deploy as usual (`deploy/deploy.sh` from your computer).

## How deploy picks it up

- `deploy/bootstrap.sh` creates `deploy/secrets/` (mode 700).
- `deploy/envfile.py check` runs on the VM before @pm stops. If `GITHUB_APP_ID` is set, it refuses
  the deploy unless `secrets/github-app.pem` is a PEM private key in a plain file owned by the deploy
  user (`PM_UID`) with mode 600. A bad key stops the deploy before anything changes, and @pm keeps
  running.
- `deploy/docker-compose.yml` mounts `deploy/secrets/` **read-only** into @pm's container at
  `/run/hagency-github/`. It passes `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_CLIENT_ID`,
  `HAGENCY_REPOS` and `GITHUB_APP_PRIVATE_KEY_PATH=/run/hagency-github/github-app.pem` through to @pm.
- `deploy/envfile.py` checks the four values (digits, a slug, a Client ID starting with `Iv`,
  `owner/name` pairs). All four are optional, so an empty `GITHUB_APP_ID` leaves @pm without GitHub
  reads on the server.
- `deploy/secrets/` is git-ignored. `deploy.sh` replaces only the commit's files, so the key
  survives deploys.

## What @pm does with it (github_app.py, github_app_reads.py)

- It signs a JWT with the key (RS256, valid 9 minutes, `iss` = App ID). It sends that JWT only to
  look up a repo's installation (`GET /repos/{owner}/{repo}/installation`) and to get an
  installation token for **that one repo**. The token is cached until 5 minutes before it expires.
- @pm's own reads (`github.run_json` with no agent) go through the App when `GITHUB_APP_ID` is
  set. That covers issue lists and views, PR views (GraphQL, for the review decision), labels, and
  `repos/...` paths. Any other command, including every write, is refused. Agents' calls always
  use their own `gh` identity.
- The key file must be mode 600. Errors and logs never contain the key, the JWT or a token.
  Redirects are not followed, so a token never goes to another host.

## Rotating the key

Generate a new key on the App's page (step 1.12), replace `secrets/github-app.pem` on the VM the
same way, deploy, then delete the old key on the App's page.

## Connect GitHub: who may choose a room's repo (ADR 0007)

Only people with write access to a repo (write, maintain or admin) can bind a room to it. On the
public server @pm learns which GitHub account is yours with GitHub's device flow:

1. In the room's **Hagency** panel, the **GitHub** card: click **Connect GitHub**. (Or say
   "connect my github" to @pm; in a project room @pm sends the code to your private chat.)
2. The card and your private chat with @pm show a code and **https://github.com/login/device**.
   Open the link, enter the code, and approve **Hagency Rooms**. The code works for 15 minutes.
3. The card now says **GitHub: @your-login ✓**. **Disconnect GitHub** (or "disconnect my github")
   forgets it.

@pm uses the token GitHub gives it once, to ask who approved (`GET /user`), and drops it. It keeps
only your login and GitHub account id (`data/github-links.json`). When you choose a repo, @pm reads
your access as the App (`GET /repos/{owner}/{repo}/collaborators/{login}/permission`) and checks the
account id GitHub reports, so a renamed username taken by someone else doesn't pass. The owner must
tick **Enable Device Flow** on the App (step 1.5) and the operator must set `GITHUB_APP_CLIENT_ID`
(step 3). Local dev (no `GITHUB_APP_ID`) keeps checking your agent's own `gh` login.
