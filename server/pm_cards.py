"""Panel cards (ADR 0019): "Your cards" in a project room's Hagency panel. A person describes a view they
want; @pm sends their own agent a "card" task (card_task.py), checks the HTML that comes back
(common/cards.py) and keeps it for that person in that room only. The panel shows each card in a locked
frame and hands it the room's data (card_data). Change or Remove it any time.

Mixed into the Loop (say, own_agent, agent_online, state, boards, open_issues, last_plans, room_repos);
the panel's actions come here through console.act (ACTIONS).
"""
import hashlib
import secrets
import time
import uuid

from common import cards
from common import registry
from common import test_limits
from common.bridge_config import ROOT
from common.saved_dict import SavedDict
from server import ownership
from server.pm_paused import num

ACTIONS = ("card_make", "card_change", "card_remove", "card_html")
CARDS_FILE = ROOT / "data" / "panel-cards.json"
MAX_PER_PERSON = 6                # in one room
MAX_PERSON_ALL = 30               # in all their rooms
MAX_ALL = 1000                    # on this server (at most ~40 MB) TODO(R4): one file per person if cards grow
MAKES_PER_HOUR = test_limits.pick(10, 30)
WAIT_S = 15 * 60                  # an agent's turn times out after 10 minutes (agents.TURN_TIMEOUT_S)
ERROR_S = 10 * 60                 # how long a failure stays shown
HOUR_S = 3600
NO_AGENT = "Your agent makes your cards, and it isn't in this room. Bring it in (My agents), then try again."
OFFLINE = "Your agent is offline. Start it on its computer, then try again."
BUSY = "Your agent is still making your last card."
FULL = f"You have {MAX_PER_PERSON} cards here. Remove one first."
SERVER_FULL = "This server holds as many cards as it can just now. Remove one of yours, or try again later."
TOO_MANY = "That's a lot of cards for one hour. Try again later."
BAD_REQUEST = f"Describe the card in a sentence (at most {cards.MAX_REQUEST} characters)."
GONE = "That card isn't there any more."
OLD_CODE = "Your agent runs older code and needs an update first."   # the panel adds Update my agent
NO_ANSWER = "Your agent didn't answer. Try again."
NOT_SENT = "Couldn't reach your agent just now. Try again."
HERMES = "Hermes agents can't make cards yet."
STOPPED = "Your agent is stopped. Start it in My agents to make cards."
PERSON_FULL = f"You have {MAX_PERSON_ALL} cards across your rooms. Remove one first."


def cannot(user: str, agent: str | None) -> str | None:
    """Why this agent of theirs can't take a card task now (Hermes has none; a stopped one waits), or None."""
    mine = next((a for a in registry.agents_of(user) if a["localpart"] == local(agent)), None) if agent else None
    if mine and mine.get("agent_kind") == "hermes":
        return HERMES                                 # its hagency tools have no card task (mcp_core.py)
    return STOPPED if mine and mine.get("stopped") else None


def key_of(room_id: str, user: str) -> str:
    return f"{room_id}\n{user}"


def local(user_id: object) -> str | None:
    return user_id.split(":")[0].lstrip("@") if isinstance(user_id, str) and user_id else None


def why(error: object) -> str:
    """A failed card in the panel's words (an old agent adapter doesn't know the task at all)."""
    text = str(error)
    if "unknown task kind" in text or "wrong repo" in text:
        return OLD_CODE
    return f"Your agent couldn't make it: {text[:200]}"


