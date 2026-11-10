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

| key | CLI | sign in with |
| --- | --- | --- |
| `claude` | Claude Code | Claude Pro or Max |
| `antigravity` | Antigravity CLI (`agy`) | Google account / AI Plus, Pro or Ultra |
| `gemini` | Gemini CLI | **personal sign-in discontinued** — see below |
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
| `/status` | what's running and what's queued |
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
  hard lock use a numeric id. Setting any one of the three lists refuses
  everyone who doesn't match — including when the values turn out to be
  unparseable, so a typo locks the bot rather than opening it.
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
bot.py                Discord layer: mention handling, slash commands
jerry/agents.py       backend registry + headless process runner
jerry/jobs.py         per-channel queue, global parallelism cap, cancellation
jerry/config.py       env settings, per-channel overrides, thread memory
jerry/formatting.py   Discord-safe chunking, durations, rosters
jerry/quips.py        the personality. load-bearing.
```
