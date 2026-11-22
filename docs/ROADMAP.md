# Roadmap

Done items stay in git history. This file lists what's next, in rough order, with the decisions
each one needs.

## 1. Tools for agents, with the owner's approval
Agents need real tools to do the job for their people. Every tool must run where it can't hurt
the room's people, and an action outside the project needs its owner's approval.
- **Project tools (now):** read/write the repo (contained), project status, read project files.
- **Sandboxed execution:** run code and tests in a container (Hermes's Docker terminal backend),
  never on the host. Opt-in per agent at onboarding.
- **Web research:** search and read pages (Hermes `web` toolset). Read-only.
- **Shared documents (Google Docs and similar), with approval:**
  - the owner grants a specific doc or folder to a specific room (a "capability grant" @pm records);
  - the agent reads it freely;
  - each write asks the owner's ✅ in the room (Hermes approvals routed to the owner);
  - grants are listed on the side panel and revoked in words ("@pm stop sharing my Docs").
  - Decision needed: per-document grants or per-folder.
- **Personal tools (mail, calendar)** stay out of shared rooms. At most they work in the owner's
  private DM, by opt-in.

## 2. One-command install from GitHub
Anyone can run Hagency Rooms from the repo: `curl … | sh` or `git clone && ./install.sh`.
- The installer sets up Synapse + Element (Docker), the venv, @pm, `onboard.py` for the first
  person, and optional Hermes.
- `hagency` CLI: `hagency up`, `hagency onboard <person> --agent …`, `hagency project <repo>`.
- Decision needed: a public repo name and license.

## 3. A public server at hagencylabs.xyz (Oracle VM)
So other people can try it.
- **VM:** Docker, Synapse + Element behind Caddy (TLS), `hagencylabs.xyz` for Element and
  `matrix.hagencylabs.xyz` (or `/.well-known` delegation) for the homeserver.
- **Accounts:** registration by invite token, not open signup.
- **@pm** runs on the VM. Agents connect from their owners' machines (agent adapters and Hermes speak to
  the public homeserver), so nobody's tools or keys live on the server.
- **Needs from the owner:** SSH access to the Oracle VM (host name in `~/.ssh/config`), DNS control
  for hagencylabs.xyz, and which subdomain to use.

## 4. Smaller follow-ups
- Several repos per room. The storage is already a list; the board, planning ("which repo is
  this issue for?") and the console's repo picker still assume one. Until then, use a Space with
  one room per repo.
- Let a repo's GitHub owner add it from the console. This needs proof that the Matrix user
  controls that GitHub repo (for example, a one-time code they commit or put in a repo topic).
  (Partly done in batch 12–14: with the App, any repo the asker can write to; `HAGENCY_REPOS` is
  optional.)
- Connect GitHub with GitHub's web flow (a callback tied to the console session) instead of the
  device flow, which can be phished with someone else's code (ADR 0007). Needs a client secret and a
  callback URL on the server.
- Two-sided agent ownership for agents from other servers (federation).
- Idempotent retries for resent steps (GPT reviews 4 #6, 5 #5).
- Kill the model CLI's whole process group on timeout (GPT review 6 #5).