class Cards:
    def card_store(self) -> SavedDict:
        """room + person → their cards there [{id, title, request, html, made}] (kept on disk)."""
        if "_cards" not in self.__dict__:
            CARDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._cards = SavedDict(CARDS_FILE)
        return self._cards

    def card_jobs(self) -> dict:
        """room + person → the card their agent is making {task_id, agent, card, request, at} or a failure
        {error, at}. In memory: a restart forgets, and they ask again."""
        return self.__dict__.setdefault("_card_jobs", {})

    async def make_card(self, room, user: str, request: str, card_id: str = "") -> str:
        rid, key = room.room_id, key_of(room.room_id, user)
        request = cards.clean_request(request)
        if request is None:
            return BAD_REQUEST
        mine = self.card_store().get(key, [])
        old = next((c for c in mine if c["id"] == card_id), None) if card_id else None
        if card_id and old is None:
            return GONE
        if not card_id and len(mine) >= MAX_PER_PERSON:
            return FULL
        if not card_id:
            self.prune_cards()
            if sum(len(v) for k, v in self.card_store().items() if k.endswith("\n" + user)) >= MAX_PERSON_ALL:
                return PERSON_FULL
            if sum(len(v) for v in self.card_store().values()) >= MAX_ALL:
                return SERVER_FULL
        job = self.card_jobs().get(key)
        if job and "task_id" in job and time.time() - job["at"] < WAIT_S:
            return BUSY
        agent = self.own_agent(room, user)
        if agent is None:
            return NO_AGENT
        if unable := cannot(user, agent):
            return unable
        if self.agent_online(local(agent)) is False:
            return OFFLINE
        made = [t for t in self.__dict__.setdefault("_card_makes", {}).get(user, []) if time.time() - t < HOUR_S]
        if len(made) >= MAKES_PER_HOUR:
            return TOO_MANY
        stamp = time.time()
        self._card_makes[user] = [*made, stamp]
        task_id = uuid.uuid4().hex
        self.card_jobs()[key] = {"task_id": task_id, "agent": agent, "card": card_id, "request": request, "at": time.time()}
        what = "change a Hagency panel card" if old else "make a Hagency panel card"
        try:
            await self.say(rid, f"{agent}, please {what} for {local(user)}", agent,
                           {"kind": "card", "task_id": task_id, "repo": self.room_repos.get(rid) or "",
                            "request": request, "html": old["html"] if old else "", "card": card_id})
        except RuntimeError:                          # loop.SendError: Matrix refused it; nothing is waiting
            if self.card_jobs().get(key, {}).get("task_id") == task_id:
                self.card_jobs().pop(key)
            if stamp in self._card_makes.get(user, []):   # not counted against them (only this one: GPT re-check)
                self._card_makes[user].remove(stamp)
            return NOT_SENT
        return "ok"

    async def on_card_result(self, room, sender: str, result: dict) -> None:
        jobs = self.card_jobs()
        key = next((k for k, j in jobs.items() if k.startswith(room.room_id + "\n")
                    and j.get("task_id") == result.get("task_id") and j.get("agent") == sender), None)
        if key is None:
            return                                    # stale, or not the agent we asked
        job = jobs.pop(key)
        if time.time() - job["at"] > WAIT_S:          # given up on: a late answer doesn't count, the panel says so
            jobs[key] = {"error": NO_ANSWER, "request": job["request"], "at": time.time()}
            return
        user = key.partition("\n")[2]
        if user not in room.users:                    # left the room meanwhile: nothing kept for them
            return
        try:
            if result.get("error"):
                raise cards.CardError(why(result["error"]))
            title, html = cards.clean_title(result.get("title", "")), cards.clean_html(result.get("html"))
        except cards.CardError as e:                  # shown in the panel, where they asked
            jobs[key] = {"error": str(e) if str(e).startswith("Your agent") else f"Your agent couldn't make it: {e}",
                         "request": job["request"], "at": time.time()}
            return
        mine = list(self.card_store().get(key, []))
        card = {"id": job["card"] or secrets.token_hex(4), "title": title, "request": job["request"],
                "html": html, "made": time.time()}
        at = next((i for i, c in enumerate(mine) if c["id"] == job["card"]), None) if job["card"] else None
        if at is not None:
            mine[at] = card
        elif job["card"]:
            return                                    # removed while it was being changed: stays removed
        elif full := (SERVER_FULL if sum(len(v) for v in self.card_store().values()) >= MAX_ALL else   # filled meanwhile
                      PERSON_FULL if sum(len(v) for k, v in self.card_store().items() if k.endswith("\n" + user)) >= MAX_PERSON_ALL
                      else None):
            jobs[key] = {"error": full, "request": job["request"], "at": time.time()}
            return
        else:
            mine.append(card)
        self.card_store()[key] = mine[-MAX_PER_PERSON:]

    def prune_cards(self) -> None:
        """Cards of people no longer in a room (or of rooms I left) go: they count against the caps."""
        rooms = self.client.rooms
        for key in list(self.card_store()):
            room_id, _, user = key.partition("\n")
            if room_id not in rooms or user not in rooms[room_id].users:
                self.card_store().pop(key, None)

    def remove_card(self, room_id: str, user: str, card_id: str) -> str:
        key = key_of(room_id, user)
        mine = self.card_store().get(key, [])
        if not any(c["id"] == card_id for c in mine):
            return GONE
        rest = [c for c in mine if c["id"] != card_id]
        if rest:
            self.card_store()[key] = rest
        else:
            self.card_store().pop(key, None)
        return "ok"


