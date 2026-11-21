"""Voice messages for @pm. A voice message can't carry an @mention, so it is for @pm only when it
starts with "PM, …" / names the "Product Manager", replies to one of @pm's messages, approves the
question @pm is waiting on, or is in a two-person chat with @pm. Anything else is someone else's
business (an agent hears its own: bridge_voice.py) and @pm says nothing at all about it.
Mixed into loop.Loop (client, user_id, say, pending, busy, on_human, is_person).
"""
import asyncio
import logging
import re
from types import SimpleNamespace

from nio import MatrixRoom, RoomMessageAudio

import intents
import voice
from lifecycle import is_stale

log = logging.getLogger("loop")
NAMED_PM = re.compile(r"^\W*((hey|hi|hello|ok(ay)?)\W+)?(p\.?\s?m\.?|product manager)(?=[\s,.:;!?]|$)\W*", re.I)
SAID_PM = re.compile(r"\bproduct manager\b", re.I)


class Voice:
    async def on_voice(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        """Transcribing takes seconds: do it beside the sync loop, like any slow decision."""
        if (is_stale(event, self.started_ms) or not self.is_person(room, event.sender)
                or not self.sync_point.first_time(event.event_id)):
            return
        if sum(1 for j in self.busy if getattr(j, "voice", False)) >= voice.MAX_QUEUED:
            log.warning("voice queue full; skipped %s", event.event_id)   # quiet: it may not be for me at all
            return
        job = asyncio.create_task(self.hear(room, event))
        job.voice = True
        self.busy.add(job)
        job.add_done_callback(self.busy.discard)

    async def hear(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        sure = len(room.users) == 2 or await voice.replied_to_sender(self.client, room.room_id, event) == self.user_id
        try:
            text = await voice.listen(self.client, event)
        except (voice.VoiceError, OSError, RuntimeError, ValueError) as e:
            log.warning("voice message %s: %s", event.event_id, e)
            if sure:                          # clearly for me: say so; otherwise it may not be my business
                await self.say(room.room_id, f"🎙 I couldn't hear that ({e}). Please type it.", reply_to=event.event_id)
            return
        to_me = sure or NAMED_PM.match(text) is not None or SAID_PM.search(text) is not None or (
            room.room_id in self.pending and intents.is_approval(text))      # answered with "click 👍"
        if not text or not to_me:
            return
        await self.say(room.room_id, f"📝 Transcript: “{text}”", plain=True, reply_to=event.event_id)
        await self.on_human(room, self.as_text(event, text))

    def as_text(self, event: RoomMessageAudio, text: str) -> SimpleNamespace:
        """The voice message as the typed message to @pm it stands for (same sender, same reply)."""
        content = event.source.get("content", {})
        body = NAMED_PM.sub("", text, count=1) or text
        source = {"content": {"body": body, "m.mentions": {"user_ids": [self.user_id]},
                              **({"m.relates_to": content["m.relates_to"]} if "m.relates_to" in content else {})}}
        return SimpleNamespace(sender=event.sender, body=body, source=source, event_id=event.event_id, voice=True)
