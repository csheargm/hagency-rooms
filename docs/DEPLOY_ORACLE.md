# Hagency Rooms on Oracle Cloud

The public server runs the shared side. Each person's agent runs on that person's own computer
and connects to the server over Matrix, so model keys, GitHub accounts and tools never live on
the server. The site is not on the VM: www.hagencylabs.xyz is GitHub Pages
(`.github/workflows/pages.yml` publishes `site/`).

```
Oracle VM (public, Always Free)                Each person's computer
───────────────────────────────────────        ─────────────────────────────────────────
rooms.hagencylabs.xyz   Element Web            their agent's connector (connect.py):
  /panel /console /auth/*  @pm's console         agent adapter running Codex or Claude Code
  /connect /connect.sh     pairing + installers  → logs in as @anna-agent on the server
  /connect.ps1             Windows installer
  /connect/start           the Connect page @pm links to (shows the right installer line)
matrix.hagencylabs.xyz  Synapse (443 + 8448)   Element desktop / mobile (or the web)
admin.hagencylabs.xyz   Synapse Admin, behind Cloudflare Access (the only way to /_synapse/admin)
@pm                     welcome, pairing, the console (no model: PM_BRAIN=off)
```

Not on the VM: people's agents, the server agent (not built yet), any GitHub sign-in. The
existing `app.hagencylabs.xyz`, the apex redirect and this Mac's cloudflared tunnel stay as they are.

## What runs (deploy/docker-compose.yml)

| Service  | What | Reachable |
|----------|------|-----------|
| caddy    | HTTPS (Let's Encrypt), routes below; also serves Element's Hagency home page, logo and background at `/hagency/` (`deploy/element_home.py` writes them) | 80, 443, 8448 (the only published ports) |
| element  | Element Web, pointed at this homeserver; its home page is Hagency's three steps (`embedded_pages.home_url`) | via Caddy |
| synapse  | the homeserver: sign-up with an invite token, or with a confirmed email once SMTP is set, or through Google/GitHub once their keys are set ("Sign-up and sign-in" below); no guests, no URL previews; @pm's private chat with a person here opens already joined (only @pm's: `-agent` names aren't reserved at sign-up) (`synapse_modules/hagency_auto_accept.py`) | via Caddy; `/_synapse/admin` only on the admin host |
| synapse-admin | the admin web UI (static), `restrictBaseUrl` = the admin host | via Caddy, admin host only |
| postgres | Synapse's database | an internal network only Synapse is on |
| pm       | @pm (`loop.py`): welcomes newcomers, the console, `/connect`, the Connect page (`/connect/start`), `/connect.sh` and `/connect.ps1` | via Caddy |

Images are pinned in the compose file (Synapse Admin also by digest), so a commit always runs the same versions. @pm runs as
the deploy user (PM_UID in .env, set by bootstrap) from `~/hagency/pm` (that commit's files plus its own state: `.env` with its passwords,
`agents.json`, `data/`); each deploy clears everything else there (`deploy/release.py`: dropped
files, anything planted, the virtualenv). It reads Synapse's registration secret from a read-only
copy in a root-owned folder (`deploy/pm-secret/`), never Synapse's whole config.

## The admin host

`admin.hagencylabs.xyz` is proxied by Cloudflare (orange cloud) and guarded by a Cloudflare Access
policy (the owner's, in Cloudflare). Caddy serves Synapse Admin there and proxies `/_matrix` and
`/_synapse/admin` to Synapse, but only for requests from Cloudflare's published addresses (vendored
in the Caddyfile from cloudflare.com/ips-v4 and /ips-v6, 2026-11-20; update them when Cloudflare's
list changes) that carry a `Cf-Access-Jwt-Assertion` header; anything else gets 403. Caddy does not
verify the token's signature (that needs a plugin or a verifier service plus Access's team domain
and AUD tag): Access itself checks it, and the address filter stops anyone bypassing Cloudflare.
On `matrix.*` and `rooms.*`, `/_synapse/admin` is 404.

