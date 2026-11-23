# 0017: One GitHub sign-in for a person and their agent

## Status
Accepted, 2026-11-23 (owner: "No need to sign in twice. When the person signs in, the agent should be signed
in as well. They use the same thing.").

## Context
A person signed in to GitHub twice: "Connect GitHub" (ADR 0007, @pm's device flow on the server) proved which
account is theirs, for binding a room to a repo; and the connect line signed their agent in with `gh` on
their computer (client/github_signin.py) so it can open issues and PRs. The server never sees the agent's
token, so it couldn't simply trust the agent's word for which account that is.

## Decision
The connect line's one sign-in now counts for both. After the agent's `gh` sign-in, the client proves the
account to @pm with a gist challenge (`client/github_check.py` → `server/github_proof.py`):
1. With the agent's own Matrix token (homeserver whoami: an agent of this server with one registered owner)
   it asks `POST /connect/github/challenge` for a one-time nonce (10 minutes, 5 an hour per agent).
2. It makes a secret gist holding the nonce with the agent's `gh` sign-in.
3. `POST /connect/github/proof` with the gist id: @pm reads the gist from GitHub itself and takes the owner's
   login and id from GitHub's answer (one file, exactly the nonce, no fork). GitHub sets a gist's owner to
   the account that made it, so a modified client can't claim an account it doesn't control.
4. @pm saves the same `{login, id}` link that Connect GitHub saves; a different existing link is never
   replaced (the person disconnects first). The client deletes the gist and signs its one-off session out.

No GitHub token reaches the server. People without an agent keep Connect GitHub.

## Consequences
- The agent holds the person's own GitHub sign-in (all their repos): an agent misled by room or issue text
  could reach any of them. A separate bot account for the agent stays possible (it then becomes the linked
  account).
- The person can't approve their own agent's PRs on GitHub; reviews come from another person's agent, or
  as a comment (ADR 0016).
- Unauthenticated gist reads share GitHub's 60-an-hour limit per server address; a refusal is said and the
  person can Connect GitHub instead.
