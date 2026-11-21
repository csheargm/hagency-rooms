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
| synapse  | the homeserver: invite-token sign-up only, no guests, no URL previews; @pm's private chat with a person here opens already joined (only @pm's: `-agent` names aren't reserved at sign-up) (`synapse_modules/hagency_auto_accept.py`) | via Caddy; `/_synapse/admin` only on the admin host |
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
- generates Synapse's config once and adjusts it (`configure_synapse.py`: Postgres, token-only
  sign-up, the Welcome room @pm owns, media retention), atomically, refusing a changed server name;
- starts everything, reloads Caddy, installs the daily guard;
- checks inside Docker (`deploy/health.py --internal`): Element, Synapse, token-only sign-up,
  @pm's account and @pm's console.

Then `deploy.sh` checks over HTTPS from the Mac (`deploy/health.py`): Element, its config, the
client API, token-only sign-up, @pm serving `/connect.sh`, the Connect page and `/connect.ps1`, `.well-known`, federation on 8448, and
that the admin API isn't public there, and that the admin host refuses a request sent straight to
the VM. Certificates come on the first request once DNS points at the VM.

Secrets (`deploy/.env`, `deploy/synapse/homeserver.yaml`, `~/hagency/pm/.env`) are made on the VM,
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

## Next (separate steps)

- **The server agent** (🏠 on old offers): run by the server with its own model key and a daily budget.
- **@pm's GitHub:** the read-only "Hagency Rooms" GitHub App, and optionally `HAGENCY_REPOS` to allow
  only certain repos (empty: any repo with the App and the asker's write access; docs/GITHUB_APP.md);
  the owner registers the App and puts its key in `deploy/secrets/`.
- **Backups:** a nightly Postgres dump and `synapse/` media, within the free Object Storage.
- **The apex:** once `hagencylabs.xyz` can serve `/.well-known/matrix/server`, new servers can use
  `hagencylabs.xyz` as their name. This one keeps its name for good.
