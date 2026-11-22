"""Speech to text for voice messages, on this computer (the audio never leaves it). Shared by @pm
(pm_voice.py) and the agent adapters (bridge_voice.py): each hears only what is addressed to it.
"""
import asyncio
import logging
import os
import shutil
import signal
import tempfile
from pathlib import Path

log = logging.getLogger("voice")
STT = os.environ.get("HAGENCY_STT") or shutil.which("mlx_whisper")   # local speech-to-text CLI
STT_MODEL = os.environ.get("HAGENCY_STT_MODEL", "mlx-community/whisper-large-v3-turbo")
MAX_BYTES = 10_000_000               # about 10 minutes of Element's Opus voice
MAX_MS = 5 * 60 * 1000
STT_TIMEOUT_S = 180
DOWNLOAD_TIMEOUT_S = 30
MAX_WORDS_CHARS = 2000               # what I post and act on
MAX_QUEUED = 3                       # voice messages waiting or being transcribed, per process
_turns = asyncio.Semaphore(1)        # one transcription at a time on this computer


class VoiceError(Exception):
    """Why a voice message couldn't be turned into words (said in the room)."""


async def transcribe(audio: bytes) -> str:
    """Speech to text with the local model, offline. Raises VoiceError."""
    if not STT:
        raise VoiceError("speech-to-text isn't installed here (mlx_whisper)")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "voice.ogg"
        src.write_bytes(audio)
        # Only the first MAX_MS of audio is decoded, whatever the message claims its length is; a
        # minimal environment (no tokens); offline; its own process group so ffmpeg dies with it.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "HF_HUB_OFFLINE": "1"}
        proc = await asyncio.create_subprocess_exec(
            STT, str(src), "--model", STT_MODEL, "--output-format", "txt", "--output-dir", d,
            "--output-name", "words", "--verbose", "False", "--clip-timestamps", f"0,{MAX_MS // 1000}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env, start_new_session=True)
        try:
            _, err = await asyncio.wait_for(proc.communicate(), STT_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass                          # already gone: nothing left to stop
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise VoiceError("transcribing took too long") from None
        words = Path(d) / "words.txt"
        if proc.returncode != 0 or not words.exists():
            log.warning("speech-to-text failed (%s): %s", proc.returncode, err[-300:].decode(errors="replace"))
            raise VoiceError("speech-to-text failed")
        return " ".join(words.read_text()[:MAX_WORDS_CHARS * 2].split())[:MAX_WORDS_CHARS]


async def listen(client, event) -> str:
    """Download a voice message (bounded) and transcribe it. Raises VoiceError."""
    info = event.source.get("content", {}).get("info")
    duration = info.get("duration") if isinstance(info, dict) else None
    size = info.get("size") if isinstance(info, dict) else None
    if isinstance(duration, int) and duration > MAX_MS:      # a hint only: decoding is capped anyway
        raise VoiceError("it's longer than 5 minutes")
    if isinstance(size, int) and size > MAX_BYTES:
        raise VoiceError("it's too large")
    async with _turns:
        try:
            resp = await asyncio.wait_for(client.download(mxc=event.url), DOWNLOAD_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise VoiceError("downloading it took too long") from None
        body = getattr(resp, "body", None)
        if not isinstance(body, bytes) or len(body) > MAX_BYTES:
            raise VoiceError("I couldn't download it" if not isinstance(body, bytes) else "it's too large")
        return await transcribe(body)


async def replied_to_sender(client, room_id: str, event) -> str | None:
    """Who sent the message this one replies to (asked of the server, never taken from the event)."""
    rel = event.source.get("content", {}).get("m.relates_to")
    if not isinstance(rel, dict) or rel.get("is_falling_back"):
        return None                       # a thread's fallback "reply" isn't a reply to anyone in particular
    reply = rel.get("m.in_reply_to")
    target = reply.get("event_id") if isinstance(reply, dict) else None
    if not isinstance(target, str):
        return None
    resp = await client.room_get_event(room_id, target)
    return getattr(getattr(resp, "event", None), "sender", None)
