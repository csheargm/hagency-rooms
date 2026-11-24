# 0018: People can skip Element's identity check; new rooms aren't encrypted

## Status
Accepted, 2026-11-23 (owner: "there is no need for most of the use cases here").

## Context
After every new sign-in, Element v1.12.35 showed "Confirm your digital identity" (another device, a recovery
key, or "Can't confirm?" → reset) to any account with cross-signing keys, with no way past it but a reset:
`force_verification` defaults to true. Hagency's rooms with @pm and agents aren't end-to-end encrypted (bots
can't read encrypted rooms), so the check protects almost nothing here and blocks people at sign-in.

## Decision
- Element's config: `"force_verification": false` (deploy/element_home.py). The screen gets an X; people
  skip it (one "Are you sure?") and go straight in.
- `/.well-known/matrix/client`: `"io.element.e2ee": {"force_disable": true}` (deploy/Caddyfile). New rooms and
  DMs made in Element aren't encrypted, and accounts without keys that are in no encrypted room are normally
  never asked to set any up (Element reads the well-known after the first sync; if that read is slow or
  fails, it may still offer setup, which they can skip).

No setting in this Element version removes the screen for an account that already has keys
(MatrixChat.postLoginSetup checks none); the X is the closest.

## Consequences
- Rooms already encrypted stay encrypted; a skipped session can't read their history.
- Direct messages between people are no longer encrypted by default.
- Element may still show a "Verify this device" reminder for skipped sessions.
