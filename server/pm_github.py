"""Connect GitHub, as @pm does it (ADR 0007): "Connect GitHub" in the Hagency panel, or "connect my
github" in @pm's private chat. @pm starts GitHub's device flow (github_link.py), sends the code only
to that person (their private chat with @pm, and their own view of the panel), waits for them to
approve on github.com, and then knows their GitHub login. "Disconnect GitHub" forgets it.

One sign-in per person at a time (asking again shows the same code), a few per person per hour,
and a cap for the whole server: GitHub limits how many device codes an App may ask for.
Mixed into loop.Loop (say, private_room, in_private, addressed_text: pm_settings.py, pm_agents.py).
"""
import asyncio
import logging
import re
import time

from nio import MatrixRoom, RoomMessageText

from common import github
from server import github_link
from server.pm_address import unaddressed
from common.redact import room_safe

log = logging.getLogger("loop")
_GITHUB = r"\s+(my\s+)?github(\s+account)?(\s+please)?\W*$"
CONNECT_RE = re.compile(r"^\W*(please\W+)?(connect|link|sign in with|verify)" + _GITHUB, re.I)
DISCONNECT_RE = re.compile(r"^\W*(please\W+)?(disconnect|unlink|forget)" + _GITHUB, re.I)
STARTS_PER_HOUR = 5               # one person: new codes asked for per hour
SERVER_STARTS_PER_HOUR = 40       # everyone together (GitHub rate-limits the App's device codes)
HOUR_S = 3600
CODE = ("To connect your GitHub account, open {url} and enter the code **{code}** (it works for {minutes} "
        "minutes). GitHub asks you to authorize **Hagency Rooms**: I read only your GitHub username, then "
        "throw the sign-in away. Enter this code only if you asked for it yourself.")
OFF = "This server doesn't use Connect GitHub: your agent's own GitHub login counts here."
# Screen test: Connect GitHub in the panel, then "connect my github" in the chat posted the same code twice.
STILL = "The GitHub code I sent above still works for {minutes} more minute{s}: enter it at {url}."
ON_CARD = "Your code is on the GitHub card (and in our private chat)."
CANCELLED = "Cancelled the GitHub sign-in."


def _recent(times: list, now: float) -> list:
    return [t for t in times if now - t < HOUR_S]


