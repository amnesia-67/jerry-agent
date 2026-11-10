# Jerry

A Discord bot that does nothing until you `@` it, then hands your request to a
real CLI coding agent running against a real repo on this machine.

Built for a hackathon: one enormous untouched codebase, several people, one
chat channel, and nobody wanting to babysit a terminal.

```
@jerry the payments module has no tests. fix that.
```

Jerry opens a thread, spins up Gemini CLI in the workspace, streams progress,
and posts the agent's summary when it's done.

## How it decides to care

Jerry reads **only** messages that mention him — an explicit `@jerry`, or a
reply to one of his own messages (Discord counts that as a ping). Everything
else in the channel is invisible to him, so the privileged *message content*
intent stays off.

## Agents

Each backend is an interactive coding CLI driven in headless mode. **Auth is
the CLI's own signed-in account — a paid subscription, not an API key.** Jerry
never sees a credential; it shells out to a binary that is already logged in.

| key | aliases | CLI | sign in with |
| --- | --- | --- | --- |
| `claude` | | Claude Code | Claude Pro or Max |
| `antigravity` | | Antigravity CLI (`agy`) | Google account / AI Plus, Pro or Ultra |
| `grok` | `grokbot`, `xai` | Grok Build | X/xAI browser sign-in, or `XAI_API_KEY` |
| `codex` | `chatgpt`, `gpt` | OpenAI Codex CLI | ChatGPT Plus / Pro / Team |
| `gemini` | | Gemini CLI | **personal sign-in discontinued** — see below |
| `codex` | OpenAI Codex CLI | ChatGPT Plus / Pro / Team |
| `copilot` | GitHub Copilot CLI | GitHub Copilot subscription |
| `cursor` | Cursor Agent | Cursor Pro |
| `qwen` | Qwen Code | Qwen account |
| `opencode` | opencode | whichever provider you log it into |
| `amp` | Amp | Sourcegraph account |

Install at least one and log in **once, interactively**, before starting Jerry:

```bash
npm i -g @anthropic-ai/claude-code
```

```bash
claude
```

Then `/login`. The credentials are cached, and every headless run Jerry makes
reuses them. Same idea for the others (`codex`, `copilot` + `/login`,
`cursor-agent login`, `agy`).

`/doctor` tells you which ones Jerry can actually see — and, importantly,
which are on `PATH` but don't run. A `copilot` that resolves to a VS Code shim
looks installed until you execute it.

### A note on Gemini

Google stopped serving Gemini CLI for personal accounts on **2026-06-18** —
Gemini Code Assist for individuals, Google AI Pro and AI Ultra all included.
Signing in succeeds and then requests fail with `IneligibleTierError` /
`UNSUPPORTED_CLIENT`. The `gemini` backend is kept because paid Gemini API keys
and Code Assist Standard/Enterprise licences still work, but on a personal plan
use `antigravity` instead — it's Google's official successor and runs on the
same subscription:

```bash
irm https://antigravity.google/cli/install.ps1 | iex
```

```bash
agy
```

Sign in with Google once; headless runs reuse the keyring session. Note that
`agy` in its default headless mode *soft-denies* anything it can't get approval
for — **the run exits 0 having skipped the edit**. Set `JERRY_FULL_AUTO=1` if it
needs to actually change files and run commands unattended.

### Fixing a CLI's flags without touching code

CLI flags move around between releases. Drop an `agents.local.json` next to
`bot.py` to patch any backend, or add a new one:

```json
{
  "gemini": { "default_model": "gemini-2.5-pro" },
  "claude": { "argv": ["{AUTO}", "{MODEL}", "-p", "{prompt}"] },
  "mything": {
    "binary": "mything",
    "argv": ["run", "--headless", "{prompt}"],
    "label": "My Agent",
    "emoji": "🦀"
  }
}
```

`{prompt}` is the task, `{MODEL}` and `{AUTO}` expand to zero or more arguments
in place (so sub-commands like `codex exec` stay first).

