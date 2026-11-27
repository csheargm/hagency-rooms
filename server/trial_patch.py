"""A trial build's patch, made and checked on OUR side (ADR 0027), never taken from the sandbox as a diff.

The worker keeps the pristine repo (from GitHub's tarball at the base commit). After the tool loop it
lists the sandbox's files with their hashes, reads only those that differ, and builds the patch here
against the pristine copy. The sandbox chooses nothing about the result's shape: every change is a
plain file (mode kept from the pristine copy, else 100644), so no symlink, gitlink or mode change can
come out of it. A patch is refused (Refused, with the rule broken) when it has:
  - paths under .github/, .devcontainer/ or .vscode/, or a .gitattributes, .gitmodules or
    .pre-commit-config.yaml anywhere, or any .git path;
  - a symlink, or anything but regular files, where the sandbox's repo is;
  - binary content (a NUL byte, or not UTF-8);
  - edits to test files or test config (the fixed test command must test the visitor's change, not
    a rewritten test), or a module that would stand in for the test runner (pytest.py, a .pth);
  - more than MAX_BYTES of changed content or more than MAX_FILES files;
  - path problems once case and Unicode are normalised (absolute, "..", control characters,
    backslashes, not NFC, two paths that differ only by case);
  - no change at all (R14: an empty diff is a failed build, never a pass).
Build output (caches, node_modules) is left out and never part of a patch.
"""
import unicodedata
from dataclasses import dataclass

MAX_BYTES = 200 * 1024
MAX_FILES = 50
MAX_PATH = 400
FORBIDDEN_DIRS = (".github", ".devcontainer", ".vscode", ".git")
FORBIDDEN_NAMES = {".gitattributes", ".gitmodules", ".pre-commit-config.yaml"}
IGNORED_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".venv", ".mypy_cache", ".ruff_cache"}
IGNORED_SUFFIXES = (".pyc", ".pyo")
TEST_DIRS = {"test", "tests", "__tests__", "spec", "testing"}
TEST_CONFIG = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py",
               "package.json", "package-lock.json", ".mocharc.js", ".mocharc.json", ".mocharc.yml", "Makefile",
               "requirements.txt", "requirements-dev.txt", ".coveragerc", "sitecustomize.py", "usercustomize.py"}
TEST_CONFIG_PREFIXES = ("jest.config.", "vitest.config.", "karma.conf.", "babel.config.", "playwright.config.")
# A top-level module or package that would stand in for the test runner itself (GPT review: a repo-root
# pytest.py could answer "passed"); the test command also runs with `python -P`, so the repo isn't first.
RUNNER_MODULES = {"pytest", "_pytest", "py", "pluggy", "iniconfig", "packaging", "unittest", "site", "runpy"}
DEFAULT_MODE = "100644"
UNSAFE_CHARS = set('\\`<>|*?"')     # never in a name the PR, the review or a chat message shows
PLAIN_MODES = ("100644", "100755")      # anything else in the template (a symlink, a submodule) is never touched


@dataclass
class Change:
    path: str
    mode: str
    data: bytes | None          # None: the file is deleted


class Refused(ValueError):
    """The patch breaks a rule; str() is a fixed, plain sentence safe to show (it names the rule)."""
    def __init__(self, rule: str, detail: str = "") -> None:
        super().__init__(rule)
        self.rule, self.detail = rule, detail


EMPTY, PROTECTED, LINK, BINARY, TESTS, TOO_BIG, BAD_PATH = (
    "it made no change", "it changed a protected file (CI, editor or git settings)", "it made a symlink",
    "it wrote a binary file", "it changed the tests or the test settings", "the change was too big",
    "it used a file name that isn't allowed")


def ignored(path: str) -> bool:
    parts = path.split("/")
    return any(p in IGNORED_DIRS for p in parts[:-1]) or parts[-1] in IGNORED_DIRS or path.endswith(IGNORED_SUFFIXES)


def check_path(path: str) -> None:
    if (not path or len(path) > MAX_PATH or path.startswith("/") or any(c in UNSAFE_CHARS for c in path)
            or any(ord(c) < 32 or ord(c) == 127 for c in path) or unicodedata.normalize("NFC", path) != path):
        raise Refused(BAD_PATH, path[:80])
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise Refused(BAD_PATH, path[:80])
    lowered = [p.casefold() for p in parts]
    if any(p in FORBIDDEN_DIRS for p in lowered) or lowered[-1] in FORBIDDEN_NAMES:
        raise Refused(PROTECTED, path[:80])


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1].casefold()
    top = parts[0].casefold().removesuffix(".py")
    return (any(p.casefold() in TEST_DIRS for p in parts[:-1]) or name in {n.casefold() for n in TEST_CONFIG}
            or name.startswith(TEST_CONFIG_PREFIXES) or name.startswith("test_") or name.endswith("_test.py")
            or ".test." in name or ".spec." in name or name.endswith(".pth") or top in RUNNER_MODULES)


def check_text(path: str, data: bytes) -> None:
    if b"\0" in data:
        raise Refused(BINARY, path[:80])
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(BINARY, path[:80]) from None


def check_case(paths: list[str]) -> None:
    """Two paths that are one on a case-insensitive or Unicode-folding disk."""
    seen: dict[str, str] = {}
    for p in paths:
        key = unicodedata.normalize("NFKC", p).casefold()
        if seen.get(key, p) != p:
            raise Refused(BAD_PATH, p[:80])
        seen[key] = p


def changes(pristine: dict, after: dict) -> list[Change]:
    """pristine: {path: (mode, bytes)}; after: {path: bytes} (what the sandbox has now, build output
    included). The changes, sorted by path; nothing is checked here."""
    out = [Change(p, (pristine.get(p) or (DEFAULT_MODE,))[0], d) for p, d in after.items()
           if not ignored(p) and (p not in pristine or pristine[p][1] != d)]
    out += [Change(p, pristine[p][0], None) for p in pristine
            if p not in after and not ignored(p) and pristine[p][0] in PLAIN_MODES]   # a link isn't uploaded
    return sorted(out, key=lambda c: c.path)


def validate(pristine: dict, after: dict, links: list[str] = (), other: list[str] = ()) -> list[Change]:
    """The checked patch, or Refused. `links` / `other`: what the sandbox listed as symlinks or
    neither file nor folder (refused wherever they are, build output aside)."""
    if any(not ignored(p) for p in [*links, *other]):
        raise Refused(LINK, next(p for p in [*links, *other] if not ignored(p))[:80])
    found = changes(pristine, after)
    if not found:
        raise Refused(EMPTY)
    for c in found:
        check_path(c.path)
        if c.mode not in PLAIN_MODES:
            raise Refused(LINK, c.path[:80])  # written over a symlink or submodule of the template
    check_case(sorted(set(pristine) | {c.path for c in found}))
    for c in found:
        if is_test_path(c.path):
            raise Refused(TESTS, c.path[:80])
        if c.data is not None:
            check_text(c.path, c.data)
    if len(found) > MAX_FILES or sum(len(c.data or b"") for c in found) > MAX_BYTES:
        raise Refused(TOO_BIG)
    return found


def summary(patch: list[Change]) -> list[str]:
    """One line a file: "path (changed|added|deleted)"; for the PR, the review and the visitor."""
    return [f"{c.path} ({'deleted' if c.data is None else 'changed or added'})" for c in patch]
