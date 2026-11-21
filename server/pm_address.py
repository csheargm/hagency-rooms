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

ADDRESS_RE = re.compile(r"^\s*@?(pm|product manager)\b[:,]?\s*(.*)$", re.I | re.S)


def without_quote(body: str) -> str:
    """A reply's body starts with the quoted message ("> <@someone> text", then a blank line): read
    only what the person wrote, so "ok" in a reply to any message is just "ok"."""
    lines = body.strip().splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    return "\n".join(lines).strip()


def unaddressed(body: str) -> str:
    """What the person wrote, without its quote and a leading "@pm" / "Product Manager": in our private
    chat people type the address too ("@pm add another agent …"), and it must read the same."""
    text = without_quote(body)
    m = ADDRESS_RE.match(text)
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
        body = without_quote(event.body)
        m = ADDRESS_RE.match(body)
        if room is not None and self.is_one_to_one(room, event.sender):
            return m.group(2) if m else body
        mentions = event.source.get("content", {}).get("m.mentions", {})
        ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if m:
            return m.group(2)
        return body if isinstance(ids, list) and self.user_id in ids else None
