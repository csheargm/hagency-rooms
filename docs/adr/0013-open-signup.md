# 0013: Open sign-up with a confirmed email, and sign-in with Google and GitHub

## Status
Accepted, 2026-11-22. Changes the public server's "invite-token sign-up only" (DEPLOY_ORACLE.md).

## Context
Every account on matrix.hagencylabs.xyz needed a registration token from the owner
(`invite.sh token`). That made the owner the bottleneck for every newcomer. The owner weighed three
ways to open it:
- **no checks**: Synapse refuses this unless `enable_registration_without_verification` is set,
  for good reason; bots would fill the server within days;
- **CAPTCHA**: needs a Google reCAPTCHA key, sends every visitor's browser to Google, and is
  routinely solved by paid farms;
- **a confirmed email**: one working mailbox per account, rate-limited, and the same email gives
  password reset, which the server lacked.

The owner chose email. In the same decision the owner added sign-in with Google and GitHub: those
providers have already verified the person, and most newcomers have one of the two.

## Decision
- `deploy/signup.py`: once all five `SMTP_*` keys in the VM's `deploy/.env` are set and valid, and
  `HAGENCY_OPEN_SIGNUP=1` (the default), Synapse gets `registration_requires_token: false`,
  `registrations_require_3pid: [email]`, an `email:` block (STARTTLS required, or TLS on port 465;
  `Hagency Rooms <SMTP_FROM>`), `disable_msisdn_registration: true` and rate limits
  (`rc_registration` 3 then one per ~5.5 min per address; `rc_3pid_validation` 3 then one per ~8 min).
  `request_token_inhibit_3pid_errors: true`, so the email step doesn't reveal whether an address
  already has an account.
- `synapse_modules/hagency_reserved_names.py` (loaded only when sign-up is public): a sign-up that
  carries an email, or comes through Google/GitHub, can't take @pm, @owner, or a name ending in
  `-agent` or `-bot`; the admin API, which makes those accounts, is untouched. Without it, someone
  could take @pm on a fresh database before @pm's container makes it, or a person's future agent name.
- Fail closed: any SMTP key missing, partial or malformed, or the switch at 0, keeps token-only
  sign-up. With no SMTP the config is byte for byte what it was before this change (tested against a
  saved copy). `enable_registration_without_verification` is removed on every run, whatever mode.
- Complete SMTP with the switch at 0 still configures email: password reset works, sign-up is
  invite-only. That is how the owner closes sign-up quickly: `HAGENCY_OPEN_SIGNUP=0`, deploy.
- `deploy/sso_login.py`: Google (`issuer https://accounts.google.com/`, openid/profile/email; the
  localpart from the email; the email itself is not bound to the account, because Synapse would bind
  it unchallenged and move it off an account that already has it) and GitHub (no discovery, its three OAuth endpoints,
  `subject_claim: id`, the localpart from the login), each on only when both of its keys are set;
  independent of each other and of SMTP. `allow_existing_users: false` (Synapse's default, written
  out so nobody flips it unnoticed): a taken localpart is never linked; Synapse tries name1, name2, ...
  Localparts ending in `-agent` or `-bot` get `-g` / `-gh`, so an SSO account can't take the name a
  person's agent will get (identity.py). `sso.client_whitelist` returns to Element without Synapse's
  "continue?" page.
- Secrets live only in the VM's git-ignored `.env` (mode 600) and in `homeserver.yaml` (mode 600,
  Synapse's own). `envfile.py get` refuses them; bootstrap prints only which mode is on and the
  names of missing keys. A malformed sign-up or sign-in value turns that feature off instead of
  stopping the deploy (`envfile.FAIL_CLOSED`), so a typo made while closing sign-up still closes it.
  `deploy/health.py` checks that every registration flow needs exactly what bootstrap configured
  (`signup.py mode`: token or email); from outside, a token or an email, never neither.

## What this protects, and what is left
- Protects: no account without a working mailbox or a Google/GitHub account; the existing
  accounts (@pm, agents, people) can't be claimed through SSO; the SMTP plan's daily quota and the
  server's disk are bounded per address by the rate limits.
- Left: disposable mailboxes and fresh Google/GitHub accounts still work, so a determined spammer
  can make accounts slowly, from many addresses. Synapse has no limit on SSO account creation
  (`rc_registration` covers password sign-up only) and no server-wide email budget: the SMTP plan's
  own daily cap is the ceiling, after which confirmation emails fail until the next day. Every new account lands in the private Welcome room
  with @pm only and reaches other rooms only when they are Open or it is invited; room owners and Synapse Admin
  (deactivate, shadow-ban) are the remedy. A token holder signing up without an email can still take
  a reserved name, as before this change (the owner chose them). If abuse shows up: close sign-up with the switch, add
  `allowed_local_3pids` (only some email domains), or a CAPTCHA as well.

## Consequences
- The owner fills SMTP and the OAuth keys on the VM (DEPLOY_ORACLE.md, "Sign-up and sign-in") and
  runs `deploy.sh`; the repo never holds a value.
- CI installs Jinja2 so `test_sso_login.py` renders the localpart templates the way Synapse does.