def card_data(loop, room, user: str) -> dict:
    """The room data every card gets (common/cards.DATA_DOC): only what the panel shows its members anyway."""
    rid, state = room.room_id, getattr(loop, "state", None) or {}    # before its first poll: none yet
    work = [{"issue": num(k), "title": str(it.get("title", "")), "stage": it.get("stage"), "builder": local(it.get("builder")),
             "reviewer": local(it.get("reviewer")), "pr": it.get("pr"), "ended": it.get("ended")}
            for k, it in state.items() if it.get("room") == rid]
    return {"room": room.display_name, "repo": getattr(loop, "room_repos", {}).get(rid), "me": local(user),   # mode: the panel adds it
            "board": (getattr(loop, "boards", {}).get(rid) or {}).get("text", ""), "work": work[-50:],
            "queued": [{"issue": int(i["number"]), "title": str(i.get("title", ""))} for i in getattr(loop, "open_issues", {}).get(rid, [])][:50],
            "plan": [str(i.get("title", "")) for i in (getattr(loop, "last_plans", {}).get(rid) or {}).get("items", [])],
            "agents": [{"name": local(a), "owner": local(ownership.owner_id(a)), "online": loop.agent_online(local(a))}
                       for a in loop.agents_in(room)]}


def view(loop, room, user: str) -> dict:
    """The panel's "Your cards": this person's cards here (no HTML: card_html fetches it), and what's going on."""
    key = key_of(room.room_id, user)
    job = loop.card_jobs().get(key)
    if job and time.time() - job["at"] > (WAIT_S if "task_id" in job else ERROR_S):
        loop.card_jobs().pop(key, None)
        job = {"error": NO_ANSWER, "request": job["request"], "at": time.time()} if "task_id" in job else None
        if job:
            loop.card_jobs()[key] = job
    items = [{"id": c["id"], "title": c["title"], "request": c["request"], "v": hashlib.sha256(f"{c['made']}{c['html']}".encode()).hexdigest()[:12]}
             for c in loop.card_store().get(key, [])]
    agent = loop.own_agent(room, user)
    return {"items": items, "making": job["request"] if job and "task_id" in job else None,
            "changing": job.get("card") if job and "task_id" in job else None,
            "error": job["error"] if job and "error" in job else None,
            "agent_here": agent is not None, "max": MAX_PER_PERSON, "unable": cannot(user, agent),
            "update": local(agent) if job and job.get("error") == OLD_CODE else None,   # Update my agent
            "data": card_data(loop, room, user)}


async def act(loop, room, user: str, action: str, value: str) -> str | dict:
    if action == "card_make":
        return await loop.make_card(room, user, value)
    card_id, _, request = value.partition("\n")
    if action == "card_change":
        return await loop.make_card(room, user, request, card_id.strip() or "-")
    if action == "card_remove":
        return loop.remove_card(room.room_id, user, card_id.strip())
    found = next((c for c in loop.card_store().get(key_of(room.room_id, user), []) if c["id"] == card_id.strip()), None)
    return {"result": "ok", "doc": cards.frame_doc(found["html"])} if found else GONE