The origin certificate (Cloudflare SSL mode Full (strict)) is Caddy's own Let's Encrypt one, by
HTTP-01 through the proxy. Let's Encrypt must reach
`http://admin.hagencylabs.xyz/.well-known/acme-challenge/` past Access: give that path a Bypass
policy in Access (or issue the certificate before Access is switched on), and don't let
"Always Use HTTPS" redirect it before the first certificate exists.

**The owner's admin account:** bootstrap creates `@owner` as a server admin with a random password
that is kept nowhere. Set yours (typed twice, never echoed or stored; other sessions are signed out):

```
ssh -t hagency-vm '~/hagency/app/deploy/invite.sh owner-password'
```

Then sign in at https://admin.hagencylabs.xyz as `owner`. Run it again whenever you want a new password.

## Free-tier guards

- Synapse: uploads up to 20 MB; local media kept 90 days, remote media 30 days (`media_retention`).
- Docker logs rotate (json-file, 3 × 10 MB per service); the journal is capped at 200 MB.
- `hagency-guard.timer` (daily, `deploy/guard.py`): @pm warns the operator (`OPERATOR` in
  `deploy/.env`) when the disk is over 80% or this month's outbound traffic is over 1 TB (vnstat).
  Over 5 TB, media downloads are paused for the rest of the month (`deploy/flags/media-paused`,
  which Caddy checks) and come back by themselves next month. `journalctl -u hagency-guard` shows
  every run.
- Nothing creates Oracle resources; the boot disk stays as it is.

## Once: the VM and DNS (the owner)

1. **VM:** Ubuntu 24.04, Ampere A1 Flex. Oracle's Always Free A1 allowance is now 1,500 OCPU-hours
   and 9,000 GB-hours a month, i.e. **2 OCPU / 12 GB** (checked 2026-11-20 on Oracle's Always Free
   page); a 4 OCPU / 24 GB instance running all month goes past it on a Pay-As-You-Go account.
   Everything here fits in 2 OCPU / 12 GB. Ingress rules for TCP 80, 443 and 8448.
   `ssh hagency-vm` in `~/.ssh/config` on the Mac.
2. **DNS** in Cloudflare, **DNS only** (grey cloud): Caddy gets its own certificates, and
   federation on 8448 can't pass Cloudflare's proxy.

   | Name   | Type | Content       | Proxy    |
   |--------|------|---------------|----------|
   | rooms  | A    | `<public IP>` | DNS only |
   | matrix | A    | `<public IP>` | DNS only |
   | admin  | A    | `<public IP>` | Proxied, with a Cloudflare Access policy |

   `www` points at GitHub Pages (see the Pages settings), not at the VM.

Oracle's host iptables reject inbound ports other than 22, but Docker's published ports go through
Docker's own FORWARD rules, so nothing on the host firewall is changed.

## Deploy (from the Mac, from GitHub only)

```
deploy/deploy.sh            # the tip of master
deploy/deploy.sh <commit>   # any commit that is on master; anything else is refused
```

It checks the commit is on GitHub's master, then on the VM: fetches it from GitHub into
`~/hagency/app` (refusing if code there was edited), stops @pm, copies the commit's files to
`~/hagency/pm`, and runs `deploy/bootstrap.sh`, which:
- installs Docker, Compose and vnstat from Ubuntu's archive, caps the journal;
- creates `deploy/.env` once (mode 600, a random Postgres password; plain data, never sourced);
- generates Synapse's config once and adjusts it (`configure_synapse.py`: Postgres, sign-up
  (token-only, or email-verified: `signup.py`; Google/GitHub: `sso_login.py`), the Welcome room @pm owns, media retention), atomically, refusing a changed server name;
- starts everything, reloads Caddy, installs the daily guard;
- checks inside Docker (`deploy/health.py --internal`): Element, Synapse, that every sign-up flow needs a token or a confirmed email,
  @pm's account and @pm's console.

