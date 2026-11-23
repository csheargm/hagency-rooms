# 0014: A one-step invite link for a project room

## Status
Accepted, 2026-11-22. Builds on 0013 (open sign-up). Project rooms stay Invite only.

## Context
Inviting a newcomer to a private project room took two steps: they made an account, then told the
room's owner their full Matrix ID, and the owner invited it. The owner asked for one link that does
both. Synapse has no "register and join this room" link, so the hard part is tying the account that
signs in to the link that was opened, without a new way to take over an account, reveal a private
room, or redirect anywhere.

Options weighed:
- **A Synapse module on registration** (`check_registration_for_spam`, `on_user_registration`): it
  sees the new account but not which link the browser came from; Element's sign-up sends no extra
  field. It also covers only new accounts, not people who already have one.
- **Read Element's session from the browser** (our pages share Element's origin): Element keeps the
  access token encrypted in IndexedDB, in a format that changes between releases. Fragile, and
  reading another app's secret is the wrong habit.
- **Mint a login token for Element** (`login_via_existing_session`): a server-wide switch that lets
  any session make new sessions; with UI auth it needs the password again, and SSO accounts can't
  give one. Not worth it for this.
- **Sign in on our own invite page, against our own homeserver, then hand over to Element.** Chosen.

## Decision
The link is `https://<rooms>/invite#<token>`. The token is in the fragment: browsers never send it to
a server, so it is not in Caddy's or @pm's request logs, and it never leaks in a Referer.

1. **Making a link.** A room's owner or a maintainer (power level 50 or more, or a room v12 creator;
   `room_state.is_moderator`) says `@pm invite link` (optionally "for 3 days", 1 to 30; 7 by default)
   in a project room, or clicks "Make an invite link" under "Who can join" in the Hagency panel. @pm
   answers in its private chat with that person, never in the room: the link, how long it works, and
   how to cancel it (`cancel invite link <id>`; the id is a separate short name, not the token).
   `@pm cancel invite links` in the room cancels all of that room's unused links. The Welcome room,
   the Lobby and private chats can't have links.
2. **Opening it.** The page (`server/invite_page.py`, `static/invite.*`) posts the token to
   `POST /invite/check`. A valid, unused link answers with the room's name and who invited them; a
   used, expired or cancelled one says exactly that and nothing about the room; an unknown token
   says "this link doesn't work".
3. **Signing in, on the page, straight with our homeserver** (the browser talks to Synapse; @pm never
   sees a password or a session):
   - **Google or GitHub**, whichever Synapse lists in `GET /login`: the page goes to Synapse's
     `/login/sso/redirect/<idp>?redirectUrl=https://<rooms>/invite`. That address is already in
     `sso.client_whitelist` (0013: the rooms origin), so there is no "continue?" page. Synapse makes the
     account if it's new (with 0013's rules: no existing account is ever linked) and comes back with a
     one-time `loginToken`, which the page swaps for a session. The invite token waits in this tab's
     `sessionStorage` meanwhile, never in a URL. **For SSO this is one step for a newcomer.**
     The page takes a `loginToken` only in the tab that started that sign-in (it marks the tab when
     the button is clicked), so a crafted link carrying someone else's login token is ignored.
   - **Username and password**, for people who have an account: the page posts them to Synapse's
     `/login`, never to @pm.
   - **Email sign-up can't be one step.** Synapse's email sign-up is Element's form, which knows
     nothing of our link, and we don't build a second sign-up form. The page says so: "Create your
     account in the new tab, then come back here and sign in". The link is still unused, so they sign
     in with the new password and are in. While sign-up is token-only (the owner hasn't filled the
     keys), the page reads Synapse's own registration flows and says new accounts need a sign-up code
     from the server's owner.
4. **Joining.** With that session the page asks Synapse for an OpenID token (it proves only who the
   user is, and lasts an hour) and posts it with the invite token to `POST /invite/redeem`. @pm checks
   it with our homeserver (`console.openid_user`: people on this server only, as the console), then
   uses the link (checked and marked used with no `await` in between, so two tabs can't both use it),
   and invites them, but only while the link's maker is still in the room as an owner or maintainer
   (a power level left behind by someone who left counts for nothing; `check` then names no room).
   The page joins the room with its session, so `hagency_auto_accept.py` stays
   limited to @pm's private chats. If the server refuses the invite, the link goes back to unused, unless it was cancelled meanwhile;
   if the server doesn't answer, the invite may have gone through, so the link stays used. Someone already
   in the room is sent there without using the link. @pm says hello in the room, naming who invited them.
5. **Into Element.** The page signs its own session out (`/logout`: the device is gone) and opens
   Element at `#/room/<id>`, the only place it ever sends anyone (Element's origin is fixed in the
   page; the room ID comes from @pm, encoded). If Element in this browser has no session and they
   came by Google or GitHub, the page sends them through the same provider once more with Element as
   the return address, which the provider normally passes through without asking; Element finishes
   that sign-in itself. This uses Element's own SSO hand-back (`mx_sso_hs_url` in localStorage, the
   key Element sets before any SSO), so if a future Element renames it, people see Element's sign-in
   page instead, already pointed at the room. A password user without a session signs in to Element
   once, and Element opens the room.

### Storage and limits (`server/invite_links.py`)
- 32 random bytes per token, kept only as its SHA-256; `data/invite-links.json` holds the hash, room,
  inviter, expiry and what happened to it, never the token. Used, cancelled and expired links are
  dropped after 30 days.
- Lookups (`check` and `redeem` together) are limited per client address (Caddy's
  `X-Forwarded-For`, as the Connect page) and for everyone together, with `fed_openid.may_try`.
- At most 20 unused links per room, and 10 new links per person a day.
- No token in a log line, a chat message other than the creator's private chat, or an error: tested.

## What this protects, and what is left
- Takeover: identity comes only from our homeserver (a session the person made with their own
  password or provider). The page never sets anyone else's session; Synapse's SSO never links an
  existing account (0013). The only new power is "join this one room once", and only for the account
  that signed in.
- Disclosure: without a valid unused token, the page and the API say nothing about any room.
- Redirects: the SSO return address and the Element address are fixed; nothing comes from the URL.
- Left: a link is a bearer credential for one join. Whoever has it first gets in, so owners send it
  privately; cancelling is one sentence. People from other servers can't use a link (their OpenID
  isn't checked here); they're invited by ID as before. Email newcomers take two steps, as above.

## Consequences
- Caddy sends `/invite` and `/invite/*` to @pm. The page's Content-Security-Policy allows requests to
  this origin and to the public homeserver only.
- Live sign-up is token-only until the owner fills 0013's keys: links work for everyone who has an
  account; for newcomers the page explains they need a sign-up code for now.
