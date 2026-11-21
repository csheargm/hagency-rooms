# Security

Hagency Rooms runs agent CLIs (Codex, Claude Code, Hermes) on chat messages, on the owner's own
computer. The checks that matter most:

Trust model (docs/adr/0006-owner-cli-powers.md): **your agent is your CLI as you configured it;
the room can't steer it, because only you can trigger it.**

- **Owner only.** An agent acts only on its owner's messages (`bridge.should_ignore`). Other
  people's messages are context, never a trigger. With `--allow-members`, members' questions run
  locked down (read-only, no web/MCP/connectors), never with the owner's setup.
- **Your CLI, full power, your deny rules.** For its owner, the agent adapter runs Codex / Claude Code
  with the owner's settings and MCP servers, plus web, edits and commands in its work folder
  without an approval prompt (`owner_cli.py`); the owner's deny rules and explicit Codex sandbox
  settings still win, and refusals are named in the reply. Hermes: two profiles on one account,
  the owner's (their own tools, obeys only them) and the loop's (contained tools, obeys only @pm,
  because @pm's tasks carry others' text).
  Turns on untrusted text that the owner didn't ask for (loop builds/reviews/plans, @pm's brain,
  members' questions) stay locked down.
- **Chat never becomes a command.** The prompt goes on stdin, never through a shell or as CLI
  flags; saved session ids are validated.
- **Its own folder.** The agent works in `~/.hagency/work/<owner>/<room>/`, outside the app.
- **No secrets in chat, logs or git.** Passwords are generated into a git-ignored `.env`; agent
  replies are scrubbed of credential-shaped text and the bridge's own token.

A change touching any of these needs a test that shows the check still holds.

Please report a vulnerability privately via GitHub's **Report a vulnerability** button on the
repository's Security tab rather than in a public issue. Known open items are listed in
[REVIEWS.md](REVIEWS.md) under each review's deferred items.
