"""Voice messages for this agent, heard by the agent itself (no @pm in between): transcribed on its
owner's computer (voice.py), answered like a typed question. A voice message can't carry an
@mention, so it is for this agent when it names it ("Betty's agent, …", "betty agent", "betty-agent"),
replies to one of its messages, or is in a two-person chat with it.

Also: when someone other than the owner asks, the agent says once why it won't answer, instead of
staying silent. Mixed into bridge.Bridge (client, owner, owner_id, user_id, send, should_ignore,
answer_locked, tasks, sync_point, started_ms).
"""
import asyncio
import logging
import re
import time
from types import SimpleNamespace

from nio import MatrixRoom, RoomMessageAudio

import identity
import lifecycle
import voice

log = logging.getLogger("bridge")
REFUSAL_EVERY_S = 600                 # the "only my owner" note, at most once per person per room per 10 min
NOT_OWNER = "sender is not the agent's owner"


class BridgeVoice:
    def is_addressed(self, room: MatrixRoom, event) -> bool:
        """A typed message for me: a DM, an @mention, or my name typed (not inside a quote or an edit)."""
        content = event.source.get("content", {})
        rel = content.get("m.relates_to")
        if isinstance(rel, dict) and rel.get("rel_type") == "m.replace":
            return False                      # an edit isn't a new question
        if room.joined_count == 2 and room.invited_count == 0:   # DM with the agent
            return True
        mentions = content.get("m.mentions")
        user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if isinstance(user_ids, list) and self.user_id in user_ids:
            return True
        own = "\n".join(line for line in event.body.splitlines() if not line.startswith(">"))   # not the quoted part
        return re.search(rf"(?<![\w-])@?{re.escape(self.owner)}-agent\b", own, re.I) is not None

    def named_me(self, text: str) -> bool:
        """A spoken request starts with my name: "Betty's agent, …" (not a mention of me mid-sentence)."""
        owner = re.escape(self.owner_name)
        return re.match(rf"^\W*((hey|hi|hello|ok(ay)?)\W+)?{owner}(?:'s|s)?[\s-]*agent\b", text, re.I) is not None

    def reply_relation(self, room_id: str, question) -> dict:
        """Answer in the question's thread if it was in one; quote it only if others spoke in between."""
        rel = question.source.get("content", {}).get("m.relates_to")
        if isinstance(rel, dict) and rel.get("rel_type") == "m.thread" and isinstance(rel.get("event_id"), str):
            return {"m.relates_to": {"rel_type": "m.thread", "event_id": rel["event_id"], "is_falling_back": True,
                                     "m.in_reply_to": {"event_id": question.event_id}}}
        if self.last_message.get(room_id) != question.event_id:
            return {"m.relates_to": {"m.in_reply_to": {"event_id": question.event_id}}}
        return {}

    async def on_voice(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        if lifecycle.is_stale(event, self.started_ms) or not self.sync_point.first_time(event.event_id):
            return
        if event.sender == self.user_id or event.sender.split(":")[0].endswith("-agent"):
            return                            # never other agents' audio (loop guard)
        if sum(1 for t in self.tasks if getattr(t, "voice", False)) >= voice.MAX_QUEUED:
            log.warning("voice queue full; skipped %s", event.event_id)   # quiet: it may not be for me
            return
        job = asyncio.create_task(self.hear(room, event))
        job.voice = True
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def hear(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        sure = (room.joined_count == 2 and room.invited_count == 0) or (
            await voice.replied_to_sender(self.client, room.room_id, event) == self.user_id)
        if not self.owner_here(room) or (event.sender != self.owner_id and not self.allow_members and not sure):
            return                            # nothing I'd act on: never spend this computer transcribing it
        if await self.stopped_now(room, event, notify=sure):   # agent_stop.py: never transcribed while stopped
            return
        try:
            text = await voice.listen(self.client, event)
        except (voice.VoiceError, OSError, RuntimeError, ValueError) as e:
            log.warning("voice message %s: %s", event.event_id, e)
            if sure:                          # clearly for me: say so (our own words, never a raw error)
                why = str(e) if isinstance(e, voice.VoiceError) else "something went wrong"
                await self.send(room.room_id, f"🎙 I couldn't hear that ({why}). Please type it.", reply_to=event)
            return
        if not text or not (sure or self.named_me(text)):
            return
        asked = SimpleNamespace(sender=event.sender, body=text, event_id=event.event_id, source=event.source)
        reason = self.should_ignore(room, asked)
        if reason:
            await self.refuse(room, asked, reason)
            return
        name = room.user_name(event.sender) or event.sender
        job = asyncio.create_task(self.answer_locked(room, asked, f"{name} ({event.sender}) said by voice: {text}"))
        self.tasks.add(job)
        job.add_done_callback(self.tasks.discard)

    async def refuse(self, room: MatrixRoom, event, reason: str) -> None:
        """Why I won't answer, said once in a while to the person who asked (never silent)."""
        log.info("ignored message in %s: %s", room.room_id, reason)
        if not reason.startswith(NOT_OWNER):
            return                            # other reasons (loop guard, owner absent) stay quiet on purpose
        key, now = (room.room_id, event.sender), time.time()
        if now - self.refused.get(key, 0) < REFUSAL_EVERY_S:
            return
        self.refused = {k: t for k, t in self.refused.items() if now - t < REFUSAL_EVERY_S}   # stays small
        self.refused[key] = now
        try:
            owner = identity.shown_name(room, self.owner_id)       # as they wrote it: "cx", never "Cx"
            await self.send(room.room_id, f"I only take requests from {owner}.", reply_to=event,
                            source=f"I only take requests from {identity.plain(owner)}.")
        except RuntimeError as e:             # a note that can't be posted must never stop the sync loop
            log.warning("couldn't post the owner-only note in %s: %s", room.room_id, e)
