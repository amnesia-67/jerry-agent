# Set up a sub-Jerry on this machine

**For the human reading this:** hand this whole file to your coding agent
(Claude Code, Cursor, Codex, whatever you use) and tell it to follow it. It
will do everything except one browser sign-in, which only you can do.

**For the agent:** you are setting up a long-running Discord bot called
sub-Jerry on this computer. Follow the steps in order. Do not skip the
verification step. Stop and ask the human where a step says to.

---

## Values for this machine

> Everything in this block is filled in by whoever runs master Jerry. If any
> of it still says `FILL_IN`, stop and ask them for it — guessing will produce
> a bot that silently never answers.

```
YOUR_DISCORD_HANDLE   = FILL_IN        # no @, lowercase, e.g. a.o.luzhnov
SUB_BOT_TOKEN         = FILL_IN        # this machine's own bot token
MASTER_BOT_ID         = FILL_IN        # an ID, not a token. public, 17+ digits
DISPATCH_CHANNEL_ID   = FILL_IN        # the private channel it listens in
AGENT                 = FILL_IN        # claude | antigravity | grok | codex
WORKSPACE             = FILL_IN        # folder the agent may edit here
REPO_TO_CLONE         = FILL_IN        # git URL, or "-" if WORKSPACE exists
FULL_AUTO             = 0              # 1 lets agents run shell commands here
```

`SUB_BOT_TOKEN` is a **secret**. It goes in `.env` and nowhere else. Never
commit it, never paste it into a chat channel, never echo it to the terminal.

There is deliberately **no master token** in this list, and there should never
be one. `MASTER_BOT_ID` is only a user id — public, and useless on its own.
Master Jerry's token stays with whoever runs master and is shared with nobody.
A sub-Jerry holding master's token would be a second master, not a sub;
`setup_sub.py --check` refuses that outright.

---

## What this machine ends up doing

A single master Jerry sits in Discord and everyone talks to it. When this
machine's owner asks for something, master hands the job to this sub-Jerry,
the coding agent runs **here** against `WORKSPACE` using **this** machine's
CLI login, and the answer travels back through master to the Discord thread.

So: this person's work uses their own subscription, their own checkout, and
their own CPU — not the host's.

---

## Step 1 — prerequisites

Check each, and tell the human what's missing rather than installing system
software yourself:

- **Python 3.10 or newer** — `python --version` (try `python3` too)
- **Node.js and npm** — `npm --version` (needed for most agent CLIs)
- **git** — `git --version`

## Step 2 — get the code

If you are not already inside a `jerry-agent` checkout:

```bash
git clone <the repo url the human gives you> jerry-agent
cd jerry-agent
```

## Step 3 — virtualenv and dependencies

Windows:

```bash
python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt
```

macOS or Linux:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
```

## Step 4 — the workspace

If `WORKSPACE` already exists as a directory, do nothing here.

If it does not and `REPO_TO_CLONE` is a git URL:

```bash
git clone <REPO_TO_CLONE> <WORKSPACE>
```

If it does not and `REPO_TO_CLONE` is `-`, create the empty directory.

## Step 5 — write `.env`

Create a file called `.env` in the repo root with exactly this content,
substituting the values from the block above. Write the file directly; do not
run `setup_sub.py` without `--check`, because it is interactive and will hang
waiting for input you cannot provide.

```
DISCORD_TOKEN=<SUB_BOT_TOKEN>

JERRY_SUB_OWNER=<YOUR_DISCORD_HANDLE>
JERRY_MASTER_ID=<MASTER_BOT_ID>
JERRY_DISPATCH_CHANNEL_ID=<DISPATCH_CHANNEL_ID>

JERRY_WORKSPACE=<WORKSPACE>
JERRY_DEFAULT_AGENT=<AGENT>
JERRY_TIMEOUT=900
JERRY_FULL_AUTO=<FULL_AUTO>
```

`.env` is already covered by `.gitignore`. Confirm with
`git check-ignore -v .env` and stop if it reports nothing.

## Step 6 — install the agent CLI

Only the one named in `AGENT`:

| AGENT | install |
| --- | --- |
| `claude` | `npm i -g @anthropic-ai/claude-code` |
| `codex` | `npm i -g @openai/codex` |
| `grok` | `npm i -g @xai-official/grok` |
| `antigravity` | Windows: `irm https://antigravity.google/cli/install.ps1 \| iex` · macOS/Linux: `curl -fsSL https://antigravity.google/cli/install.sh \| bash` |

Note for `antigravity`: its installer appends to the user PATH, which does not
reach shells that are already open. Sub-Jerry searches the usual install
directories too, so this works anyway — but `agy --version` may fail in your
current shell until it is restarted. That alone is not a problem.

## Step 7 — STOP. The human signs in.

**You cannot do this step.** It opens a browser and needs their account.

Tell the human to run exactly one of these in a terminal, and to come back
when it's done:

| AGENT | they run | then |
| --- | --- | --- |
| `claude` | `claude auth login` | check with `claude auth status` |
| `codex` | `codex` | choose *Sign in with ChatGPT* |
| `grok` | `grok` | finish the browser sign-in |
| `antigravity` | `agy` | sign in with Google |

Do not try to automate this, do not ask them to paste a token or password to
you, and do not continue until they confirm.

## Step 8 — verify

```bash
.venv\Scripts\python setup_sub.py --check
```

(macOS/Linux: `.venv/bin/python setup_sub.py --check`)

This asks nothing and exits non-zero if something is wrong. Fix whatever it
reports and run it again. It prints this sub's **bot user id** — send that to
whoever runs master Jerry, they need it.

It cannot check whether the CLI is signed in. Step 7 is on trust.

## Step 9 — run it

```bash
.venv\Scripts\python subjerry.py
```

Expected output:

```
sub-Jerry up as <name> (<id>)
owner: @<handle> | workspace: <path>
obeying master id <id> in channel <id>
agents available here: <agent>
```

Leave it running — in its own terminal window, or `tmux`/`screen`. It does
nothing until master sends it a job. If you see
`Can't see channel ...`, the bot has not been added to the server or cannot
read that channel; tell the human to send their invite URL to the host.

## Report back

Tell the human, in one message they can forward to the host:

- the sub's bot user id from step 8
- which agent is installed and signed in here
- the workspace path
- whether `FULL_AUTO` is 0 or 1
- that `subjerry.py` is running

---

## Troubleshooting

| what you see | what it means |
| --- | --- |
| `Missing required settings` on start | a blank value in `.env` |
| `Can't see channel <id>` | bot not invited, or no read access to that channel |
| Nothing happens when a job is sent | wrong `JERRY_MASTER_ID` or wrong channel id — the sub ignores anything else by design |
| `<cli> isn't installed on this machine` in Discord | step 6 didn't take, or PATH is stale; try a fresh terminal |
| Jobs fail with an auth error | step 7 was skipped or signed into the wrong account |
| Job returns success but changed nothing | `FULL_AUTO=0`, so shell commands were denied; ask the human before changing it |

## Things not to do

- Do not commit `.env`, or print the token.
- Do not set `FULL_AUTO=1` on your own initiative. It lets agents run any
  shell command on this machine with no sandbox. That is the owner's call.
- Do not change `JERRY_MASTER_ID` or `JERRY_DISPATCH_CHANNEL_ID` to make
  something work. Those two are what stop anyone else from running code here.
- Do not run `subjerry.py` with a token that belongs to someone else.