Then `deploy.sh` checks over HTTPS from the Mac (`deploy/health.py`): Element, its config, the
client API, sign-up needing a token or a confirmed email, @pm serving `/connect.sh`, the Connect page and `/connect.ps1`, `.well-known`, federation on 8448, and
that the admin API isn't public there, and that the admin host refuses a request sent straight to
the VM. Certificates come on the first request once DNS points at the VM.

Secrets (`deploy/.env` with the SMTP and sign-in keys the owner fills, `deploy/synapse/homeserver.yaml`, `~/hagency/pm/.env`) are made on the VM,
mode 600, and are never printed, copied back or committed. Nobody edits code on the server:
change it here, push to master, deploy the commit.

## Invites and the operator

```
ssh hagency-vm '~/hagency/app/deploy/invite.sh token 5'   # a token for 5 sign-ups, valid 7 days
ssh -t hagency-vm '~/hagency/app/deploy/invite.sh admin'  # another admin account (asks for a password)
```

`token` runs in @pm's container: it signs in as `admin-bot` (a server admin @pm made with the
registration secret; its password stays in `~/hagency/pm/.env`) only for Synapse's
registration-token API, and prints the token. People sign up at
`https://rooms.hagencylabs.xyz/#/register` with it, land in Welcome, and @pm offers them
🔌 Bring your own: a one-time code and the Connect page (`/connect/start#CODE`, static, served by
@pm's console; the code is in the fragment, so it never reaches Caddy's or @pm's logs), which
shows the Mac/Linux or Windows line for their computer. To get the guard's warnings, put your account's name in `OPERATOR=` in
`~/hagency/app/deploy/.env` and deploy again.

People from other Matrix servers (e.g. matrix.org) get an agent too, by opening a private chat with
@pm (docs/adr/0003-people-from-other-servers.md). `ALLOW_REMOTE=0` in `deploy/.env` turns that off;
`REMOTE_PER_HOUR=` caps how many of their agents are made per hour (default 10).

## Sign-up and sign-in (docs/adr/0013-open-signup.md)

Out of the box sign-up needs an invite token (above). Three things change that, each set by the
owner in `~/hagency/app/deploy/.env` on the VM and nowhere else (never in git, never in chat):

| Keys in `.env` | What it turns on |
|----------------|------------------|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` (all five) and `HAGENCY_OPEN_SIGNUP=1` (the default) | anyone may create an account with an email address they confirm (a link Synapse emails them); no token needed; password reset by email |
| `HAGENCY_GOOGLE_CLIENT_ID`, `HAGENCY_GOOGLE_CLIENT_SECRET` | "Continue with Google" on the sign-in and sign-up pages |
| `HAGENCY_GITHUB_CLIENT_ID`, `HAGENCY_GITHUB_CLIENT_SECRET` | "Continue with GitHub" |

Each one is independent. Anything missing, partial or malformed leaves that part off, and
`bootstrap.sh` prints which (setting names, never values), e.g. `sign-up by invite token only:
SMTP not ready (SMTP_PASS is empty); sign-in with google on, github off`. Registration is never
opened without verification: password sign-up always needs a token or a confirmed email
(`deploy/health.py` checks it on every deploy).

**Apply** (after editing `.env`; nothing else on the VM is edited by hand):

```
ssh -t hagency-vm 'nano ~/hagency/app/deploy/.env'      # fill the keys; keep mode 600
deploy/deploy.sh                                       # from your computer: re-runs bootstrap, restarts Synapse
```

**Close sign-up again:** set `HAGENCY_OPEN_SIGNUP=0` and run `deploy/deploy.sh`: sign-up is back to
invite tokens only, and email keeps working for password reset. To turn email off entirely, empty
`SMTP_HOST`. To turn off a sign-in button, empty that provider's client ID. (SSO accounts already made
stay; disable one in Synapse Admin.)

### Email (SMTP)

`.env` values are plain (no quotes, `$`, backslashes or spaces). Port 465 uses TLS from the start;
587 or 2525 must offer STARTTLS, or Synapse won't send (never in the clear). `SMTP_FROM` is the
address only; mail goes out as `Hagency Rooms <SMTP_FROM>`. The sending domain must be verified with
the provider (its SPF/DKIM DNS records on hagencylabs.xyz), or the mail lands in spam.

Any SMTP server (your mail host, Fastmail, Google Workspace, Amazon SES, ...):
```
SMTP_HOST=smtp.yourprovider.example
SMTP_PORT=587
SMTP_USER=your-smtp-username
SMTP_PASS=your-smtp-password
SMTP_FROM=no-reply@hagencylabs.xyz
```

A free tier, Brevo (about 300 emails a day free at the time of writing; check their current plan):
add and verify hagencylabs.xyz under Senders & Domains, then SMTP & API → SMTP keys → generate one.
```
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USER=<the SMTP login Brevo shows, e.g. 1a2b3c001@smtp-brevo.com>
SMTP_PASS=<the SMTP key>
SMTP_FROM=no-reply@hagencylabs.xyz
```
The free daily limit is also the sign-up limit: Synapse sends at most 3 confirmation emails per
address, then one every ~8 minutes (`signup.py`), and allows 3 sign-ups per address, then one every
~5.5 minutes.

### Google

Redirect URL: `https://matrix.hagencylabs.xyz/_synapse/client/oidc/callback` (Caddy sends
`/_synapse/client/*` to Synapse).
1. https://console.cloud.google.com → create (or pick) a project, e.g. "Hagency Rooms".
2. Google Auth Platform (APIs & Services → OAuth consent screen) → Get started: app name "Hagency
   Rooms", support email yours, Audience **External**, contact email, agree, Create.