## Setup

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
```

```bash
copy .env.example .env
```

Fill in `DISCORD_TOKEN` and, importantly, `JERRY_WORKSPACE` — the repo agents
are allowed to edit. Then:

```bash
.venv\Scripts\python bot.py
```

In the Discord developer portal the bot needs the `bot` and
`applications.commands` scopes, and these permissions: Send Messages, Send
Messages in Threads, Create Public Threads, Embed Links, Attach Files, Add
Reactions, Read Message History. No privileged intents required.

Set `JERRY_GUILD_IDS` to your server id so slash commands register instantly
instead of waiting on Discord's global sync.

## Running agents on other people's machines

By default every agent runs wherever the bot runs — one machine, one working
tree, one person's subscriptions. That stops scaling the moment a second person
uses it.

So Jerry can split in two. **Master Jerry** is the bot everyone talks to.
**Sub-Jerries** run on their owners' machines, each signed into that person's
own CLI, each with its own checkout. Master routes a request to the right
sub, the agent runs there, and the answer comes back to the thread. One
`@Jerry` for the team, work spread across everyone's hardware and quota.

Discord itself is the transport, which is the point: master usually lives on a
laptop behind NAT, so nothing can connect *to* it. But every machine involved
is already connected to Discord, so there's no tunnel, no database and no
public address anywhere.

```
you ──@Jerry──> master ──dispatch channel──> sub on Luzhnov's laptop
                      <──result envelope────      (runs grok there)
```

### Setting it up

Make a **private** channel for dispatch — say `#jerry-dispatch` — that only
master and the sub-bots can post in. Then on master:

```
JERRY_DISPATCH_CHANNEL_ID=1570000000000000000
JERRY_SUBS=a.o.luzhnov:1570000000000000001, papai0.3:1570000000000000002
```

Anyone not listed in `JERRY_SUBS` still runs on master's machine, so you can
move people over one at a time.

Each teammate clones this repo and runs one command:

```bash
python setup_sub.py
```

It builds the venv, asks for their handle, their own bot token, master's id and
the channel id, picks an agent, sets up the workspace (cloning it if needed),
writes their `.env`, prints their bot's invite URL, and prints the exact
`JERRY_SUBS` line to send you. Then:

```bash
.venv\Scripts\python subjerry.py
```

`/subs` asks every configured sub to identify itself and reports who answered,
what they have installed, which machine, and whether they're on full auto.

### What keeps this safe

A sub-Jerry runs whatever an agent decides to do, on someone's real computer.
It accepts a job only when **all three** of these hold: the message is from the
configured `JERRY_MASTER_ID`, it is in the configured dispatch channel, and it
mentions that sub. Anything else — a teammate posting a perfect-looking
envelope, master talking in another channel, a different bot — is ignored, and
a missing setting fails closed rather than open.

That is why the dispatch channel must be private. Anyone who can post there as
master could run commands on your teammates' machines.

Two further things worth knowing. A sub never grants more than its own owner
configured: master can ask for full auto, but if that machine has
`JERRY_FULL_AUTO=0` it stays off there. And the whole job travels as a JSON
attachment rather than in message text, because a bot only receives message
content when mentioned — attachments are always readable, so neither side needs
the privileged message-content intent.

## Slash commands

| command | what it does |
| --- | --- |
| `/ask prompt:… [agent] [thread]` | same as mentioning him, with a one-off agent override |
| `/agent list` | the roster, and which CLIs are actually installed |
| `/agent use <agent>` | set the agent for this channel |
| `/agent server <agent>` | set the server-wide default (Manage Server) |
| `/agent reset` | drop the channel override |
| `/model <id\|clear>` | pin a model for this channel |
| `/workspace [path] [server_wide]` | show, or move, the folder agents work in (Manage Server to change) |
| `/status` | what's running, where, and what's queued |
| `/subs` | which sub-Jerries are alive and what they have |
| `/cancel` | kill this channel's run, children included |
| `/reset` | forget this thread's conversation history |
| `/doctor` | installed CLIs, versions, workspace, git status, access rules |
| `/ping` | proof of life |
| `/help` | the menu |

## How a request flows

1. Mention Jerry (or `/ask`). He reacts 👀.
2. He opens a thread named after your request.
3. The task is wrapped in a brief that tells the agent it's headless, where the
   repo is, not to ask questions, not to commit, and to finish with a short
   summary. The last few exchanges in that thread ride along as context.
4. The CLI runs in `JERRY_WORKSPACE` with its output streamed into a status
   message that updates every 12s.
5. On exit the status message is replaced by the agent's answer — chunked to
   fit Discord, or attached as a `.md` file if it's long. ✅ or 💥 on your
   original message.

One run at a time per channel (two agents in one working tree is a bad time);
`JERRY_MAX_PARALLEL` caps the whole bot. Follow-up requests queue behind.

## Things worth knowing

