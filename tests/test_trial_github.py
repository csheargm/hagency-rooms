"""Trial builds' GitHub client (server/trial_github.py, ADR 0027): the write allowlist, tokens scoped to
one demo repo and revoked, the tarball unpacked safely, and recycling a lease back to the template.
Run: .venv/bin/python -m unittest tests.test_trial_github"""
import asyncio
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from server import trial_github as tg
from server import trial_pool
from tests import trial_build_kit as kit
from tests.fake_trial_github import FakeTrialGitHub, tarball


def run(coro):
    return asyncio.run(coro)


class Allowlist(unittest.TestCase):
    def test_only_the_trial_writes_pass(self):
        tg.check_write("POST", "/git/refs", {"ref": "refs/heads/trial/1-ab", "sha": "x"})
        tg.check_write("POST", "/pulls/3/reviews", {"event": "COMMENT", "body": "x"})
        tg.check_write("PATCH", "/pulls/3", {"state": "closed"})
        for method, sub, body in [("PUT", "/pulls/3/merge", {}), ("POST", "/git/refs", {"ref": "refs/heads/main"}),
                                  ("POST", "/pulls/3/reviews", {"event": "APPROVE"}), ("PATCH", "/pulls/3", {"title": "x"}),
                                  ("PUT", "/contents/.github/workflows/x.yml", {}), ("POST", "/hooks", {}),
                                  ("DELETE", "/git/refs/heads/main", None), ("PUT", "/collaborators/x", {}),
                                  ("POST", "/actions/workflows/x/dispatches", {})]:
            with self.assertRaises(tg.TrialGitHubError, msg=f"{method} {sub}"):
                tg.check_write(method, sub, body)

    def test_only_the_demo_repos(self):
        gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=FakeTrialGitHub({kit.REPO: kit.TEMPLATE}).call)
        with self.assertRaises(tg.TrialGitHubError):
            run(gh.token("csheargm/hagency-rooms"))


class Tokens(unittest.TestCase):
    def setUp(self):
        self.fake = FakeTrialGitHub({kit.REPO: kit.TEMPLATE})
        self.gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=self.fake.call, fetch=self.fake.fetch)

    def test_read_and_write_tokens_ask_only_for_what_they_need(self):
        read, write = run(self.gh.token(kit.REPO)), run(self.gh.token(kit.REPO, write=True))
        self.assertEqual(self.fake.tokens[read]["permissions"], tg.READ)
        self.assertEqual(self.fake.tokens[write]["permissions"], tg.WRITE)
        mint = [b for m, p, a, b in self.fake.requests if p.endswith("/access_tokens")]
        self.assertEqual({tuple(b["repositories"]) for b in mint}, {("demo-1",)})
        run(self.gh.revoke(read))
        self.assertTrue(self.fake.tokens[read]["revoked"])

    def test_a_read_token_cant_write(self):
        token = run(self.gh.token(kit.REPO))
        with self.assertRaises(tg.TrialGitHubError):
            run(self.gh.write(token, kit.REPO, "POST", "/git/trees", {"tree": []}))

    def test_the_tarball_is_read_into_memory_without_its_top_folder(self):
        token = run(self.gh.token(kit.REPO))
        files = run(self.gh.tarball(token, kit.REPO, "a" * 40))
        self.assertEqual(files["src/calc.py"], ("100644", kit.BROKEN.encode()))


class Unpack(unittest.TestCase):
    def test_symlinks_keep_their_mode_and_no_content(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            link = tarfile.TarInfo("top/evil")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)
            exe = tarfile.TarInfo("top/run.sh")
            exe.size, exe.mode = 3, 0o755
            tar.addfile(exe, io.BytesIO(b"hi\n"))
        files = tg.unpack(raw.getvalue())
        self.assertEqual(files, {"evil": ("120000", b""), "run.sh": ("100755", b"hi\n")})

    def test_garbage_is_refused(self):
        with self.assertRaises(tg.TrialGitHubError):
            tg.unpack(b"not a tarball")
        self.assertIn("src/calc.py", tg.unpack(tarball(kit.TEMPLATE)))


class Recycle(unittest.TestCase):
    def test_a_lease_ends_when_its_pr_closes_and_the_next_visitor_gets_the_template_back(self):
        fake = FakeTrialGitHub({kit.REPO: kit.TEMPLATE})
        gh = tg.TrialGitHub("1", str(kit.key_file()), [kit.REPO], call=fake.call, fetch=fake.fetch)
        pool = trial_pool.Pool(Path(tempfile.mkdtemp()) / "pool.json", [kit.REPO], 7)
        template = fake.head[kit.REPO]
        self.assertEqual(run(pool.lease(gh, 1)), kit.REPO)
        with self.assertRaises(trial_pool.NoRepoFree):
            run(pool.lease(gh, 2))                             # leased, nothing opened yet: still theirs
        fake.pulls[100] = {"repo": kit.REPO.lower(), "head": "trial/1-x", "state": "open", "base": "main", "title": "", "body": ""}
        fake.refs[(kit.REPO.lower(), "refs/heads/trial/1-x")] = "c" * 40
        pool.opened(kit.REPO, 100)
        with self.assertRaises(trial_pool.NoRepoFree):
            run(pool.lease(gh, 2))                             # its PR is open: still theirs
        fake.pulls[100]["state"] = "closed"
        fake.head[kit.REPO.lower()] = "d" * 40                 # a person merged something meanwhile
        self.assertEqual(run(pool.lease(gh, 2)), kit.REPO)
        self.assertEqual(fake.head[kit.REPO.lower()], template)   # reset to the template
        self.assertFalse(fake.refs)                            # trial branches deleted
        self.assertTrue(all(t["revoked"] for t in fake.tokens.values()))


if __name__ == "__main__":
    unittest.main()
