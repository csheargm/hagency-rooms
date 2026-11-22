"""How to start an agent CLI without a shell on every OS (the agent adapter's turns, connect.py's check).

On macOS and Linux the bare name is started as it always was. On Windows, npm installs each CLI as
a .cmd wrapper (codex.cmd, claude.cmd). Started by bare name without a shell, Windows looks only for
an .exe, so the CLI "isn't found". Started by its full path, a .cmd runs through cmd.exe, which
re-reads every argument by its own rules (% ^ & | < >). So a wrapper is never started: we read
which program npm's wrapper (cmd-shim) would start, either node with the package's script or the
package's own .exe, and start that directly. No shell either way; the prompt goes on stdin, never
in argv.
"""
import ntpath
import os
import re
import shutil
from pathlib import Path

WRAPPERS = (".cmd", ".bat")
SCRIPTS = (".js", ".cjs", ".mjs")
# npm cmd-shim's last line: `"%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*`, or for
# a package's own program `"%dp0%\node_modules\...\claude.exe"   %*` (older shims: "%~dp0\...").
SHIM_TARGET = re.compile(r'"%~?dp0%?\\([^"%]+)"\s*%\*')


def windows() -> bool:
    return os.name == "nt"


def command(name: str) -> list[str]:
    """The argv prefix that starts `name`. Raises FileNotFoundError for a wrapper we can't read."""
    if not windows():
        return [name]
    found = shutil.which(name)               # honours PATHEXT, so it finds codex.cmd
    if found is None or Path(found).suffix.lower() not in WRAPPERS:
        return [found or name]               # a real program (or nothing: the start then says so)
    return unwrap(Path(found))


def startable(name: str) -> bool:
    """Installed here in a form command() can start."""
    if not shutil.which(name):
        return False
    try:
        command(name)
    except FileNotFoundError:
        return False
    return True


def unwrap(shim: Path) -> list[str]:
    """The program an npm .cmd wrapper starts, as an argv prefix: inside the wrapper's own folder,
    and a real program (an .exe, or node.exe with a script), never another wrapper."""
    lines = [ln.strip() for ln in shim.read_text(errors="replace").splitlines()]
    lines = [ln for ln in lines if ln and not re.match(r"(?i)(@?rem\b|::)", ln)]   # comments never count
    m = SHIM_TARGET.search(lines[-1]) if lines else None    # cmd-shim's command is its last line
    parts = re.split(r"[\\/]", m.group(1)) if m else []
    if not parts or any(not p or p in (".", "..") or ":" in p for p in parts):
        raise FileNotFoundError(f"{shim.name} isn't an npm wrapper I can read; install the CLI's own program (.exe)")
    # Resolved once, and that absolute path is the one checked and started: a relative PATH entry would
    # else be read from the turn's cwd, the checkout (GPT review and re-check, batch 5+6).
    target = shim.parent.joinpath(*parts).resolve()
    if not target.is_file() or not target.is_relative_to(shim.parent.resolve()):   # links: no escape
        raise FileNotFoundError(f"{shim.name} starts {target}, which isn't there; reinstall the CLI")
    if target.suffix.lower() == ".exe":
        return [str(target)]
    if target.suffix.lower() not in SCRIPTS:
        raise FileNotFoundError(f"{shim.name} starts {target.name}, which I can't start without a shell")
    local = shim.parent / "node.exe"        # what the wrapper itself prefers
    node = str(local.resolve()) if local.is_file() else shutil.which("node")
    if node is None or Path(node).suffix.lower() != ".exe":   # PATHEXT may find a node.cmd: that needs a shell
        raise FileNotFoundError(f"{shim.name} needs Node.js (node.exe), which isn't on PATH")
    return [node if ntpath.isabs(node) else os.path.abspath(node), str(target)]   # (a Windows path: ntpath)
