# 0015: The home page's steps ① and ③ done on a page, not through the chat

## Status
Accepted, 2026-11-22. Builds on 0002 (bring your own agent), 0008 (agent kinds) and 0014 (invite links:
its sign-in is reused as it is).

## Context
Element's home page (`deploy/element_page/home.html`) promised "three steps, one click each", but
step ① only opened the chat with @pm: there the person clicked 🔌 Bring your own, waited for a message,
and followed its link to the Connect page. Step ③ was plain text (Room info → Invite). The owner asked
for buttons that get each step done right there.

The home page is inlined by Element through its sanitiser: no script runs in it, so a button can only
be a link. Whatever a button opens must find out who the person is by itself.

Options weighed:
- **Keep the chat, deep-link a message**: still the detour, and @pm's answer arrives asynchronously.
- **Read Element's session** from the same origin: rejected in 0014 (encrypted, release-specific,
  reading another app's secret).
- **The Hagency panel (a widget) on the home page**: widgets live in rooms, not on the home page.
- **A page of ours that signs the person in with our homeserver, as /invite does.** Chosen: 0014 already
  built and reviewed that sign-in (Google, GitHub or a password, straight to Synapse; @pm sees only an
  OpenID token it checks with Synapse).

## Decision
Two pages on @pm's console, under paths Caddy already sends to @pm (`/connect/*`, `/invite/*`), opened
from the home page in a new tab (`target="_blank"`), so Element stays open:

1. **Get your agent, `/connect/new`** (`server/agent_page.py`, `static/agent.html`, `agent.js`).
   Sign in; the page asks `POST /connect/new/me` what the person has (their first agent's name,
   whether it has connected, whether they may still choose its AI: `console_agents.first_pick`, the
   same rule as My Hagency), shows the AI picker (the kinds from `agent_kinds.kinds()`, a model for
   Ollama or a remote model, the service's address for a remote one; never an API key: the picker's
   JSON goes through `pm_kinds.from_panel`, which refuses keys), and on "Get my command" posts
   `POST /connect/new/code`. @pm then does exactly what 🔌 does (`pm_first.bring_own`): it sets the
   first agent up (`remote_agent.provision`, one at a time per person, shared with 🔌 and Add an agent)
   and issues a code with `remote_agent.CODES.issue(user)` for the user the homeserver vouched for:
   their older codes stop working, the code works once, for the same 30 minutes, and only its hash is
   kept. The code is in the reply's body only (never logged). The page then opens the Connect page,
   `/connect/start#CODE&kind=KIND`, the same page @pm's message links to, with the copyable line for
   macOS, Linux and Windows. Once the first agent has connected it keeps its AI (as in the chat), and
   the page gives a new code to connect it on another computer. Someone who removed their first agent
   is sent to My agents (a new agent needs a new name there).
2. **Make an invite link, `/invite/new`** (`server/invite_new_page.py`, `static/invite_new.*`). Sign in;
   `POST /invite/new/rooms` lists the project rooms they are in as an owner or a maintainer (the rule of
   `@pm invite link`: `pm_invite.link_refusal`, now shared by the chat and this page); `POST
   /invite/new/make {room, days}` makes the same link (`invite_links.create`, 1, 3, 7 or 30 days, the
   same per-room and per-person limits) and returns it to that page only. Not in a chat: the page shows
   it with a Copy button and how to cancel it ("cancel invite link <id>" to @pm). The id is logged,
   never the token.
3. **Shared sign-in.** `static/signin.js` (served at `/auth/signin.js`) and `server/signin_page.py`
   hold what the invite page did inline: SSO buttons, the one-tab `loginToken` check, the password form,
   the OpenID token, the page CSP (`connect-src` this origin and the homeserver only, no frames, no
   forms) and `verified()`. The new pages sign their own session out right after taking the OpenID
   token: they keep nothing but that token, in memory, for at most its hour. A Google or GitHub return
   counts only when it carries the random nonce this tab stored when its button was clicked (the return
   address is `<page>?sso=<nonce>`; Synapse adds `loginToken` to it), not merely a recent click (GPT
   review): a crafted link with someone else's login token can't sign this tab in as them. This also
   applies to /invite.
4. **Limits.** The new pages' calls count per client address and for everyone together
   (`signin_page.asks`, 30 and 600 per 10 minutes, as invite links), before the OpenID check, so a flood
   of made-up tokens never reaches Synapse.
5. **The home page.** ① is a button to `/connect/new`; ② keeps its two buttons, in plainer words; ③ has
   a "Make an invite link" button and keeps one line for people on other servers (Room info → Invite).
   The links are relative, so the page stays free of hosts (its test forbids `http` in it). @pm's chat
   stays the fallback for both.

## What this protects, and what is left
- A code is still bound to the account that signed in, which only our homeserver can vouch for
  (`console.openid_user`: people on this server only). Nothing on the page names whose code to make.
- The address the page moves to is built only from a code and a kind of the exact shapes @pm makes;
  the only places these pages send anyone are our homeserver's SSO and the Connect page.
- People from other Matrix servers can't use these pages (their OpenID isn't checked here, as in 0014);
  they use @pm's chat as before.
- As in 0014, if @pm is down at the moment Google or GitHub hands back, Caddy's error line for the 502
  includes `?loginToken=...` (one use, about two minutes, on the VM only). Accepted, as there.
- Signing in again: Element's own session isn't reused (0014), so a password user types it once more;
  Google and GitHub usually pass straight through.
- Not tested here: a real install from the page's line on a real computer (the line and the code are
  the ones the chat flow gives; the tests run connect.py against @pm's real `/connect` answer with the
  network faked).

## Consequences
- New routes on @pm: `GET /connect/new`, `/connect/new/page.js`, `POST /connect/new/me`, `/connect/new/code`,
  `GET /invite/new`, `/invite/new/page.js`, `POST /invite/new/rooms`, `/invite/new/make`, `GET /auth/signin.js`.
  No Caddy change. `deploy/health.py` checks the pages and the home page's buttons on every deploy.
