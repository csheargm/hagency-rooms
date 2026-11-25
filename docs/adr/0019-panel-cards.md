# 0019: People make their own panel cards with their agent

## Status
Accepted, 2026-11-25 (owner: let anybody use AI to personalize their experience of the software).

## Context
The Hagency panel shows the same cards to everyone. People want views of their own (who built what
this week, what waits for me, a burndown) and each has an AI agent already. SuperElf showed that people
like describing a small app and getting it. Changing Hagency's own code per person already exists
(customize.py, owner-only, for their agent); a view needs much less power than that.

## Decision
- **Your cards** in a project room's panel: one line ("e.g. who built what this week, as a chart") and
  **Make card**. @pm sends the person's own agent a `card` task (card_task.py): a read-only, fresh turn
  that answers with one HTML document (common/cards.py: the prompt, the data contract, the checks).
- @pm checks what comes back again (size, title, only the agent it asked, only the task it sent) and keeps
  it for that person in that room only (data/panel-cards.json; 6 per person per room, 30 per person, 1000 on
  the server, 10 makes an hour, 30 while HAGENCY_TEST_LIMITS=1; cards of people who left a room are pruned). Each card has **Change** (the agent gets the card as it
  is plus the request) and **Remove** (two clicks).
- The panel shows a card in an `<iframe sandbox="allow-scripts" srcdoc>`: an opaque origin, so no access to
  the panel, its session, cookies or storage; no forms, popups or navigating the page. The document starts
  with its own `Content-Security-Policy: default-src 'none'; webrtc 'block'`, and the panel's own policy
  applies too. A frame that loads a second document (it navigated) gets no more data and is blanked. A card gets the room's data by `postMessage` (hagency.onData): the board, issues in work and
  queued, the plan, the agents, the mode. All of it is what the panel already shows a member.
- Frames sit outside what the panel redraws every 3 s, and are reloaded only when their card changed.

## Not now
- Sharing a card with the room (others would run code someone else's agent wrote; needs a review step).
- Cards in My Hagency (no room data there), cards that act (buttons that change anything), cards that
  read GitHub directly.

## Consequences
- An agent adapter from before this change answers "unknown task kind": the panel says to run its connect
  line again. Hermes agents (MCP) report the task as not supported yet.
- The task and its result pass through the room's chat like a plan does, so members see one line each
  ("…please make a Hagency panel card for anna", "panel card ready: …"); the card's HTML is in those
  events' content, so a member reading the raw event could see it (it holds no data, only the drawing).
- Accepted risks (reviews and live test, 2026-11-25): WebRTC isn't blocked (`webrtc 'block'` had no effect
  in Chromium: a test card reached a STUN server), and DNS prefetch isn't covered everywhere. Fetches,
  images, the panel page, its storage and cookies, popups and navigating anywhere were all blocked live. A card is the person's own agent's work, made
  for them, and gets only what they already see, so a leak needs their own agent to turn against them.
  A card can postMessage to Element's window; Element answers the panel's frame, never the card.
- Cards are views: they are made in any mode, Off included.
