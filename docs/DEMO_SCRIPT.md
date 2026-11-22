# Demo script: a whole product cycle, driven by conversation

**Names.** `your-account` stands for your GitHub account (the room owner's) and `second-account` for a
second account that Betty's agent signs in as. Use your own wherever they appear in commands.

**Story.** Anna and Betty each bring their own AI agent into a project room. They talk about what to
build, and the Product Manager (`@pm`) turns that into GitHub issues. The agents build, review and
merge each other's work. Anna and Betty review it, try the result, ask for a change, and it gets
built too.

**Cast.**
- **Anna:** room owner. Her agent is Hermes (`@anna-agent`).
- **Betty:** contributor. Her agent is Claude Code (`@betty-agent`).
- **Product Manager** (`@pm`): plans, assigns, tracks, keeps the pinned **Project status**. It has
  no AI and no GitHub write access of its own.

Automated rehearsal of steps 5–9 (no Element, fake GitHub, Codex agents):
`.venv/bin/python -m tests.test_demo_e2e`. It finishes in about a minute.

---

## 0. Before the demo (once)

| What | How | Check |
|---|---|---|
| Server and Element running | `./start.sh` (Ctrl-C after it's up; step 3 restarts it) | http://localhost:8649 opens |
| Hermes installed, model signed in | `hermes model` (Anna's default profile; only its model key is reused) | `hermes status` |
| Claude Code signed in on this Mac | `claude` once, interactively | `claude -p "hi"` answers |
| Agents' GitHub identities | already signed in: `.gh/anna-agent` = your-account, `.gh/betty-agent` = second-account ([GITHUB_SETUP.md](GITHUB_SETUP.md)) | `.venv/bin/python github_setup.py check` |

## 1. Create the two agents (≈1 min)

```bash
.venv/bin/python onboard.py anna --agent hermes
```

```bash
.venv/bin/python onboard.py betty --agent claude
```

Each command does the following, and then prints how to sign in:
- creates the person's account and `<person>-agent` (passwords go to `.env` and are never printed);
- opens a private DM between the person and their agent;
- records which agent they bring;
- for Hermes, creates a clean room profile (`annaroom`) that obeys only Anna and `@pm`, has no
  file, shell or web tools, and edits repos only through the hagency MCP tools.

## 2. Create the project's repo (≈2 min)

```bash
gh repo create your-name/hagency-hello --private --add-readme
```

```bash
gh api -X PUT repos/your-name/hagency-hello/collaborators/second-account -f permission=push
```

Accept that invite as second-account (github.com/notifications, in the browser where second-account is
signed in). Then make GitHub the merge gate (a PR needs one approval; nobody bypasses), and check:

```bash
LOOP_REPO=your-name/hagency-hello .venv/bin/python github_setup.py ruleset
```

```bash
LOOP_REPO=your-name/hagency-hello .venv/bin/python github_setup.py check
```

Publish the repo as a website, so people can try each merged change in the browser. This is a
repo setting, so you do it, not the agents:

```bash
LOOP_REPO=your-name/hagency-hello .venv/bin/python github_setup.py pages
```

## 3. Start everyone's agents (≈10 s)

```bash
LOOP_REPO=your-name/hagency-hello ./start.sh
```

This starts `@pm`, Anna's Hermes gateway and Betty's Claude Code agent adapter. Saving a code file
restarts them without losing messages.

## 4. Create the project room and invite people (≈2 min, in Element)

1. **Anna** signs in at http://localhost:8649 (homeserver `http://localhost:8648`; password
   `PW_ANNA` in `.env`).
2. Create room **"Hello World"**.
   - ⚠️ Turn **off** "Enable end-to-end encryption": the agents can't read encrypted rooms yet.
3. Invite `@betty`, `@anna-agent`, `@betty-agent` and `@pm`, all `:bridge.localhost`.
4. `@pm` joins and asks to be a moderator. Room settings → Roles & Permissions → **Product
   Manager** → Moderator.
5. **Betty** signs in (a second browser or a private window, password `PW_BETTY`) and joins.
6. Optional, to show the agents are personal: Anna DMs her agent ("who are you?"). Betty tries
   `@anna-agent hello` in the room, and gets no answer: an agent obeys only its owner.

## 5. Decide what to build: just talk (≈3 min)

> **Anna:** Let's make a tiny Hello World web page for the demo.
> **Betty:** A name box and a Greet button? Empty name says "Hello, World!".
> **Anna:** `@anna-agent` anything we're missing for something this small?
> **anna-agent:** A README saying how to open it; and show the name as text, not HTML.
> **Betty:** Agreed — page, greeting, escaping, README. Keep it one index.html.

## 6. Turn the discussion into work

> **Anna:** `@pm` let's build it
> **Product Manager:** Here's the plan from your discussion: 1. Add index.html with name box and
> Greet button … 4. README … 👍 to open these 4 issue(s)

Anna reacts 👍. Then:

> **Product Manager:** Opened #1, #2, #3, #4. Say "take it all the way" or "let us approve each
> change" to start building.

- The plan is drafted by **Anna's own agent** on her model. The issues are opened under **Anna's
  agent's GitHub identity**.
- Only the owner or a maintainer can turn talk into work. If Betty says "@pm let's build it",
  she's told so.

## 7. Build, with people reviewing

> **Anna:** `@pm` let us approve each change

Watch the pinned **Project status**:
- `#1 … building · anna-agent · 1 min`
- then `reviewing · betty-agent`
- then `ready — a person merges`

The builders take turns. Each PR is reviewed by **the other person's agent**, and GitHub's rule
requires that approval. If the reviewer asks for changes, the builder's agent revises its PR
("fixing review feedback", up to 2 rounds) before a person is called in.

Human review: click the PR link in the room, read the change, and merge it on GitHub. Or, to let
the agents finish on their own:

> **Anna:** `@pm` take it all the way (it starts at once; "stop" undoes it)

> **Anyone:** `@pm` where are we? (mode, agents, link to the board) · `@pm` stop (immediately,
> anyone, no AI involved)

## 8. Test-drive the result

Open the live site from @pm's "🎉 All done … Try it:" message
(https://your-name.github.io/hagency-hello/), about a minute after the last merge. Or locally:

```bash
gh repo clone your-name/hagency-hello /tmp/hello -- --quiet && open /tmp/hello/index.html
```

Type a name, click Greet, try an empty name, and try `<b>x</b>`.

## 9. Iterate

> **Betty:** Works! Can it remember the last name I typed?
> **Anna:** Yes, add that. `@pm` let's build it

`@pm` plans **only the new item** (it shows the agent the open issues), Anna reacts 👍, and the
loop builds it. Refresh `index.html` after it's merged.

## 10. Wrap-up talking points

- **People decide what to build by talking.** Agents implement it. Nobody learns commands.
- **Bring your own agent:** Hermes and Claude Code in the same room. Each answers only to its
  owner and works under its owner's accounts.
- **Checks between people's agents:** a different person's agent reviews every change, GitHub's
  rule is the gate, and people keep the last word (✅, merge, "stop").
- **One live picture for everyone:** the pinned Project status.

## Reset between rehearsals

- Turn the room's loop off with `@pm stop`.
- Delete the test repo: `gh repo delete your-name/hagency-hello`. This is permanent, so only for
  a throwaway rehearsal repo.
- Remove the room in Element (leave and forget).

## If something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| An agent never answers in the room | the room is encrypted | create it unencrypted (step 4) |
| `@pm` says it can't change the mode | it isn't a moderator | Room settings → Roles → Product Manager → Moderator |
| "plan with your own agent … isn't in this room" | the requester's agent wasn't invited | invite `@<you>-agent` |
| A step says "no GitHub identity" | `.gh/<person>-agent` not signed in | [GITHUB_SETUP.md](GITHUB_SETUP.md), step 1 |
| PR stays "approved, waiting for GitHub" | ruleset missing | `github_setup.py ruleset`, then `check` |
| Hermes is slow or confused | small model | `onboard.py anna --agent codex` and restart `start.sh` |
