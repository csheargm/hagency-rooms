"""Where trial builds run untrusted code (ADR 0027): an E2B Firecracker microVM per build, never our VM.

Two kinds of sandbox, both fresh for one use and killed in `finally`:
  - BUILD: the agent's tool loop. Egress denied, except the package indexes (EGRESS_ALLOW). Before
    anything is uploaded, a blocked host is probed: if it answers, the build is refused (fail closed).
  - TEST: no network at all; the pristine repo plus the validated patch, and the template's fixed
    test command.
Nothing secret ever goes in: no environment variables are passed, and check_clean() refuses a sandbox
whose environment holds anything shaped like a credential. Every sandbox carries METADATA, so the
reaper can kill anything of ours older than REAP_AFTER_S (a crash, a lost connection).

E2BSandboxes wraps the E2B SDK (requirements-trial.txt pins it; imported only when used, so @pm and
the tests never need it). FakeSandboxes is the same interface in memory, for tests.
"""
import asyncio
import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass, field

BUILD, TEST = "build", "test"
ROOT = "/home/user/repo"
EGRESS_ALLOW = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
PROBE_HOST = "example.com"                    # not allowed: it must not answer
PROBE_CMD = f"curl -sS -o /dev/null -m 5 https://{PROBE_HOST}/"
ENV_CMD = "env"
SAFE_ENV_RE = re.compile(r"^(PATH|HOME|USER|LOGNAME|SHELL|PWD|OLDPWD|SHLVL|TERM|LANG|LC_[A-Z]+|HOSTNAME|_|"
                         r"PYTHON[A-Z_]*|PIP_[A-Z_]*|NODE_[A-Z_]*|NPM_CONFIG_[A-Z_]*|E2B_SANDBOX|E2B_SANDBOX_ID|"
                         r"E2B_TEMPLATE_ID|E2B_EVENTS_ADDRESS|DEBIAN_FRONTEND|VIRTUAL_ENV|TZ|MAIL)$")
CREDENTIAL_NAME_RE = re.compile(r"TOKEN|SECRET|PASS|KEY|CRED|AUTH|SESSION|COOKIE", re.I)
METADATA = {"hagency": "trial-build"}
REAP_AFTER_S = 20 * 60
VCPU, MEMORY_MB = 2, 1024
# Lists the repo's files as JSON, inside the sandbox (its own python3): {"files": {path: sha256},
# "links": [paths], "other": [paths]}. Whatever it says is checked again on our side (trial_patch.py).
LIST_PY = r"""
import hashlib, json, os, stat, sys
root, out = sys.argv[1], {"files": {}, "links": [], "other": []}
for d, dirs, names in os.walk(root):
    dirs[:] = [x for x in dirs if x != ".git"]
    for n in names + [x for x in dirs if os.path.islink(os.path.join(d, x))]:
        p = os.path.join(d, n); rel = os.path.relpath(p, root); st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode): out["links"].append(rel)
        elif stat.S_ISREG(st.st_mode): out["files"][rel] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        else: out["other"].append(rel)
print(json.dumps(out))
"""
LIST_CMD = f"python3 -c {shlex.quote(LIST_PY)} {ROOT}"
MAX_LIST_CHARS = 2_000_000


class SandboxError(RuntimeError):
    """The sandbox couldn't be made, used or trusted; str() is safe to show (no keys, no paths of ours)."""


@dataclass
class Result:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def is_stale(started: float, now: float, older_than_s: int) -> bool:
    """The reaper's rule: started `older_than_s` ago or more (0: every one, the kill switch)."""
    return now - started >= older_than_s


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_listing(text: str) -> dict:
    """LIST_CMD's output, shape-checked (a sandbox that answers nonsense is refused)."""
    if len(text) > MAX_LIST_CHARS:
        raise SandboxError("the sandbox listed too many files")
    try:
        data = json.loads(text)
    except ValueError:
        raise SandboxError("the sandbox's file listing wasn't readable") from None
    files, links, other = (data.get(k) if isinstance(data, dict) else None for k in ("files", "links", "other"))
    if not (isinstance(files, dict) and isinstance(links, list) and isinstance(other, list)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
            and all(isinstance(p, str) for p in links + other)):
        raise SandboxError("the sandbox's file listing wasn't readable")
    return {"files": files, "links": links, "other": other}


async def check_egress(box) -> None:
    """Refuse (SandboxError) unless a blocked host is unreachable: a firewall that doesn't hold fails closed."""
    try:
        probe = await box.run(PROBE_CMD, timeout=15)
    except asyncio.TimeoutError:
        return                    # it hung: unreachable, as it should be
    if probe.exit_code == 0:
        raise SandboxError("the sandbox could reach a blocked host, so it was stopped for safety")


async def check_clean(box) -> None:
    """Refuse a sandbox whose environment holds anything but plain settings (ADR 0027: no secrets in it)."""
    names = [line.split("=", 1)[0] for line in (await box.run(ENV_CMD, timeout=15)).stdout.splitlines() if "=" in line]
    odd = [n for n in names if CREDENTIAL_NAME_RE.search(n) or not SAFE_ENV_RE.match(n)]
    if odd:
        raise SandboxError("the sandbox's environment wasn't clean, so it was stopped for safety")


