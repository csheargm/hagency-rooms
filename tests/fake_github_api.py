"""A small local stand-in for the GitHub REST/GraphQL API, as the "Hagency Rooms" App sees it.
NOT used in production; test_github_app.py points github_app.API at it.

It checks what GitHub checks: the App's JWT (RS256 with the App's public key, iss, at most 10
minutes long) for /app/* and installation lookups, and an installation token for everything else,
valid only on the repos it was minted for. Every request is recorded (method, path, auth).
"""
import json
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

MAX_JWT_S = 600


class FakeGitHub:
    def __init__(self, app_id: str, public_key) -> None:
        self.app_id, self.public_key = app_id, public_key
        self.installations: dict[str, int] = {}        # "owner/name" (lower) -> installation id
        self.issues: dict[str, list] = {}              # repo -> REST issue objects (PRs included, like GitHub)
        self.labels: dict[str, list] = {}
        self.prs: dict[tuple, dict] = {}               # (repo, number) -> GraphQL fields
        self.pulls: dict[str, list] = {}               # repo -> REST pull objects (open ones listed)
        self.pages: dict[str, dict] = {}
        self.permissions: dict[tuple, str] = {}        # (repo, login) -> permission
        self.private: set[str] = set()                 # "owner/name" (lower) of private repos; others public
        self.names: dict[str, str] = {}                # "owner/name" (lower) -> GitHub's own spelling
        self.token_ttl_s = 3600
        self.echo_auth = False                         # a misbehaving upstream: its errors repeat the credential
        self.tokens: dict[str, set] = {}               # token -> repos (lower) it may read
        # Connect GitHub (github_link.py): the device flow on github.com, and GET /user with a user token
        self.client_id, self.device_flow, self.interval = "Iv23liFAKECLIENT", True, 5
        self.devices: dict[str, dict] = {}             # device code -> {"user_code", "answers": [...], "login", "id"}
        self.user_tokens: dict[str, dict] = {}         # user token -> {"login", "id"}
        self.user_ids: dict[str, int] = {}             # login -> id, as the permission endpoint reports it
        self.requests: list[tuple] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- auth, as GitHub does it ---------------------------------------------------------
    def app_ok(self, auth: str) -> bool:
        try:
            claims = jwt.decode(auth, self.public_key, algorithms=["RS256"], options={"require": ["iat", "exp", "iss"]})
        except jwt.PyJWTError:
            return False
        return claims["iss"] == self.app_id and claims["exp"] - claims["iat"] <= MAX_JWT_S

    def mint(self, inst: int, body: dict) -> tuple[int, dict]:
        repos = {r for r, i in self.installations.items() if i == inst}
        owner = next(iter(repos)).split("/")[0] if repos else ""
        wanted = {f"{owner}/{n}".lower() for n in body.get("repositories", [])} or repos
        if not wanted <= repos:
            return 422, {"message": "repository not in installation"}
        tok = f"ghs_fake{len(self.tokens)}"
        self.tokens[tok] = wanted
        exp = datetime.fromtimestamp(time.time() + self.token_ttl_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return 201, {"token": tok, "expires_at": exp}

    # --- Connect GitHub: the device flow -------------------------------------------------
    def approve(self, login: str, gid: int, answers=("authorization_pending",)) -> str:
        """The next device code GitHub hands out: these poll answers first, then a user token for login."""
        code = f"dev{len(self.devices)}"
        self.devices[code] = {"user_code": f"ABCD-{1000 + len(self.devices)}", "answers": list(answers),
                              "login": login, "id": gid, "issued": False}
        return code

    def device_code(self, body: dict) -> tuple[int, dict]:
        if body.get("client_id") != self.client_id:
            return 200, {"error": "incorrect_client_credentials"}
        if not self.device_flow:
            return 200, {"error": "device_flow_disabled"}
        code = next((c for c, d in self.devices.items() if not d["issued"]), None) or self.approve("nobody", 0)
        self.devices[code]["issued"] = True
        return 200, {"device_code": code, "user_code": self.devices[code]["user_code"], "expires_in": 900,
                     "interval": self.interval, "verification_uri": "https://github.com/login/device"}

    def device_poll(self, body: dict) -> tuple[int, dict]:
        device = self.devices.get(body.get("device_code"))
        if device is None or body.get("client_id") != self.client_id or \
                body.get("grant_type") != "urn:ietf:params:oauth:grant-type:device_code":
            return 200, {"error": "incorrect_device_code"}
        if device["answers"]:
            return 200, {"error": device["answers"].pop(0)}
        tok = f"ghu_fake{len(self.user_tokens)}"
        self.user_tokens[tok] = {"login": device["login"], "id": device["id"]}
        return 200, {"access_token": tok, "token_type": "bearer", "scope": ""}

    # --- endpoints ------------------------------------------------------------------------
    def answer(self, method: str, path: str, auth: str, body: dict) -> tuple[int, object]:
        route, _, _query = path.partition("?")
        parts = route.strip("/").split("/")
        if route == "/login/device/code" and method == "POST":
            return self.device_code(body)
        if route == "/login/oauth/access_token" and method == "POST":
            return self.device_poll(body)
        if route == "/user" and method == "GET":
            who = self.user_tokens.get(auth)
            return (200, dict(who)) if who else (401, {"message": "Bad credentials"})
        if route.startswith("/app/installations/") and route.endswith("/access_tokens") and method == "POST":
            return self.mint(int(parts[2]), body) if self.app_ok(auth) else (401, {"message": "Bad credentials"})
        if len(parts) == 4 and parts[0] == "repos" and parts[3] == "installation":
            if not self.app_ok(auth):
                return 401, {"message": "Bad credentials"}
            inst = self.installations.get(f"{parts[1]}/{parts[2]}".lower())
            return (200, {"id": inst}) if inst else (404, {"message": "Not Found"})
        if route == "/graphql" and method == "POST":
            return self.graphql(auth, body)
        if parts[0] != "repos" or len(parts) < 3:
            return 404, {"message": "Not Found"}
        repo = f"{parts[1]}/{parts[2]}"
        if repo.lower() not in self.tokens.get(auth, set()):
            return 401, {"message": "Bad credentials"}
        return self.repo_read(repo, parts[3:], _query)

    def repo_read(self, repo: str, rest: list[str], query: str) -> tuple[int, object]:
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if rest == ["issues"]:
            page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
            label = q.get("labels")
            items = [i for i in self.issues.get(repo, []) if i["state"] == q.get("state", "open")
                     and (not label or label in [lb["name"] for lb in i["labels"]])]
            return 200, items[(page - 1) * per:page * per]
        if len(rest) == 2 and rest[0] == "issues":
            found = [i for i in self.issues.get(repo, []) if str(i["number"]) == rest[1]]
            return (200, found[0]) if found else (404, {"message": "Not Found"})
        if rest == ["pulls"]:
            page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
            items = [p for p in self.pulls.get(repo, []) if p["state"] == q.get("state", "open")]
            return 200, items[(page - 1) * per:page * per]
        if rest == ["labels"]:
            page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
            return 200, self.labels.get(repo, [])[(page - 1) * per:page * per]
        if rest == ["pages"]:
            return (200, self.pages[repo]) if repo in self.pages else (404, {"message": "Not Found"})
        if not rest:
            return 200, {"full_name": self.names.get(repo.lower(), repo), "default_branch": "main",
                         "private": repo.lower() in self.private}
        if len(rest) == 3 and rest[0] == "rules" and rest[1] == "branches":
            return 200, [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
        if len(rest) == 3 and rest[0] == "collaborators" and rest[2] == "permission":
            perm = self.permissions.get((repo, rest[1]))
            user = {"login": rest[1], "id": self.user_ids.get(rest[1], 0)}
            return (200, {"permission": perm, "user": user}) if perm else (404, {"message": "Not Found"})
        return 404, {"message": "Not Found"}

    def graphql(self, auth: str, body: dict) -> tuple[int, object]:
        v = body.get("variables", {})
        repo = f"{v.get('owner')}/{v.get('name')}"
        if repo.lower() not in self.tokens.get(auth, set()):
            return 401, {"message": "Bad credentials"}
        pr = self.prs.get((repo, v.get("number")))
        return 200, {"data": {"repository": {"pullRequest": pr}}}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def handle_one(self, method: str) -> None:
                auth = self.headers.get("Authorization", "").removeprefix("Bearer ")
                n = int(self.headers.get("Content-Length") or 0)
                raw_body = self.rfile.read(n) if n else b""
                if self.headers.get("Content-Type") == "application/x-www-form-urlencoded":
                    body = dict(urllib.parse.parse_qsl(raw_body.decode()))      # github.com's sign-in endpoints
                else:
                    body = json.loads(raw_body) if raw_body else {}
                fake.requests.append((method, self.path, auth))
                status, data = fake.answer(method, self.path, auth, body)
                if fake.echo_auth and status >= 400:
                    data = {"message": f"{data.get('message')} for {auth}"}
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.handle_one("GET")

            def do_POST(self):
                self.handle_one("POST")

            def log_message(self, *args):
                pass              # quiet test output

        return Handler
