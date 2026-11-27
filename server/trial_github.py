"""Trial builds' GitHub access, as the separate "Hagency Trial Builder" App (ADR 0027, docs/TRIAL_BUILDER_SETUP.md).

The App has Contents, Pull requests and Issues read/write and Metadata read, and is installed only on
the hagency-trials org: never Workflows, Administration, Actions or Secrets. @pm's own read-only App
(ADR 0004) is unchanged and never used here.

Every token is minted fresh for ONE demo repo (one of TRIAL_REPOS, nothing else), with only the
permissions that step needs (READ for the tarball and the issue, WRITE for the pull request), used,
and revoked at once (DELETE /installation/token). Writes go through an allowlist (WRITES): a trial
branch, its tree and commit, the pull request, its label, a COMMENT review, and the recycling of a
lease (closing trial PRs, deleting trial branches, resetting the default branch to the template).
Never logged or put in an error: the private key, the JWT, a token.
"""
import asyncio
import io
import logging
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request

from common import github
from common import github_app

log = logging.getLogger("trial-build")
READ = {"contents": "read", "issues": "read", "pull_requests": "read", "metadata": "read"}
WRITE = {"contents": "write", "issues": "write", "pull_requests": "write", "metadata": "read"}
BRANCH_PREFIX = "trial/"
LABEL = "written by AI in a sandbox"
SEED_LABEL = github.LABEL                # "agent-ok": the loop's own label (common/github.py)
MAX_TARBALL = 50 * 1024 * 1024
MAX_FILE = 5 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_EXPANDED = 100 * 1024 * 1024       # the whole tree, unpacked (GPT review: members alone weren't bounded)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TARBALL_HOSTS = ("codeload.github.com",)
# (method, path under /repos/<repo>) a token may write; bodies are checked too (check_write)
WRITES = [("POST", re.compile(r"^/git/(trees|commits|refs)$")), ("PATCH", re.compile(r"^/git/refs/heads/[A-Za-z0-9._/-]{1,200}$")),
          ("DELETE", re.compile(r"^/git/refs/heads/trial/[A-Za-z0-9._-]{1,100}$")), ("POST", re.compile(r"^/pulls$")),
          ("PATCH", re.compile(r"^/pulls/[0-9]{1,9}$")), ("POST", re.compile(r"^/issues/[0-9]{1,9}/labels$")),
          ("POST", re.compile(r"^/pulls/[0-9]{1,9}/reviews$"))]


class TrialGitHubError(github.GitHubError):
    """GitHub refused or answered nonsense; str() is safe (no tokens)."""


def check_write(method: str, sub: str, body: dict | None) -> None:
    """Refuse any write outside the allowlist (ADR 0027), or with a body that does more than it should."""
    if not any(m == method and rx.match(sub) for m, rx in WRITES):
        raise TrialGitHubError(f"trial builds may not {method} {sub.split('/')[1] if '/' in sub else sub}")
    body = body or {}
    if sub == "/git/refs" and not str(body.get("ref", "")).startswith(f"refs/heads/{BRANCH_PREFIX}"):
        raise TrialGitHubError("trial builds only make trial/ branches")
    if method == "PATCH" and sub.startswith("/git/refs/") and (
            sub.startswith(f"/git/refs/heads/{BRANCH_PREFIX}") or set(body) != {"sha", "force"}
            or not SHA_RE.match(str(body.get("sha"))) or body.get("force") is not True):
        raise TrialGitHubError("trial builds only reset a default branch to its template")
    if method == "PATCH" and sub.startswith("/pulls/") and body != {"state": "closed"}:
        raise TrialGitHubError("trial builds only close their own pull requests")
    if sub.endswith("/reviews") and body.get("event") != "COMMENT":
        raise TrialGitHubError("a trial review is a comment, never an approval")


def unpack(data: bytes) -> dict:
    """GitHub's tarball → {path: (mode, bytes)}, without the top folder. Symlinks and submodules keep
    their mode ("120000"), with no content, so the patch can refuse touching them (trial_patch.py)."""
    out: dict = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise TrialGitHubError("the demo repo has too many files")
            for m in members:
                path = m.name.split("/", 1)[1] if "/" in m.name else ""
                if not path or m.isdir():
                    continue
                if m.issym() or m.islnk() or not m.isfile():
                    out[path] = ("120000", b"")
                elif m.size > MAX_FILE or total + m.size > MAX_EXPANDED:
                    raise TrialGitHubError("the demo repo is too big to build on")
                else:
                    total += m.size
                    out[path] = ("100755" if m.mode & 0o111 else "100644", tar.extractfile(m).read())
    except (tarfile.TarError, OSError, EOFError):
        raise TrialGitHubError("the demo repo's tarball wasn't readable") from None
    return out


