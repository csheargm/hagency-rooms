"""A simulated project room for the owner-AI flow tests (ADR 0022): a real Loop and a real agent
adapter (Bridge) talk through a fake to-device bus; only the model's answer and Matrix are faked."""
import asyncio
import json
from types import SimpleNamespace
from unittest import mock

from nio import RoomGetStateResponse, ToDeviceResponse

from adapters.agents import agents
from adapters.agents import bridge
from common import github
from common import room_state
from server import pm_brain
from server import pm_owner_brain
from server.loop import Loop

ANNA, BETTY = "@anna:bridge.localhost", "@betty:bridge.localhost"     # anna made the room: its owner
PM = f"@{github.PM_BOT}:bridge.localhost"
ANNA_AGENT = "@anna-agent:bridge.localhost"
CREATE = RoomGetStateResponse([{"type": "m.room.create", "sender": ANNA, "state_key": "", "content": {"room_version": "12"}},
                               *({"type": "m.room.member", "state_key": u, "content": {"membership": "join"}}
                                 for u in (ANNA, BETTY, PM, ANNA_AGENT))], "!r")


class RoomBase:
    """Mixed into a unittest.TestCase (not one itself, so its tests aren't collected twice)."""
    def setUp(self):
        for target, name, value in ((pm_brain, "BRAIN", "owner"), (pm_owner_brain, "ACK_S", 0.2),
                                    (pm_owner_brain, "cannot", lambda user, agent: None),
                                    (room_state, "repo_of", mock.AsyncMock(return_value="you/hello")),
                                    (pm_owner_brain.pm_actions.kind_report, "SEND_TIMEOUT_S", 1)):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        no_prs = mock.patch("server.pm_paused.agent_prs", mock.AsyncMock(return_value={}))   # no unrecorded PRs
        no_prs.start()
        self.addCleanup(no_prs.stop)
        self.room = SimpleNamespace(room_id="!r", display_name="Hello", users=dict.fromkeys([ANNA, BETTY, PM, ANNA_AGENT]),
                                    invited_users={}, power_levels=SimpleNamespace(get_user_level=lambda u: 0))
        self.said, self.levels, self.asked, self.model = [], [], [], {"action": "none", "reply": "Hi!"}
        self.make_pm()
        self.make_agent()

    def make_pm(self):
        lp = self.lp = Loop.__new__(Loop)
        lp.user_id, lp.pending, lp.boards, lp.room_repos, lp.state = PM, {}, {}, {"!r": "you/hello"}, {}
        lp.generation, lp.busy, lp.last_plans, lp.lock = {}, set(), {}, asyncio.Lock()

        async def say(room_id, body, *a, **k):
            self.said.append(body)
            return f"$m{len(self.said)}"

        async def to_device(message, txn):          # @pm → the agent's computer
            self.asked.append(message.content)
            await self.b.on_understand(SimpleNamespace(type=message.type, sender=PM, source={"content": message.content}))
            return ToDeviceResponse(message)
        lp.say, lp.discussion = say, lambda room_id, include_self=False: "anna: hello page\nbetty: in French too"
        lp.client = SimpleNamespace(rooms={"!r": self.room}, user_id=PM, to_device=to_device, room_typing=mock.AsyncMock(),
                                    add_to_device_callback=mock.Mock(), room_get_state=mock.AsyncMock(return_value=CREATE),
                                    room_get_event=mock.AsyncMock(return_value=None))
        lp.level = mock.AsyncMock(side_effect=lambda rid: (self.levels or ["assist"])[-1])
        lp.change_level = mock.AsyncMock(side_effect=lambda rid, level, gen=None: self.levels.append(level))
        lp.agents_in = lambda room: [ANNA_AGENT]
        lp.own_agent = lambda room, person: ANNA_AGENT if person == ANNA else None
        lp.agent_online = lambda agent: True
        lp.offer_thumb = mock.AsyncMock()

    def make_agent(self):
        self.b = bridge.Bridge("anna", "claude")
        self.turns = []

        async def to_device(message, txn):          # the agent → @pm
            await self.lp.on_understood(SimpleNamespace(type=message.type, sender=ANNA_AGENT, source={"content": message.content}))
            return ToDeviceResponse(message)

        async def run_turn(backend, workdir, prompt, **kw):
            self.turns.append((prompt, kw))
            return json.dumps(self.model)
        self.b.client = SimpleNamespace(rooms={"!r": self.room}, user_id=ANNA_AGENT, access_token="tok", to_device=to_device,
                                        room_get_state=mock.AsyncMock(return_value=CREATE))
        self.b.is_stopped = mock.AsyncMock(return_value=False)
        p = mock.patch.object(agents, "run_turn", run_turn)
        p.start()
        self.addCleanup(p.stop)

    def tell(self, sender, body, mention=True, reply_to=None):
        content = {"body": body, "m.mentions": {"user_ids": [PM]} if mention else {}}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        ev = SimpleNamespace(sender=sender, body=body, source={"content": content})

        async def go():
            await self.lp.on_human(self.room, ev)
            while self.lp.busy or self.b.tasks:
                await asyncio.gather(*self.lp.busy, *self.b.tasks)
        asyncio.run(go())

    def react(self, sender, key="👍"):
        to = self.lp.pending["!r"]["event_id"]
        asyncio.run(self.lp.on_reaction(self.room, SimpleNamespace(sender=sender, key=key, reacts_to=to)))