class E2BSandboxes:
    """Makes E2B sandboxes from our pinned template (common to every build)."""
    def __init__(self, api_key: str, template: str, timeout_s: int) -> None:
        self.api_key, self.template, self.timeout_s = api_key, template, timeout_s

    async def create(self, kind: str, job_id: str) -> "E2BBox":
        from e2b import AsyncSandbox           # requirements-trial.txt (the worker only)
        network = ({"allow_out": EGRESS_ALLOW, "deny_out": lambda ctx: [ctx.all_traffic]} if kind == BUILD else None)
        try:
            sbx = await AsyncSandbox.create(template=self.template, timeout=self.timeout_s, envs={},
                                            metadata={**METADATA, "kind": kind, "job": job_id},
                                            allow_internet_access=kind == BUILD, network=network,
                                            cpu_count=VCPU, memory_mb=MEMORY_MB, api_key=self.api_key)
        except Exception as e:   # noqa: BLE001 — the SDK raises many kinds; the build is refused, the type logged
            raise SandboxError(f"the sandbox service refused a sandbox ({type(e).__name__})") from None
        return E2BBox(sbx)

    async def reap(self, now: float | None = None, older_than_s: int = REAP_AFTER_S) -> int:
        """Kill every sandbox of ours older than `older_than_s` (0: all of them: the kill switch)."""
        from e2b import AsyncSandbox, SandboxQuery
        now, killed = time.time() if now is None else now, 0
        pages = AsyncSandbox.list(query=SandboxQuery(metadata=METADATA), api_key=self.api_key)
        while pages.has_next:
            for info in await pages.next_items():
                if is_stale(info.started_at.timestamp(), now, older_than_s):
                    killed += bool(await AsyncSandbox.kill(info.sandbox_id, api_key=self.api_key))
        return killed


class E2BBox:
    def __init__(self, sbx) -> None:
        self.sbx, self.id = sbx, sbx.sandbox_id

    async def run(self, cmd: str, timeout: float = 120, cwd: str | None = None) -> Result:
        from e2b import CommandExitException, TimeoutException
        try:
            r = await self.sbx.commands.run(cmd, timeout=timeout, cwd=cwd, envs={})
        except CommandExitException as e:      # a non-zero exit is an answer, not a failure of ours
            return Result(e.exit_code, e.stdout or "", e.stderr or "")
        except TimeoutException:
            raise asyncio.TimeoutError() from None
        return Result(r.exit_code, r.stdout or "", r.stderr or "")

    async def write_files(self, files: dict[str, bytes]) -> None:
        from e2b.sandbox.filesystem.filesystem import WriteEntry
        entries = [WriteEntry(path=f"{ROOT}/{p}", data=d) for p, d in files.items()]
        for start in range(0, len(entries), 100):
            await self.sbx.files.write_files(entries[start:start + 100])

    async def read(self, path: str) -> bytes:
        return bytes(await self.sbx.files.read(f"{ROOT}/{path}", format="bytes"))

    async def listing(self) -> dict:
        return parse_listing((await self.run(LIST_CMD, timeout=60)).stdout)

    async def kill(self) -> None:
        try:
            await self.sbx.kill()
        except Exception:   # noqa: BLE001 — already gone; E2B's own timeout and our reaper still end it
            pass


@dataclass
class FakeBox:
    """A sandbox in memory: `files` is the repo; `on_run(box, cmd)` answers other commands (tests)."""
    kind: str
    job: str
    on_run: object = None
    egress_open: bool = False
    environ: str = "PATH=/usr/bin\nHOME=/home/user\n"
    files: dict = field(default_factory=dict)
    links: list = field(default_factory=list)
    killed: bool = False
    ran: list = field(default_factory=list)
    id: str = "fake"
    started: float = 0.0

    async def run(self, cmd: str, timeout: float = 120, cwd: str | None = None) -> Result:
        self.ran.append(cmd)
        if cmd == PROBE_CMD:
            return Result(0 if self.egress_open else 6, "", "" if self.egress_open else "could not resolve host")
        if cmd == ENV_CMD:
            return Result(0, self.environ)
        if cmd == LIST_CMD:
            return Result(0, json.dumps({"files": {p: sha256(d) for p, d in self.files.items()},
                                         "links": list(self.links), "other": []}))
        if self.on_run is None:
            return Result(127, "", "no such command")
        result = self.on_run(self, cmd)
        return await result if asyncio.iscoroutine(result) else result

    async def write_files(self, files: dict[str, bytes]) -> None:
        self.files.update(files)

    async def read(self, path: str) -> bytes:
        if path not in self.files:
            raise SandboxError("no such file in the sandbox")
        return self.files[path]

    async def listing(self) -> dict:
        return parse_listing((await self.run(LIST_CMD)).stdout)

    async def kill(self) -> None:
        self.killed = True


class FakeSandboxes:
    """Hands out FakeBoxes; `boxes` keeps every one made (tests check they were all killed)."""
    def __init__(self, on_run=None, egress_open: bool = False, refuse: bool = False, environ: str | None = None,
                 clock=time.time) -> None:
        self.on_run, self.egress_open, self.refuse, self.environ, self.clock = on_run, egress_open, refuse, environ, clock
        self.boxes: list[FakeBox] = []
        self.reaped: list[int] = []

    async def create(self, kind: str, job_id: str) -> FakeBox:
        if self.refuse:
            raise SandboxError("the sandbox service refused a sandbox (fake)")
        box = FakeBox(kind, job_id, self.on_run, self.egress_open and kind == BUILD, id=f"sbx-{len(self.boxes) + 1}",
                      started=self.clock())
        if self.environ is not None:
            box.environ = self.environ
        self.boxes.append(box)
        return box

    async def reap(self, now: float | None = None, older_than_s: int = REAP_AFTER_S) -> int:
        now = self.clock() if now is None else now
        self.reaped.append(older_than_s)
        live = [b for b in self.boxes if not b.killed and is_stale(b.started, now, older_than_s)]
        for box in live:
            box.killed = True
        return len(live)
