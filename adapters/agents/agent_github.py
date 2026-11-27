"""The panel's "Sign in to GitHub", the agent's side (docs/adr/0021-agent-github-signin.md).

Live test 2026-11-26: an agent couldn't open issues ("needs a GitHub sign-in"), and the only fix was a
shell command, no use to a newcomer on a phone. Now its owner clicks Sign in to GitHub in My agents;
@pm sends this agent github_login.REQUEST_TYPE {"id"}. Only @pm's account is obeyed (Synapse sets the
sender). The agent runs GitHub's device sign-in into its own gh folder (github_login.py, the same recipe
as the connect line), with no browser opening on this computer, reads gh's one-time code, and sends only
that code (validated) to @pm, which shows it to the owner alone. Then one word of github_login.RESULTS,
and a validated GitHub username. Nothing else gh prints goes anywhere, and nothing of it is logged.
At each start it also tells @pm whether it is signed in (STATE_TYPE), so the panel knows to offer the
button, and that this code has it (an agent without it gets Update first).
"""
import asyncio
import logging
import re
import shutil
import uuid

from aiohttp import ClientError
from nio import LocalProtocolError, ToDeviceError, ToDeviceMessage, UnknownToDeviceEvent

from common import github
from common import github_login
from common import kind_report
from common import lifecycle
from common.bridge_config import SERVER_NAME

PM_ID = f"@{github.PM_BOT}:{SERVER_NAME}"
CODE_WAIT_S = github_login.CODE_WAIT_S
SIGNIN_S = 15 * 60                # GitHub's device codes last 15 minutes
HELP_S = github_login.HELP_S
MAX_LINE = 4096                   # gh's own lines are short; a longer one: failed, never read on
MAX_LINES = 50
CODE_LINE_RE = re.compile(r"one-time code:\s*(\S+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")   # color, should a terminal setting force it
log = logging.getLogger("bridge")


def current_state(agent: str) -> dict:
    """What the agent tells @pm at each start: signed in (as whom), not yet, or no gh on this computer."""
    login = github_login.login_of(agent) if github.identity_dir(agent) is not None else None
    if login:                                 # a hosts.yml without a github.com user isn't a sign-in (GPT review)
        return {"state": "signed_in", "login": login}
    return {"state": "none" if shutil.which(github.GH_BIN) else "no_gh"}


