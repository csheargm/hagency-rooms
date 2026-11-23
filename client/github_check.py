"""The connect line proves to @pm which GitHub account the agent signed in with (ADR 0017), so the person's
own "Connect GitHub" step isn't needed: one sign-in for both.

@pm gives a one-time check (server/github_proof.py); the agent puts it in a secret gist made with its own
gh sign-in; @pm reads the gist's owner from GitHub itself. The gist is deleted right after. No GitHub
token leaves this computer. Never fatal: the agent works the same without it.
"""
import json
import os
import subprocess
import urllib.error
import urllib.request

from common import github

TIMEOUT_S = 20
DESCRIPTION = "Hagency Rooms sign-in check (deleted in a moment)"


def _post(url: str, body: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:         # the server's own reason, said to the person
        try:
            return {"error": json.loads(e.read() or b"{}").get("error") or f"HTTP {e.code}"}
        except ValueError:
            return {"error": f"HTTP {e.code}"}


def _gh(agent: str, args: list[str], run=subprocess.run) -> str:
    """gh as this agent only (its own folder; other logins' tokens left out). Raises OSError on failure."""
    folder = github.IDENTITY_ROOT / agent
    env = {k: v for k, v in os.environ.items() if k not in github.SCRUBBED_ENV}
    env.update(GH_CONFIG_DIR=str(folder), GIT_CONFIG_GLOBAL=str(folder / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")
    done = run(["gh", *args], env=env, capture_output=True, text=True, timeout=60, check=False)
    if done.returncode != 0:
        raise OSError(f"gh {args[0]} failed")
    return done.stdout


def prove(creds: dict, server: str, run=subprocess.run, post=_post) -> str | None:
    """The GitHub login now linked to the agent's owner, or None (what went wrong is printed)."""
    agent = str(creds.get("user_id", "")).lstrip("@").partition(":")[0]
    signed = post(f"{creds['homeserver'].rstrip('/')}/_matrix/client/v3/login",
                  {"type": "m.login.password", "identifier": {"type": "m.id.user", "user": creds["user_id"]},
                   "password": creds["password"], "initial_device_display_name": "github check"})
    token = signed.get("access_token")
    if not token:
        print(f"note: couldn't link your GitHub account to you ({signed.get('error', 'sign-in failed')})")
        return None
    gist = None
    try:
        asked = post(f"{server.rstrip('/')}/connect/github/challenge", {}, token)
        nonce = asked.get("nonce")
        if not nonce:
            raise OSError(asked.get("error") or "no check from the server")
        made = json.loads(_gh(agent, ["api", "gists", "-X", "POST", "-F", "public=false", "-f", f"description={DESCRIPTION}",
                                      "-f", f"files[hagency-check.txt][content]={nonce}"], run))
        gist = made.get("id") if isinstance(made, dict) else None
        answer = post(f"{server.rstrip('/')}/connect/github/proof", {"nonce": nonce, "gist": gist}, token)
        if answer.get("login"):
            return answer["login"]
        print(f"note: couldn't link your GitHub account to you: {answer.get('error', 'no answer')}")
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as e:   # said; the agent still works
        print(f"note: couldn't link your GitHub account to you ({e})")
        return None
    finally:
        if gist:
            try:
                _gh(agent, ["api", "-X", "DELETE", f"gists/{gist}"], run)
            except (OSError, subprocess.SubprocessError):   # a secret gist with a used-up check: harmless
                print(f"note: couldn't delete the check gist {gist}; you can delete it on github.com")
        post(f"{creds['homeserver'].rstrip('/')}/_matrix/client/v3/logout", {}, token)   # this one-off session ends