3. Branding: add `hagencylabs.xyz` under Authorized domains. Audience: **Publish app** (in
   "Testing" only listed test users can sign in). The scopes used (openid, email, profile) need no
   Google review.
4. Clients → Create client → Application type **Web application**, name "Hagency Rooms (Synapse)",
   Authorized redirect URIs: the redirect URL above → Create.
5. Copy the Client ID and Client secret into `.env`:
```
HAGENCY_GOOGLE_CLIENT_ID=123456789012-abc123def456.apps.googleusercontent.com
HAGENCY_GOOGLE_CLIENT_SECRET=GOCSPX-...
```

### GitHub

1. github.com → Settings → Developer settings → **OAuth Apps** → New OAuth App (an OAuth App, not
   the GitHub App @pm uses: docs/GITHUB_APP.md).
2. Application name "Hagency Rooms", Homepage URL `https://rooms.hagencylabs.xyz`, Authorization
   callback URL `https://matrix.hagencylabs.xyz/_synapse/client/oidc/callback`; leave Device Flow
   off → Register application.
3. Generate a new client secret; copy it and the Client ID into `.env`:
```
HAGENCY_GITHUB_CLIENT_ID=Ov23li...
HAGENCY_GITHUB_CLIENT_SECRET=<40 characters>
```

How names are chosen: a Google account gets its email's name (anna.lee@gmail.com → @anna.lee), a
GitHub account its login (@octocat). A name already taken (@pm, someone's account, an agent) is never
linked to: Synapse picks @octocat1, @octocat2, ... instead (`allow_existing_users` stays false). A name
ending in `-agent` or `-bot` gets `-g` / `-gh` added, so nobody signing in takes a future agent's name.
Everyone, however they signed up, lands in Welcome with @pm.

## Next (separate steps)

- **The server agent** (🏠 on old offers): run by the server with its own model key and a daily budget.
- **@pm's GitHub:** the read-only "Hagency Rooms" GitHub App, and optionally `HAGENCY_REPOS` to allow
  only certain repos (empty: any repo with the App and the asker's write access; docs/GITHUB_APP.md);
  the owner registers the App and puts its key in `deploy/secrets/`.
- **Backups:** a nightly Postgres dump and `synapse/` media, within the free Object Storage.
- **The apex:** once `hagencylabs.xyz` can serve `/.well-known/matrix/server`, new servers can use
  `hagencylabs.xyz` as their name. This one keeps its name for good.