def _fetch_tarball(url: str, token: str) -> bytes:
    """GitHub answers a redirect to a signed download URL: followed by hand, without our token."""
    opener = urllib.request.build_opener(github_app._NoRedirect)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "hagency-trial-builder",
                                               "Accept": github_app.REST_ACCEPT})
    try:
        with opener.open(req, timeout=github_app.HTTP_TIMEOUT_S) as r:
            return r.read(MAX_TARBALL + 1)        # (a fake that answers at once)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "") if e.code in (301, 302, 307) else ""
    host = urllib.parse.urlsplit(location).hostname or ""
    api_host = urllib.parse.urlsplit(github_app.API).hostname
    if not location.startswith(("https://", f"{github_app.API}/")) or host not in (*TARBALL_HOSTS, api_host):
        raise TrialGitHubError("GitHub didn't give the demo repo's tarball")
    with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": "hagency-trial-builder"}),
                                timeout=github_app.HTTP_TIMEOUT_S) as r:
        return r.read(MAX_TARBALL + 1)


class TrialGitHub:
    def __init__(self, app_id: str, key_path: str, repos: list[str], call=None, fetch=None) -> None:
        self.app_id, self.key_path, self.repos = app_id, key_path, {r.lower() for r in repos}
        self.reset: tuple[str, str] | None = None      # (default branch, template commit) while recycling
        self.call = call or github_app.call
        self.fetch = fetch or (lambda url, token: asyncio.to_thread(_fetch_tarball, url, token))

    def _jwt(self) -> str:
        return github_app.app_jwt(app_id=self.app_id, key_path=self.key_path)

    def _repo(self, repo: str) -> str:
        if not github.REPO_RE.match(repo or "") or repo.lower() not in self.repos:
            raise TrialGitHubError("not one of the trial demo repos")
        return repo

    async def token(self, repo: str, write: bool = False) -> str:
        """A fresh token for `repo` alone, with READ or WRITE permissions; revoke() it when done."""
        status, data = await self.call("GET", f"/repos/{self._repo(repo)}/installation", self._jwt())
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("id"), int):
            raise TrialGitHubError(f"the Trial Builder App isn't installed on {repo} ({status})")
        status, data = await self.call("POST", f"/app/installations/{data['id']}/access_tokens", self._jwt(),
                                       {"repositories": [repo.split("/", 1)[1]], "permissions": WRITE if write else READ})
        if status != 201 or not isinstance(data, dict) or not isinstance(data.get("token"), str):
            raise TrialGitHubError(f"GitHub gave no token for {repo} ({status})")
        return data["token"]

    async def revoke(self, token: str) -> bool:
        """Revoke `token` (tried twice); False, and a warning for the operator, if GitHub didn't confirm: the
        token then lives until it expires (an hour), scoped to one demo repo (GPT review: this was silent)."""
        for _ in range(2):
            try:
                status, _ = await self.call("DELETE", "/installation/token", token)
            except github.GitHubError:
                continue          # GitHub didn't answer: once more
            if status in (204, 401):   # 401: already invalid
                return True
        log.warning("a Trial Builder token couldn't be revoked; it expires within the hour")
        return False

    async def get(self, token: str, repo: str, sub: str) -> object:
        status, data = await self.call("GET", f"/repos/{self._repo(repo)}{sub}", token)
        if status != 200:
            raise TrialGitHubError(f"GitHub answered {status} for {sub.split('?')[0]}")
        return data

    async def write(self, token: str, repo: str, method: str, sub: str, body: dict | None = None) -> object:
        check_write(method, sub, body)
        if method == "PATCH" and sub.startswith("/git/refs/") and (
                self.reset is None or (sub, body["sha"]) != (f"/git/refs/heads/{self.reset[0]}", self.reset[1])):
            raise TrialGitHubError("a branch is only reset to its template, while recycling")
        status, data = await self.call(method, f"/repos/{self._repo(repo)}{sub}", token, body)
        if status not in (200, 201, 204):
            raise TrialGitHubError(f"GitHub answered {status} for {method} {sub.split('/')[1]}")
        return data

    # --- reads -----------------------------------------------------------------------------------
    async def base(self, token: str, repo: str) -> tuple[str, str]:
        """(default branch, its head commit)."""
        info = await self.get(token, repo, "")
        branch = info.get("default_branch") if isinstance(info, dict) else None
        ref = await self.get(token, repo, f"/git/ref/heads/{urllib.parse.quote(str(branch))}")
        sha = (ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None
        if not isinstance(branch, str) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise TrialGitHubError("the demo repo's default branch couldn't be read")
        return branch, sha

    async def seeded_issues(self, token: str, repo: str) -> list[dict]:
        raw = await self.get(token, repo, f"/issues?state=open&labels={urllib.parse.quote(SEED_LABEL)}&per_page=50")
        return [{"number": i["number"], "title": str(i.get("title") or ""), "body": str(i.get("body") or "")}
                for i in (raw if isinstance(raw, list) else []) if isinstance(i, dict) and "pull_request" not in i
                and isinstance(i.get("number"), int)]

    async def tarball(self, token: str, repo: str, sha: str) -> dict:
        data = await self.fetch(f"{github_app.API}/repos/{self._repo(repo)}/tarball/{sha}", token)
        if len(data) > MAX_TARBALL:
            raise TrialGitHubError("the demo repo is too big to build on")
        return unpack(data)

    async def pr_state(self, token: str, repo: str, number: int) -> str:
        data = await self.get(token, repo, f"/pulls/{int(number)}")
        return str(data.get("state", "")) if isinstance(data, dict) else ""

    # --- writes ----------------------------------------------------------------------------------
    async def open_pr(self, token: str, repo: str, base: tuple[str, str], branch: str, patch: list,
                      title: str, body: str) -> int:
        """The patch as one commit on a new trial/ branch (Git Data API), and its pull request."""
        commit = await self.get(token, repo, f"/git/commits/{base[1]}")
        tree_sha = (commit.get("tree") or {}).get("sha") if isinstance(commit, dict) else None
        entries = [{"path": c.path, "mode": c.mode, "type": "blob",
                    **({"sha": None} if c.data is None else {"content": c.data.decode("utf-8")})} for c in patch]
        tree = await self.write(token, repo, "POST", "/git/trees", {"base_tree": tree_sha, "tree": entries})
        made = await self.write(token, repo, "POST", "/git/commits",
                                {"message": title, "tree": tree["sha"], "parents": [base[1]]})
        await self.write(token, repo, "POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": made["sha"]})
        pr = await self.write(token, repo, "POST", "/pulls", {"title": title, "head": branch, "base": base[0], "body": body})
        number = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(number, int):
            raise TrialGitHubError("GitHub didn't say which pull request it opened")
        return number

    async def label(self, token: str, repo: str, number: int) -> None:
        await self.write(token, repo, "POST", f"/issues/{int(number)}/labels", {"labels": [LABEL]})

    async def comment_review(self, token: str, repo: str, number: int, body: str) -> None:
        await self.write(token, repo, "POST", f"/pulls/{int(number)}/reviews", {"event": "COMMENT", "body": body})

    async def recycle(self, token: str, repo: str, template_sha: str) -> None:
        """Back to the template: the default branch reset first (a refusal stops here, nothing else touched),
        then trial PRs closed and trial branches deleted."""
        branch, at = await self.base(token, repo)
        if at != template_sha:
            self.reset = (branch, template_sha)    # write() allows exactly this one reset (GPT review)
            try:
                await self.write(token, repo, "PATCH", f"/git/refs/heads/{branch}", {"sha": template_sha, "force": True})
            finally:
                self.reset = None
        for pr in await self.get(token, repo, "/pulls?state=open&per_page=100") or []:
            head = (pr.get("head") or {}) if isinstance(pr, dict) else {}
            same_repo = str((head.get("repo") or {}).get("full_name", "")).lower() == repo.lower()
            if same_repo and str(head.get("ref", "")).startswith(BRANCH_PREFIX):   # ours, never a fork's (GPT review)
                await self.write(token, repo, "PATCH", f"/pulls/{int(pr['number'])}", {"state": "closed"})
        for ref in await self.get(token, repo, f"/git/matching-refs/heads/{BRANCH_PREFIX}") or []:
            name = str(ref.get("ref", "")).removeprefix("refs/heads/") if isinstance(ref, dict) else ""
            if name.startswith(BRANCH_PREFIX):
                await self.write(token, repo, "DELETE", f"/git/refs/heads/{name}")
