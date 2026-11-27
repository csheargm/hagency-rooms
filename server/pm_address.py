"""Is a message for @pm? In a room: when it starts with "@pm" / "Product Manager" or mentions @pm.
In a private chat of mine with exactly one person (one I made, or one they opened with me), every
message is for me (seen live: "resend me the code" typed there got no answer). A project room never
counts, however few people are in it: there, plain "stop" stays someone thinking aloud (GPT review).
Mixed into pm_chat.Chat (user_id, dm_rooms).
"""
import re

from nio import RoomMessageText

from common.bridge_config import ROOT
from common.saved_dict import SavedDict
from server import pm_pill

ADDRESS_RE = re.compile(r"^\s*@?(pm|product manager)\b(?![.:][A-Za-z0-9-]+[.:])[:,]?\s*(.*)$", re.I | re.S)


def without_quote(body: str) -> str:
    """A reply's body starts with the quoted message ("> <@someone> text", then a blank line): read
    only what the person wrote, so "ok" in a reply to any message is just "ok"."""
    lines = body.strip().splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    return "\n".join(lines).strip()


def without_pill(text: str, me: str | None) -> tuple[str, bool]:
    """(text, False) with a leading mention pill of `me` turned into "@pm "; (text, True) when the text
    starts with someone else's pill, so it is never read as "@pm …" (GPT review: "@pm.ops:x stop")."""
    pill = pm_pill.leading(text)
    if pill is None:
        return text, False
    user, rest = pill
    return ("@pm " + text[rest:], False) if me and user.lower() == me.lower() else (text, True)


def unaddressed(body: str, me: str | None = None) -> str:
    """What the person wrote, without its quote and a leading "@pm" / "Product Manager": in our private
    chat people type the address too ("@pm add another agent …"), and it must read the same."""
    text, other = without_pill(without_quote(body), me)
    m = None if other else ADDRESS_RE.match(text)
    return m.group(2) if m else text


class Addressing:
    def direct_chats(self) -> dict:
        """room → person: direct chats a person opened with me themselves (pm_follow.py), on disk."""
        if "_direct_chats" not in self.__dict__:
            self._direct_chats = SavedDict(ROOT / "data" / "direct-chats.json")
        return self._direct_chats

    def chats_with(self) -> dict:
        """room → person, for every private chat of mine I know of: by my own records, never by a
        room's name, which anyone can choose (GPT review)."""
        known = {room: person for person, room in (getattr(self, "dm_rooms", None) or {}).items()}
        welcomes = self.welcomes() if hasattr(self, "welcomes") else {}
        known.update({entry.get("dm"): person for person, entry in welcomes.items() if isinstance(entry, dict)})
        known.update(self.direct_chats())
        return known

    def is_private_chat(self, room) -> bool:
        """One of my private chats (mine, or one a person opened with me): never a project room."""
        return room.room_id in self.chats_with()

    def is_one_to_one(self, room, person: str) -> bool:
        """That person's private chat with me, with nobody else joined or invited."""
        return (set(getattr(room, "users", None) or {}) == {self.user_id, person}
                and self.chats_with().get(room.room_id) == person)

    def addressed_text(self, event: RoomMessageText, room=None) -> str | None:
        """The message without its "@pm" / "Product Manager" address, or None if not for me."""
        body, other = without_pill(without_quote(event.body), self.user_id)
        m = None if other else ADDRESS_RE.match(body)
        if room is not None and self.is_one_to_one(room, event.sender):
            return m.group(2) if m else body
        mentions = event.source.get("content", {}).get("m.mentions", {})
        ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if m:
            return m.group(2)
        return body if isinstance(ids, list) and self.user_id in ids else None
