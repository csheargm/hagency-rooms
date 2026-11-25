# Contributing

Thanks for improving Hagency Rooms. Changes come in as pull requests, from your own fork.

## The quick way: ask your agent

In your private room with your own agent, describe the change ("make the transcript caption
shorter"). Your agent works on its own copy, runs the tests and shows you what changed, and you
choose to use it yourself or send it here as a pull request. *(In-chat flow: in progress.)*

## By hand

```bash
git clone "https://github.com/YOUR-NAME/hagency-rooms" && cd hagency-rooms
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -p 'test_*.py'
```

The tests live in `tests/`. Run one file with `.venv/bin/python -m unittest tests.test_rules`.
The `tests/test_*_e2e.py` files are scripts that drive a live local server (`local/start.sh`); run them
by hand when you change chat behaviour, e.g. `.venv/bin/python -m tests.test_e2e`.

## What a good pull request has

- **One concern.** One fix or feature per pull request.
- **Tests.** A test for every new behaviour and a regression test for every bug fix. The test
  workflow must pass.
- **Small files.** Keep functions under 50 lines and files under 300. Split a file rather than
  growing it past that.
- **No secrets.** Never commit `.env`, tokens, passwords or room data.
- **The why.** Say why in the commit message, not just what changed.
- **A release number.** The pull request that makes a release bumps `VERSION` (major.minor.patch:
  patch for fixes, minor for features, major for changes people must act on). After it merges, tag
  that commit `v<number>` on GitHub. The panel's footer and My agents show it next to the commit.
- **Security.** Changes to the owner check or the agent sandbox need a note in the PR
  description; see [SECURITY.md](SECURITY.md).

By contributing you agree your work is released under the [MIT License](LICENSE).
