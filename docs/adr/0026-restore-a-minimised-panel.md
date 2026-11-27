# 0026: A button to restore a minimised panel

## Status
Accepted, 2026-11-27.

## Context
- @pm docks the Hagency panel above the chat in every project room (room state `io.element.widgets.layout`,
  `panel.TOP`).
- Element's **Minimise** on the panel moves it to the room's right-side Extensions list. Nothing above the
  chat shows it is still there. Owners took it for gone.
- Element v1.12.35's way back, checked live on rooms.hagencylabs.xyz: **Room info (ⓘ) → Extensions → the
  pin next to Hagency**. It works, but it is three clicks and owners didn't find it.
- What a minimise changes: the person's own room setting `Widgets.layout`, which overrides the room's layout
  while its `overrides` field names the current layout event. If @pm sends a new layout event, every
  member's override is dropped (Element's `WidgetLayoutStore.recalculateRoom`).
- The panel can't offer the restore itself: a minimised panel isn't loaded at all.

## Options
1. **@pm re-sends the room layout** ("@pm show the panel"). Works in any Element. But it resets the
   layout of everyone in the room, not only the person who asked. It also needs a new intent in
   `pm_chat.py`, which is at its size limit.
2. **Make Element's own path more visible** (CSS in `element-fixes.css`). CSS can't add a click to the
   room view. The pin is already visible; finding Extensions is the problem.
3. **A small "▾ Hagency panel" button in our Element**, under the room header, only while our panel is
   minimised in the room on screen. One click does what Element's pin does: Element's own
   `widgetLayoutStore.moveToContainer(room, app, "top")`, through Element's global `mxSdkContext`.

## Decision
Option 3. `deploy/element_page/panel-dock.js`, linked into Element's `index.html` by
`element-nginx.conf` (the same `sub_filter` as `element-fixes.css`) and served by Caddy at `/hagency/`.
- Only our own panel: widget id `hagency` whose page is this origin's `/panel`.
- The person's own layout, in that room only. Others' layouts and the room's state are untouched.
- Element internals that change in a later version: the button isn't shown, nothing else breaks. Element's
  own path stays the fallback. The panel's footer names it, and so does this ADR.
- No `innerHTML`; nothing from a room is drawn.
- The deploy health check (`health.py`) fails if Element stops loading it.

## Consequences
- It works in our Element web app only. Element Desktop and app.element.io don't load our script.
  Their users use Element's path (Room info → Extensions → pin).
- An Element upgrade needs a check of `mxSdkContext.widgetLayoutStore`. The live calls were checked on
  v1.12.35: `getContainerWidgets`, `canAddToContainer`, `moveToContainer`.
- Element's top area holds three widgets at most. When it is full, the button says so and moves nothing.
