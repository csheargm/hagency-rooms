# ADR 0005: A person can have several agents

Date: 2026-11-20 · Status: accepted

## Context
ADR 0003 gave every person (full Matrix ID) exactly one agent, named from that ID. People want
more than one: Claude Code and Codex, or one per computer. Ownership, cross-review and follow-me
must stay as strict as before: by the owner's full ID.

## Decision
- **Model**: a person has a list of agents (`registry.agents_of`), the first first. Each has a
  label (one word, `[a-z][a-z0-9]{0,8}`, unique per person; the first is "main" unless named), a
  kind (remote, or what this server runs), when it was created, its private chat and its own
  follow-me setting.
- **No migration on disk**: a person's line in agents.json keeps its fields as their FIRST agent,
  exactly as every older file and writer has them; further agents go in that line's `"more"` list.
  Reading the live single-agent shape gives one agent per person, and nothing is rewritten.
- **Names**: a further agent is `<first agent's name without -agent>-<label>-agent`
  (`cx-codex-agent`, `alice-at-matrix-org-<hash>-laptop-agent`). Labels have no `-` and at most 9
  characters, so they never look like a plain name or the 10-character hash; a name someone else
  claims, or an account that exists without our password, is refused (as in ADR 0003).
- **Adding**: only remote ("bring your own") agents, from @pm's private chat (➕, or "add another
  agent [called X]"; @pm asks once for a name) or the console's "Add an agent". Each agent gets its
  own one-time codes (`pairing.Codes(..., agent=)`) and its own sign-in; connect.py keeps a further
  agent in `~/.hagency/<its name>/`, so several run side by side on one computer.
- **Limits**: `HAGENCY_AGENTS_PER_PERSON` (default 3) per person, counted when the agent is
  reserved, before its account exists; the hourly cap for other servers counts further agents too.
- **Rooms**: follow-me is per agent (the first comes along by default, further ones only when
  their person invites them or changes the setting); web grants were already per agent per room.
- **Routing**: any agent of a person that is in the room and not known to be offline may build;
  the checker (review, verify) must belong to a different person, compared by the owners' full IDs
  (`routing.checkers_for`). An agent without a registered owner never qualifies.

## Consequences
- Chat words ("don't bring my agent", "let my agent use the web") still mean the first agent; the
  console sets each agent separately. "disconnect my agent" asks which when there are several.
- On Windows, connect.ps1 registers only the first agent's Scheduled Task: a further agent there
  runs in a window (`connect.py --name <its name> --foreground`) until the installer learns more.
- Agents this server runs (Hermes, onboard.py) stay one per person: further ones are remote.
- Removing an agent frees its slot (`agent_removal.py`, `pm_remove.py`): its account is
  deactivated through the admin API, its password leaves `.env` and its entry leaves `agents.json`.
  Synapse never re-registers a deactivated name, so the label is kept under "removed" and a new
  agent needs a new name. The first agent goes last, with its person's line. Disconnect still only
  signs an agent out. A reservation whose set-up failed keeps its slot and is finished by asking
  for the same name again (or removed). @pm also forgets people whose every account here is
  deactivated (Synapse's own list, read every 6 hours; nothing is forgotten if it can't be read).
- A disconnect is recorded per agent; a code issued before it never yields the new password.
- **Where sign-ins live** (client/signin_place.py): a computer's first agent keeps its sign-in,
  log and service in `~/.hagency/` (`credentials.json`, `logs/`), a further one in
  `~/.hagency/<its name>/`, its service pointing there with `HAGENCY_CREDENTIALS`. Two people may
  pair on the same computer and user account: a pairing whose agent isn't the one already in
  `~/.hagency/credentials.json` (another account, or another person) goes to its own folder in the
  same way, and is reached again with `--name <its name>`. Re-pairing the same agent replaces its
  own file. Before this, the second pairing replaced the first agent's file (seen live); connect.py
  now names each first agent whose service reads that file but whose sign-in in it is someone
  else's, and tells its person to ask @pm for a new code, which pairs into its own folder.
