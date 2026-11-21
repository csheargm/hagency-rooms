"""Answers without an @mention: when the agent asked its owner something, the owner's next messages
in that room may be the answer ("Tell me a city or zip code" … "95130").

While a question is open (the agent's latest reply to its owner in that room has a sentence ending
in "?"; open for OPEN_QUESTION_S, for at most MAX_CANDIDATES messages), every message from the
OWNER there that doesn't @mention or reply to someone else is offered to the agent as "this may
answer your question". The agent decides. If it isn't for it, it answers exactly NO_REPLY and the
bridge posts nothing. A real reply closes the question (or opens a new one if it asks again).
Owner-only is unchanged: nobody else's message is ever offered.
Mixed into bridge.Bridge (owner_id, user_id, open_questions).
"""
import re
import time

OPEN_QUESTION_S = 1800
MAX_CANDIDATES = 5
NO_REPLY = "NO_REPLY"
NO_REPLY_RE = re.compile(rf"^\W*{NO_REPLY}\W*$")
QUESTION_RE = re.compile(r"\?(?=\s|$|[)\"'*_])")            # a "?" ending a sentence (not "?q=" in a URL)
AGENT_NAME_RE = re.compile(r"(?<![\w-])@?([a-z0-9][a-z0-9._-]*-agent)\b", re.I)
CODE_RE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
FOLLOW_UP_PROMPT = (
    "\nYou recently asked your owner a question in this room, and this message from them was not "
    "addressed to you. It may be their answer, or about something else entirely (the conversation "
    f"above shows what came in between). If it isn't for you, reply with exactly {NO_REPLY} and "
    "nothing else; otherwise answer normally.\n")


def asks(text: str) -> bool:
    """The reply asks something: a sentence of its own (not code or a quote) ends with "?"."""
    own = "\n".join(ln for ln in CODE_RE.sub(" ", text).splitlines() if not ln.lstrip().startswith(">"))
    return QUESTION_RE.search(own) is not None


def silent(reply: str) -> bool:
    """The agent said NO_REPLY on a line of its own (a progress line or a refusal note may come too)."""
    return any(NO_REPLY_RE.match(ln) for ln in reply.splitlines())


class BridgeFollowUp:
    def note_reply(self, room_id: str, event_id: str, text: str, to: str) -> None:
        """After a real reply: a question to the owner opens (or renews) the window; else it closes."""
        if to != self.owner_id:
            return                            # a member's answer never closes my question to my owner
        if asks(text):
            self.open_questions[room_id] = {"event": event_id, "at": time.time(), "left": MAX_CANDIDATES}
        else:
            self.open_questions.pop(room_id, None)

    def open_question(self, room_id: str) -> str | None:
        """My open question's event id in the room (None: none, or its window has passed)."""
        q = self.open_questions.get(room_id)
        if q is not None and time.time() - q["at"] > OPEN_QUESTION_S:
            self.open_questions.pop(room_id, None)
            return None
        return q and q["event"]

    def may_answer(self, room, event) -> str | None:
        """The owner's un-addressed message while my question is open: offer it (counted). Returns
        the question it may answer, so a queued turn can check that question is still the open one."""
        q = self.open_questions.get(room.room_id)
        if event.sender != self.owner_id or self.open_question(room.room_id) is None:
            return None
        if q["left"] <= 0:
            self.open_questions.pop(room.room_id, None)
            return None
        names = {n.lower() for n in AGENT_NAME_RE.findall(event.body or "")}
        if names - {self.user_id.lstrip("@").partition(":")[0].lower()}:
            return None                       # names another agent in plain text: not for me
        content = event.source.get("content", {})
        mentions = content.get("m.mentions")
        user_ids = mentions.get("user_ids") if isinstance(mentions, dict) else None
        if isinstance(user_ids, list) and [u for u in user_ids if u != self.user_id]:
            return None                       # talking to someone else
        rel = content.get("m.relates_to")
        if isinstance(rel, dict):
            if rel.get("rel_type") == "m.replace":
                return None                   # an edit isn't a new message
            target = (rel.get("m.in_reply_to") or {}).get("event_id") if isinstance(rel.get("m.in_reply_to"), dict) else None
            if target and target != q["event"]:
                return None                   # a reply to someone else's message
        q["left"] -= 1
        return q["event"]
