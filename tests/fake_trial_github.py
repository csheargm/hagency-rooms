"""An in-memory GitHub for trial builds' tests (NOT used in production): the `call` that
server/trial_github.TrialGitHub makes, for the endpoints it uses. It checks what GitHub would: a token
is valid only for the repo it was minted for, only with the permissions asked for, and not once
revoked; a write needs the write permission. Every request is recorded (method, path, token, body).
"""
import gzip
import io
import itertools
import re
import tarfile

APP_JWT_HINT = "eyJ"               # what PyJWT's RS256 tokens start with
_shas = (f"{n:040x}" for n in itertools.count(1))


def tarball(files: dict, top: str = "hagency-trials-demo-1-abc123") -> bytes:
    """A gzipped tar of {path: str | bytes}, under one top folder, like GitHub's."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path, data in files.items():
            data = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


class FakeTrialGitHub:
    def __init__(self, repos: dict) -> None:
        """repos: {"org/name": {path: content}} (the template each demo repo starts as)."""
        self.files = {r.lower(): dict(f) for r, f in repos.items()}
        self.head = {r.lower(): next(_shas) for r in repos}
        self.template = dict(self.head)
        self.issues = {r.lower(): [{"number": 1, "title": "add() subtracts", "body": "add(2, 3) gives -1; it must give 5.",
                                    "labels": [{"name": "agent-ok"}]}] for r in repos}
        self.tokens: dict[str, dict] = {}         # token → {"repo", "permissions", "revoked"}
        self.requests: list[tuple] = []
        self.trees, self.commits, self.refs, self.pulls, self.reviews, self.labels = {}, {}, {}, {}, [], []
        self.installed = True

    # --- helpers -------------------------------------------------------------------------------
    def _token(self, auth: str, repo: str, need: str | None = None, perm: str = "contents") -> bool:
        t = self.tokens.get(auth)
        if not t or t["revoked"] or t["repo"] != repo.lower():
            return False
        return need is None or t["permissions"].get(perm) == need

    def open_prs(self, repo: str) -> list:
        return [p for p in self.pulls.values() if p["repo"] == repo.lower() and p["state"] == "open"]

    # --- the API -------------------------------------------------------------------------------
    async def call(self, method: str, path: str, auth: str, body: dict | None = None):
        self.requests.append((method, path, auth, body))
        if path == "/installation/token" and method == "DELETE":
            if auth in self.tokens:
                self.tokens[auth]["revoked"] = True
            return 204, {}
        if m := re.match(r"^/app/installations/1/access_tokens$", path):
            if not auth.startswith(APP_JWT_HINT):
                return 401, {"message": "a JWT is needed"}
            repo = next(r for r in self.files if r.endswith("/" + body["repositories"][0].lower()))
            token = f"ghs_fake{len(self.tokens)}{'x' * 30}"
            self.tokens[token] = {"repo": repo, "permissions": body["permissions"], "revoked": False}
            return 201, {"token": token, "expires_at": "2099-01-01T00:00:00Z"}
        m = re.match(r"^/repos/([^/]+/[^/?]+)(.*)$", path)
        if not m:
            return 404, {"message": "Not Found"}
        repo, sub = m.group(1).lower(), m.group(2)
        if sub == "/installation":
            return (200, {"id": 1}) if self.installed and auth.startswith(APP_JWT_HINT) else (404, {})
        if repo not in self.files:
            return 404, {"message": "Not Found"}
        if method == "GET":
            return self._get(auth, repo, sub)
        if not self._token(auth, repo, "write", "pull_requests" if sub.startswith("/pulls") else
                           "issues" if sub.startswith("/issues") else "contents"):
            return 403, {"message": "Resource not accessible by integration"}
        return self._write(repo, method, sub, body or {})

    def _get(self, auth: str, repo: str, sub: str):
        if not self._token(auth, repo):
            return 401, {"message": "Bad credentials"}
        if sub == "":
            return 200, {"default_branch": "main", "private": True}
        if sub == "/git/ref/heads/main":
            return 200, {"object": {"sha": self.head[repo]}}
        if sub.startswith("/issues?"):
            return 200, self.issues[repo]
        if m := re.match(r"^/git/commits/([0-9a-f]{40})$", sub):
            return 200, {"tree": {"sha": "t" + m.group(1)[1:]}}
        if m := re.match(r"^/pulls/(\d+)$", sub):
            pr = self.pulls.get(int(m.group(1)))
            return (200, {"state": pr["state"]}) if pr else (404, {})
        if sub.startswith("/pulls?"):
            return 200, [{"number": n, "head": {"ref": p["head"]}} for n, p in self.pulls.items()
                         if p["repo"] == repo and p["state"] == "open"]
        if sub.startswith("/git/matching-refs/heads/trial/"):
            return 200, [{"ref": r} for (rp, r) in self.refs if rp == repo and r.startswith("refs/heads/trial/")]
        return 404, {"message": "Not Found"}

    def _write(self, repo: str, method: str, sub: str, body: dict):
        if sub == "/git/trees":
            sha = next(_shas)
            self.trees[sha] = body
            return 201, {"sha": sha}
        if sub == "/git/commits":
            sha = next(_shas)
            self.commits[sha] = body
            return 201, {"sha": sha}
        if sub == "/git/refs":
            self.refs[(repo, body["ref"])] = body["sha"]
            return 201, {"ref": body["ref"]}
        if method == "PATCH" and sub.startswith("/git/refs/heads/"):
            self.head[repo] = body["sha"]
            return 200, {}
        if method == "DELETE" and sub.startswith("/git/refs/heads/"):
            self.refs.pop((repo, "refs/heads/" + sub.removeprefix("/git/refs/heads/")), None)
            return 204, {}
        if sub == "/pulls":
            number = 100 + len(self.pulls)
            self.pulls[number] = {"repo": repo, "head": body["head"], "base": body["base"], "title": body["title"],
                                  "body": body["body"], "state": "open"}
            return 201, {"number": number}
        if m := re.match(r"^/pulls/(\d+)$", sub):
            self.pulls[int(m.group(1))]["state"] = body["state"]
            return 200, {}
        if m := re.match(r"^/issues/(\d+)/labels$", sub):
            self.labels.append((int(m.group(1)), body["labels"]))
            return 200, []
        if m := re.match(r"^/pulls/(\d+)/reviews$", sub):
            self.reviews.append((int(m.group(1)), body))
            return 200, {"id": 1}
        return 404, {"message": "Not Found"}

    async def fetch(self, url: str, token: str) -> bytes:
        """The tarball download (trial_github's fetch): only with a valid read token for that repo."""
        m = re.search(r"/repos/([^/]+/[^/]+)/tarball/([0-9a-f]{40})$", url)
        if not m or not self._token(token, m.group(1)):
            raise AssertionError("tarball fetched without a valid token for its repo")
        return tarball(self.files[m.group(1).lower()])