- **Jerry edits real files.** Keep the workspace on a scratch branch and read
  the diffs. He will not commit or push unless you ask in the request.
- **Set an allowlist.** With `JERRY_ALLOWED_USERNAMES`,
  `JERRY_ALLOWED_USER_IDS` and `JERRY_ALLOWED_ROLE_IDS` all empty, anybody who
  can see the bot can run a coding agent on this machine. Jerry logs a warning
  about this on startup, and `/doctor` shows who is on the list.

  Usernames are the @handle, not the per-server nickname, so changing a
  display name gets nobody in. Matching ignores case and tolerates a copied
  `@` or leading `.`. A handle *can* be changed by its owner though, so for a
  hard lock use a numeric id. Setting any one of the lists refuses everyone
  who doesn't match — including when the values turn out to be unparseable, so
  a typo locks the bot rather than opening it.

### Giving different people different models

`JERRY_ALLOWED_USERNAMES` grants every backend. To let someone in but limit
them to specific ones, use `JERRY_USER_AGENTS`:

```
JERRY_USER_AGENTS=a.o.luzhnov:grok, papai0.3:chatgpt, sam:claude|grok
```

Entries are comma separated, `handle:agent`, with several agents joined by `|`
and `*` meaning all of them. Aliases resolve, so `chatgpt` records `codex`.
Being granted a model also gets you through the door — you don't list the
person twice.

Each person's **default is their own**. The channel default applies when
they're allowed to use it; otherwise they silently fall back to a backend they
do have, preferring one that's installed, and Jerry says which it picked. So
somebody granted only Grok can just `@Jerry` and get Grok without knowing or
caring what the channel is set to.

They also can't reach the others: `/agent use`, `/ask agent:` and the mention
path all check the grant, `/agent list` shows only their own roster, and the
autocomplete won't even offer the rest. `/doctor` prints the whole matrix.

An unrecognised agent name in a grant is kept rather than dropped — a typo
should narrow someone's access, never widen it.
- **`JERRY_FULL_AUTO=1`** removes each CLI's approval prompts *and* its sandbox.
  Faster, and exactly as dangerous as it sounds.
- Conversation memory and channel settings live in `jerry.state.json`, which is
  gitignored. `/reset` clears a thread's memory.
- Prompts are kept off the command line whenever it can't carry them safely —
  always for a Windows `.cmd` shim, which is how npm installs most of these
  agents and which Windows runs through `cmd.exe`, and for any prompt over
  6000 characters. The prompt then goes in a file and the agent is told to read
  it, so a message containing `"`, `&` or `%PATH%` can't be mangled or
  executed.

  That file lands in `<workspace>/.jerry/`, **not** the system temp directory,
  because agents sandbox their reads to the working directory — a task file
  outside it is one the agent isn't allowed to open, and it will report a
  permission error instead of doing the work. The directory holds a
  `.gitignore` of `*` so git ignores it and itself, leaving your repo's own
  ignore rules alone, and the task file is deleted when the run ends.

- If an agent reports success having done nothing, suspect the permission mode
  before suspecting the model. With `JERRY_FULL_AUTO=0`, `agy` and Claude will
  write files but *soft-deny* shell commands in headless mode — the run exits 0
  with a note on stderr. Set `JERRY_FULL_AUTO=1` for unattended work, or give
  `agy` scoped allow-rules in `~/.gemini/antigravity-cli/settings.json`
  (on Windows, matching subcommands needs the `regex:` prefix, so
  `command(regex:git .*)` rather than `command(git)`).

- An installer that edits your PATH can't reach processes that already exist,
  so a CLI installed after the bot started is invisible to it — and to the
  terminal that launched it. Jerry also checks the usual per-user install
  directories, so this resolves itself instead of needing a fresh terminal.

## Layout

```
bot.py                master Jerry: mentions, slash commands, routing
subjerry.py           sub-Jerry: runs agents on a teammate's own machine
setup_sub.py          one-shot setup a teammate runs to become a sub
jerry/bus.py          the dispatch protocol, and the trust gate
jerry/dispatch.py     master-side routing and result matching
jerry/agents.py       backend registry + headless process runner
jerry/jobs.py         per-channel queue, global parallelism cap, cancellation
jerry/config.py       env settings, per-channel overrides, thread memory
jerry/formatting.py   Discord-safe chunking, durations, rosters
jerry/quips.py        the personality. load-bearing.
```
