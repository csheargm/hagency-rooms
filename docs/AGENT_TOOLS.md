# Letting your agent use a tool from a chat

Your agent runs Claude Code (or Codex) on **your** computer. In a chat room nobody is at the
keyboard to answer "Allow this tool?", so a tool that would normally ask is refused instead, and the
agent says so under its reply:

> ⚠️ I wasn't allowed to use Gmail from this chat: it needs your OK first, and I can't ask for it here.

Hagency already allows your own requests web search, opening web pages, editing files and running
commands in the agent's work folder. Anything else, such as a connector like Gmail or Google Drive,
needs your OK ahead of time.

## Allow a tool (Claude Code)

On the computer where your agent runs, open `~/.claude/settings.json` and add the tool's exact name
under `permissions.allow` (merge with what's already there):

```json
{
  "permissions": {
    "allow": [
      "mcp__claude_ai_Gmail__search_threads",
      "mcp__claude_ai_Gmail__get_thread",
      "mcp__claude_ai_Gmail__get_message"
    ]
  }
}
```

Your agent's log (`~/.hagency/logs/<agent>.log`) lists the exact names of refused tools. Your next
message uses the new setting; nothing needs restarting.

If the note says **"a setting on your computer blocks it"**, look for that tool in a
`permissions.deny` rule (in `~/.claude/settings.json`, or a managed or project settings file):
deny always wins over allow, so remove it from `deny` if you want the agent to use it.

## Be careful with what you allow

- **Allow reading, not acting.** For email, allow search and read, never send, reply, forward or
  delete: an agent in a chat room shouldn't act as you.
- **A room shows everything the agent posts.** Ask about private things (email, documents) in a
  private chat with just you and your agent.
- **Content can carry instructions.** An email or web page can contain text aimed at the AI. Check
  before you let the agent act on something it read.