class GitHubLink:
    def _github_starts(self) -> dict:
        """Who asked for a code when (in memory): person → times, and "" → everyone's."""
        return self.__dict__.setdefault("_github_start_times", {})

    def _github_limited(self, person: str) -> str | None:
        starts, now = self._github_starts(), time.time()
        mine, everyone = _recent(starts.get(person, []), now), _recent(starts.get("", []), now)
        if len(mine) >= STARTS_PER_HOUR:
            return "You've asked for several GitHub codes this hour; please try again later."
        if len(everyone) >= SERVER_STARTS_PER_HOUR:
            return "Many people are connecting GitHub right now; please try again in a few minutes."
        starts[person], starts[""] = [*mine, now], [*everyone, now]
        return None

    async def github_action(self, person: str, value: str) -> str:
        """The panel's Connect / Disconnect GitHub. Returns its message line; a code shows only on the
        GitHub card (live state, gone when the sign-in ends) and in my private chat, never in that line."""
        if value == "connect":
            answer, code_given, _ = await self.start_github(person, resend=False)
            return ON_CARD if code_given else answer
        if value == "disconnect":
            return self.disconnect_github(person)
        return "unknown action"

    async def connect_github(self, person: str) -> str:
        return (await self.start_github(person))[0]

    async def start_github(self, person: str, resend: bool = True) -> tuple[str, bool, str | None]:
        """(what to tell them, whether a code was given, the chat it went to). The code goes only to my own private chat
        with them (pm_settings.private_room: only I invite, history from the invite); asking again
        shows the same code (resend: in that chat too)."""
        if not github_link.configured():
            return OFF, False, None
        flow = github_link.FLOWS.get(person)
        fresh = flow is None or flow["expires_at"] <= time.time()
        if fresh:
            flow = await self.new_github_flow(person)
            if isinstance(flow, str):
                return flow, False, None
        minutes = max(1, round((flow["expires_at"] - time.time()) / 60))
        text = CODE.format(url=flow["url"], code=flow["user_code"], minutes=minutes)
        if not (fresh or resend):
            return text, True, None
        async with flow.setdefault("sending", asyncio.Lock()):   # GPT review: a second ask while the first is sending
            posted = flow.get("told")         # the chat this code is already in (tell_github)
            if not fresh and posted and posted == await self.private_room(person):   # never the same code twice there
                still = STILL.format(minutes=minutes, s="" if minutes == 1 else "s", url=flow["url"])
                return text, True, await self.tell_github(person, still, flow)
            return text, True, await self.tell_github(person, text, flow)

    async def new_github_flow(self, person: str) -> dict | str:
        """A new device code for `person` and its background wait; or why not (a str to tell them)."""
        starting = self.__dict__.setdefault("_github_starting", {})   # person → this attempt's own marker
        if person in starting:                        # two clicks at once: still one sign-in
            return "One moment: I'm asking GitHub for your code."
        limited = self._github_limited(person)
        if limited:
            return limited
        attempt = starting[person] = object()
        try:
            flow = await github_link.start()
        except github.GitHubError as e:              # its text never carries a code or a token
            return f"I couldn't start the GitHub sign-in ({room_safe(e)}); please try again later."
        finally:
            current = starting.get(person) is attempt     # (Disconnect, then a newer click, replace it)
            if current:
                del starting[person]
        if not current:                               # Disconnect while GitHub answered: no code
            return CANCELLED
        old = github_link.FLOWS.get(person)
        if old and old.get("task"):
            old["task"].cancel()                      # one sign-in per person: an expired one ends
        github_link.FLOWS[person] = flow
        flow["task"] = asyncio.create_task(self.finish_github(person, flow))
        return flow

    def end_github_sign_in(self, person: str) -> tuple[dict | None, bool]:
        """Cancel their pending sign-in, and a code still on its way: (that sign-in, whether one was coming)."""
        flow = github_link.FLOWS.pop(person, None)
        asked = self.__dict__.setdefault("_github_starting", {}).pop(person, None) is not None   # a code on its way: dropped
        if flow and flow.get("task"):
            flow["task"].cancel()
        return flow, asked

    def forget_github_of(self, dead: set[str]) -> bool:
        """Deactivated accounts (pm_remove.prune_registry): their GitHub link goes and a pending
        sign-in is cancelled, so a late approval never brings it back (GPT review, batch 5+6). One
        the disk refused stays out of memory and is tried again even if reactivated meanwhile.
        True when one is still to be saved (the caller looks again soon)."""
        starting = self.__dict__.setdefault("_github_starting", {})
        unsaved = self.__dict__.setdefault("_github_unsaved", set())
        for person in sorted((set(github_link.links()) | set(github_link.FLOWS) | set(starting)) & dead | unsaved):
            self.end_github_sign_in(person)
            try:
                github_link.forget(person)
            except github.GitHubError as e:          # out of memory already; on disk at the next try
                unsaved.add(person)
                log.error("couldn't drop deactivated %s's GitHub link: %s", person, e)
                continue
            unsaved.discard(person)
            log.warning("dropped deactivated %s's GitHub link", person)
        return bool(unsaved)

    def disconnect_github(self, person: str) -> str:
        flow, asked = self.end_github_sign_in(person)
        try:
            gone = github_link.unlink(person)
        except github.GitHubError as e:
            return f"⚠️ {room_safe(e)}"
        if gone:
            return "Disconnected: I no longer know a GitHub account for you."
        return CANCELLED if flow or asked else "No GitHub account was connected."

    async def finish_github(self, person: str, flow: dict) -> None:
        """Waits in the background for the person to approve; tells them how it ended, privately."""
        try:
            login, gid = await github_link.wait_for_login(flow)
            if github_link.FLOWS.get(person) is not flow:   # disconnected or deactivated meanwhile: never saved
                return
            github_link.save_link(person, login, gid)
        except github_link.LinkFailed as e:
            said = str(e)
        except github.GitHubError as e:              # GitHub, or the link couldn't be saved
            said = f"The GitHub sign-in failed ({room_safe(e)}). Click Connect GitHub to try again."
        else:
            said = f"✓ Connected: your GitHub account is **@{login}**."
        finally:
            if github_link.FLOWS.get(person) is flow:
                github_link.FLOWS.pop(person, None)
        await self.tell_github(person, said)

    async def tell_github(self, person: str, text: str, flow: dict | None = None) -> str | None:
        """To that person only, in my own private chat with them; returns that chat (None: not sent).
        With `flow` (a code): only while that sign-in is still theirs (Disconnect may come meanwhile)."""
        try:
            room = await self.private_room(person)
            if flow is not None and github_link.FLOWS.get(person) is not flow:
                return None
            if room is not None:
                await self.say(room, text)
                if flow is not None:
                    flow["told"] = room       # start_github: asked again, "the code above" instead
                return room
        except RuntimeError as e:     # Matrix refused (SendError): the panel still shows it to them
            log.warning("couldn't tell %s about GitHub: %s", person, room_safe(e))
        return None

    async def on_github_text(self, room: MatrixRoom, event: RoomMessageText) -> bool:
        """True if the message was "connect my github" / "disconnect my github" (answered here)."""
        private = self.in_private(room, event.sender)
        text = unaddressed(event.body) if private else self.addressed_text(event, room)
        if text is None or not (CONNECT_RE.search(text) or DISCONNECT_RE.search(text)):
            return False
        if DISCONNECT_RE.search(text):
            await self.say(room.room_id, self.disconnect_github(event.sender))
            return True
        answer, code_given, told = await self.start_github(event.sender)
        if not code_given:            # off, a limit, GitHub refused: no code in it, safe to say here
            await self.say(room.room_id, answer)
        elif told != room.room_id:    # the code itself stays in my own private chat
            await self.say(room.room_id, "I sent you the GitHub code in our private chat." if told else
                           "I couldn't open a private chat with you: click Connect GitHub in the Hagency panel.")
        return True
