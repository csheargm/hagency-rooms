"""A stand-in for launchctl as it behaves on a Mac: `bootout` returns while the job is still being
torn down, `print` finds the job until it is gone, and a `bootstrap` meanwhile fails with error 5.
Its own clock moves only when the code under test sleeps, so no test waits."""
from pathlib import Path
from types import SimpleNamespace

NOT_FOUND = 113                               # launchctl print: "Could not find service"
BUSY = SimpleNamespace(returncode=5, stdout="", stderr="Bootstrap failed: 5: Input/output error")


class FakeLaunchd:
    def __init__(self, linger: float = 0.0, busy_after_gone: int = 0, refuse=()):
        self.now, self.calls, self.loaded = 0.0, [], set()
        self.options: list[dict] = []          # the keyword arguments of each call
        self.linger = linger                  # seconds a booted-out job is still there
        self.busy = busy_after_gone           # error-5 refusals even once print no longer finds it
        self.refuse = set(refuse)             # jobs launchd never loads again
        self.gone_at: dict[str, float] = {}

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self, args, **options):
        self.calls.append(args)
        self.options.append(options)
        verb = args[1]
        if verb == "bootout":
            job = args[2].rsplit("/", 1)[1]
            self.loaded.discard(job)
            self.gone_at[job] = self.now + self.linger
        elif verb == "print":
            going = self.now < self.gone_at.get(args[2].rsplit("/", 1)[1], self.now)
            return SimpleNamespace(returncode=0 if going else NOT_FOUND, stdout="", stderr="")
        elif verb == "bootstrap":
            job = Path(args[3]).stem
            if job in self.refuse or self.now < self.gone_at.get(job, self.now):
                return BUSY
            if self.busy:
                self.busy -= 1
                return BUSY
            self.loaded.add(job)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