async def _help(gh: str, env: dict) -> str:
    proc = await asyncio.create_subprocess_exec(gh, *github_login.LOGIN[:2], "--help", env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await lifecycle.communicate(proc, None, HELP_S)
    return out.decode(errors="replace")


async def _read_code(stream: asyncio.StreamReader) -> str | None:
    """gh's one-time code, only in GitHub's exact shape; None at its end, a bad code, or too much output."""
    for _ in range(MAX_LINES):
        line = (await stream.readline()).decode(errors="replace")
        if not line:
            return None
        found = CODE_LINE_RE.search(ANSI_RE.sub("", line))
        if found:
            return found.group(1) if github_login.USER_CODE_RE.fullmatch(found.group(1)) else None
    return None


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(MAX_LINE):        # read and dropped: gh must never block on a full pipe
        pass


async def sign_in(agent: str, tell_code) -> tuple[str, str | None]:
    """(one of github_login.RESULTS, its GitHub username or None). `tell_code(code)` → whether @pm got it."""
    if github.identity_dir(agent) is not None and github_login.login_of(agent):
        return "already", github_login.login_of(agent)
    gh = shutil.which(github.GH_BIN)
    if gh is None:
        return "no_gh", None
    env = github_login.env_for(github_login.prepare(agent), quiet=True)
    if not github_login.supported(await _help(gh, env)):
        return "too_old_gh", None
    proc = await asyncio.create_subprocess_exec(gh, *github_login.LOGIN, env=env, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=MAX_LINE)
    try:
        return await _follow(proc, agent, tell_code)
    finally:
        if proc.returncode is None:           # timed out, failed or cancelled (the agent stopping): never left running
            proc.kill()
            await asyncio.shield(proc.wait())


async def _follow(proc, agent: str, tell_code) -> tuple[str, str | None]:
    try:
        proc.stdin.write(b"\n")               # "Press Enter to open github.com…", if it asks
        await proc.stdin.drain()
        proc.stdin.close()
        code = await asyncio.wait_for(_read_code(proc.stdout), CODE_WAIT_S)
    except (asyncio.TimeoutError, ValueError, OSError):   # no code in time, an overlong line, gh gone: said as failed
        return "failed", None
    if code is None or not await tell_code(code):
        return "failed", None                 # nobody could enter it: stop now, not in 15 minutes
    try:
        await asyncio.wait_for(_drain(proc.stdout), SIGNIN_S)
        done = await asyncio.wait_for(proc.wait(), HELP_S)
    except asyncio.TimeoutError:
        return "timeout", None
    if done != 0 or github.identity_dir(agent) is None or not github_login.login_of(agent):
        return "failed", None
    return "signed_in", github_login.login_of(agent)


class BridgeGitHub:
    """Mixed into bridge.Bridge (through agent_update.BridgeUpdate)."""
    async def report_github(self) -> None:
        await self.to_pm(github_login.STATE_TYPE, current_state(f"{self.owner}-agent"))

    async def on_github_request(self, event: UnknownToDeviceEvent) -> None:
        if event.type != github_login.REQUEST_TYPE or event.sender != PM_ID:
            return                            # only @pm's account; anyone else's message is ignored
        if self.__dict__.get("_gh_job") and not self._gh_job.done():
            # The newest click wins (review, 2026-11-26): @pm sends a new one only when it has given up on the
            # old (it restarted, or the code came late), so waiting would lock the owner out for 15 minutes.
            log.info("a newer GitHub sign-in click: the running one is stopped")
            await self.stop_github()
        content = event.source.get("content") or {}
        ask = content.get("id") if isinstance(content.get("id"), str) else ""   # echoed: @pm matches it to its click
        self._gh_job = asyncio.create_task(self.github_sign_in(ask[:64]))   # kept apart from turns: a restart never waits 15 minutes

    async def stop_github(self) -> None:
        """Stops a running sign-in (its gh killed: sign_in's finally): a newer click, or the agent stopping."""
        job = self.__dict__.get("_gh_job")
        if job is not None and not job.done():
            job.cancel()
            await asyncio.wait({job})         # never raises: the job's own cancellation stays its own

    async def github_sign_in(self, ask: str) -> None:
        async def tell_code(code: str) -> bool:
            return await self.to_pm(github_login.ANSWER_TYPE, {"id": ask, "code": code})
        try:
            result, login = await sign_in(f"{self.owner}-agent", tell_code)
        except (OSError, asyncio.TimeoutError) as e:   # gh couldn't start or answer --help, no folder: said as failed
            log.warning("GitHub sign-in couldn't run: %s", type(e).__name__)
            result, login = "failed", None
        log.info("GitHub sign-in: %s", result)   # the word only: never gh's output
        await self.to_pm(github_login.ANSWER_TYPE, {"id": ask, "result": result, **({"login": login} if login else {})})
        await self.report_github()            # what @pm keeps: also after a restart of @pm forgot the click

    async def to_pm(self, kind: str, content: dict) -> bool:
        try:
            resp = await asyncio.wait_for(self.client.to_device(ToDeviceMessage(kind, PM_ID, "*", content), uuid.uuid4().hex),
                                          kind_report.SEND_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError, ClientError, LocalProtocolError) as e:
            resp = e
        if not isinstance(resp, (ToDeviceError, Exception)):
            return True
        # Not fatal on purpose: the panel then says it didn't answer, and its owner clicks again.
        log.warning("couldn't tell @pm about GitHub: %s", type(resp).__name__)
        return False
