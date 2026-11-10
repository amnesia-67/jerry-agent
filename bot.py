"""Jerry: a Discord bot that hands your requests to a real CLI coding agent.

Mention Jerry, say what you want, and he hands the whole thing to a headless
coding agent (Gemini CLI by default) running in a chosen workspace. The agent
is authenticated by whatever paid account that CLI is already signed in to on
this machine; no API keys live here.

    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from jerry import agents, formatting as fmt, quips
from jerry.config import OVERRIDES_PATH, Settings, State, normalize_username
from jerry.jobs import Job, JobManager

load_dotenv()

# Windows consoles default to cp1252, and Discord display names are not. Logging
# a name with an emoji in it should not raise.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jerry")

SETTINGS = Settings.from_env()
agents.DEFAULT_AGENT = SETTINGS.default_agent
STATE = State(SETTINGS)
JOBS = JobManager(max_parallel=SETTINGS.max_parallel)

PROGRESS_EVERY = 12.0  # seconds between status-message edits
MAX_RESULT_CHUNKS = 3  # beyond this, the transcript goes out as a file

NO_PINGS = discord.AllowedMentions.none()

intents = discord.Intents.default()
# message_content stays OFF on purpose: Discord still delivers the text of any
# message that mentions the bot, which is the only kind Jerry reads. Flip
# JERRY_MESSAGE_CONTENT=1 (and tick the intent in the dev portal) if you ever
# want him to see everything.
if os.environ.get("JERRY_MESSAGE_CONTENT", "").strip().lower() in {"1", "true", "yes"}:
    intents.message_content = True


class Jerry(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.tree = app_commands.CommandTree(self)
        self.synced = False

    async def setup_hook(self) -> None:
        for note in agents.load_overrides(OVERRIDES_PATH):
            log.info("agent override: %s", note)
        if SETTINGS.guild_ids:
            for gid in SETTINGS.guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            log.info("slash commands synced to guilds: %s", sorted(SETTINGS.guild_ids))
            self.synced = True
        # Otherwise we wait for on_ready, where we know which guilds we are in:
        # a guild-scoped sync lands instantly, a global one can take an hour.

    async def sync_to_connected_guilds(self) -> None:
        """Register commands in every guild we can see. Idempotent."""
        if self.synced or SETTINGS.guild_ids:
            return
        self.synced = True
        guilds = self.guilds[:10]
        if not guilds:
            await self.tree.sync()
            log.info("no guilds yet; synced globally (can take up to an hour)")
            return
        for guild in guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("slash commands ready in %s (%s)", guild.name, guild.id)
            except discord.HTTPException as exc:
                log.warning("could not sync to %s: %s", guild.id, exc)


client = Jerry()
tree = client.tree


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def strip_mentions(content: str, user_id: int) -> str:
    cleaned = re.sub(rf"<@!?{user_id}>", " ", content or "")
    return " ".join(cleaned.split()).strip()


def authorized(user: discord.abc.User | discord.Member) -> bool:
    """Jerry runs agents on a real machine, so the allowlists matter."""
    if SETTINGS.allowed_user_ids and user.id in SETTINGS.allowed_user_ids:
        return True
    if SETTINGS.allowed_usernames:
        # `user.name` is the @handle, not the per-server nickname, so this
        # can't be spoofed by someone changing their display name.
        if normalize_username(user.name) in SETTINGS.allowed_usernames:
            return True
    if SETTINGS.allowed_role_ids and isinstance(user, discord.Member):
        if any(r.id in SETTINGS.allowed_role_ids for r in user.roles):
            return True
    # Fail closed: if an allowlist was configured at all, an unmatched user is
    # refused, even when the ids themselves turned out to be unparseable.
    return not SETTINGS.access_locked


def is_admin(user: discord.abc.User | discord.Member) -> bool:
    if isinstance(user, discord.Member):
        perms = user.guild_permissions
        return bool(perms.manage_guild or perms.administrator)
    return True  # a DM is a conversation with one person: themselves


def context_ids(channel: discord.abc.Messageable) -> tuple[int | None, int | None]:
    channel_id = getattr(channel, "id", None)
    guild = getattr(channel, "guild", None)
    return channel_id, getattr(guild, "id", None)


def resolve(
    channel: discord.abc.Messageable,
    agent_override: str | None = None,
) -> tuple[agents.AgentSpec | None, Path, str | None, str]:
    """Work out which agent, workspace and model apply right here."""
    channel_id, guild_id = context_ids(channel)
    key = agent_override or STATE.agent_key(channel_id, guild_id)
    spec = agents.get(key)
    workspace = STATE.workspace(channel_id, guild_id)
    model = STATE.model(channel_id, guild_id)
    return spec, workspace, model, key


def convo_id_for(channel: discord.abc.Messageable) -> int:
    return int(getattr(channel, "id", 0))


def thread_title(task: str) -> str:
    title = " ".join(task.split())[:72] or "jerry"
    return title


async def open_workspace_thread(
    channel: discord.abc.Messageable,
    task: str,
    source: discord.Message | None = None,
) -> discord.abc.Messageable:
    """Keep each request in its own thread when we are allowed to."""
    if not SETTINGS.use_threads:
        return channel
    if isinstance(channel, (discord.Thread, discord.DMChannel, discord.GroupChannel)):
        return channel
    if not isinstance(channel, discord.TextChannel):
        return channel
    try:
        if source is not None:
            return await source.create_thread(
                name=thread_title(task), auto_archive_duration=1440
            )
        return await channel.create_thread(
            name=thread_title(task),
            type=discord.ChannelType.public_thread,
            auto_archive_duration=1440,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.info("no thread (%s); working in the channel", exc)
        return channel


async def react(message: discord.Message | None, emoji: str) -> None:
    if message is None:
        return
    try:
        await message.add_reaction(emoji)
    except (discord.HTTPException, discord.Forbidden):
        pass


def status_text(job: Job, blurb: str, queue_note: str = "") -> str:
    spec = job.spec
    head = (
        f"{spec.emoji} **{spec.label}** is on it — *{blurb}*\n"
        f"`{fmt.human_duration(job.elapsed)}` · "
        f"workspace `{job.workspace.name}`"
    )
    if job.model:
        head += f" · model `{job.model}`"
    if queue_note:
        head += f"\n{queue_note}"
    tail = job.handle.tail(1) if job.handle else ""
    if tail:
        head += f"\n> {fmt.tidy_tail(tail)}"
    return head[: fmt.SAFE_LIMIT]


async def send_result(
    target: discord.abc.Messageable,
    job: Job,
    result: agents.AgentResult,
) -> None:
    spec = job.spec
    duration = fmt.human_duration(result.duration)

    if result.cancelled:
        await target.send(f"\U0001f6d1 {quips.cancelled()} (`{duration}` in)")
        return

    body = fmt.strip_noise(result.output)

    if result.error:
        await target.send(f"\U0001f480 {quips.failed()}\n```\n{result.error[:1500]}\n```")
        return

    if result.timed_out:
        header = (
            f"⏱️ {quips.timed_out()} "
            f"({spec.label} ran `{duration}`, limit is `{SETTINGS.timeout}s`)"
        )
    elif result.ok:
        header = f"{spec.emoji} **{spec.label}** · `{duration}` · {quips.done()}"
    else:
        header = (
            f"{spec.emoji} **{spec.label}** · `{duration}` · "
            f"exit `{result.exit_code}` — {quips.failed()}"
        )

    if not body:
        hint = ""
        if not result.ok:
            hint = (
                "\nNo output at all, which usually means the CLI wanted to ask a "
                "human something. Check `/doctor`, or that the CLI is signed in."
            )
        await target.send(f"{header}\n_(the agent said nothing){hint}")
        return

    pieces = fmt.chunk(body)
    if len(pieces) > MAX_RESULT_CHUNKS:
        await target.send(
            f"{header}\n_(long one — first slice below, full transcript attached)_\n\n"
            + pieces[0],
            file=fmt.as_file(body, f"jerry-{job.job_id}.md"),
        )
        return

    await target.send(f"{header}\n\n{pieces[0]}")
    for piece in pieces[1:]:
        await target.send(piece)


# --------------------------------------------------------------------------- #
# The actual dispatch
# --------------------------------------------------------------------------- #


async def dispatch(
    *,
    target: discord.abc.Messageable,
    status_msg: discord.Message,
    requester: discord.abc.User | discord.Member,
    task: str,
    spec: agents.AgentSpec,
    workspace: Path,
    model: str | None,
    source: discord.Message | None = None,
) -> None:
    """Queue one agent run and babysit the status message while it goes."""
    channel_id = int(getattr(target, "id", 0))
    convo = convo_id_for(target)

    job = Job(
        channel_id=channel_id,
        requester=requester.display_name,
        requester_id=requester.id,
        task=task,
        spec=spec,
        workspace=workspace,
        model=model,
    )

    ahead = JOBS.submit(job, lambda j: _run(j, target, status_msg, convo, source))
    if ahead:
        try:
            await status_msg.edit(
                content=(
                    f"\U0001f6cf️ {quips.busy()}\n"
                    f"`#{job.job_id}` is {ahead} deep in the queue for this channel."
                ),
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass


async def _run(
    job: Job,
    target: discord.abc.Messageable,
    status_msg: discord.Message,
    convo: int,
    source: discord.Message | None,
) -> None:
    handle = agents.RunHandle(spec=job.spec)
    job.handle = handle
    blurb = quips.working()

    async def ticker() -> None:
        nonlocal blurb
        while True:
            await asyncio.sleep(PROGRESS_EVERY)
            blurb = quips.working()
            try:
                await status_msg.edit(
                    content=status_text(job, blurb), allowed_mentions=NO_PINGS
                )
            except discord.HTTPException:
                return

    try:
        await status_msg.edit(content=status_text(job, blurb), allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass

    tick = asyncio.create_task(ticker())
    prompt = agents.build_prompt(job.task, job.workspace, STATE.history(convo))

    log.info(
        "job #%s %s -> %s in %s", job.job_id, job.requester, job.spec.key, job.workspace
    )

    try:
        result = await agents.run_agent(
            job.spec,
            prompt,
            workspace=job.workspace,
            model=job.model,
            full_auto=SETTINGS.full_auto,
            timeout=SETTINGS.timeout,
            handle=handle,
        )
    except asyncio.CancelledError:
        tick.cancel()
        job.status = "cancelled"
        try:
            await status_msg.delete()
        except discord.HTTPException:
            pass
        await target.send(f"\U0001f6d1 {quips.cancelled()}")
        await react(source, "\U0001f6d1")
        raise
    finally:
        tick.cancel()

    job.status = "done" if result.ok else "failed"

    try:
        await status_msg.delete()
    except discord.HTTPException:
        pass

    await send_result(target, job, result)

    if result.ok and result.output:
        STATE.remember(convo, job.requester, job.task, fmt.strip_noise(result.output))
    await react(source, "✅" if result.ok else "\U0001f4a5")


async def begin(
    *,
    channel: discord.abc.Messageable,
    requester: discord.abc.User | discord.Member,
    task: str,
    agent_override: str | None = None,
    source: discord.Message | None = None,
    new_thread: bool = True,
    opening_line: str | None = None,
) -> str | None:
    """Validate everything, then hand off. Returns an error string if refused."""
    spec, workspace, model, key = resolve(channel, agent_override)
    if spec is None:
        return f"{quips.no_agent()} (`{key}` means nothing to me)"
    if not spec.available:
        return (
            f"{quips.no_binary()}\n`{spec.binary}` is not on this machine's PATH.\n"
            f"• install: `{spec.install}`\n• sign in: {spec.login}"
        )
    if not workspace.exists() or not workspace.is_dir():
        return (
            f"My workspace `{workspace}` does not exist, which makes coding hard.\n"
            "Point me somewhere real with `/workspace path:<folder>`."
        )

    target = channel
    if new_thread:
        target = await open_workspace_thread(channel, task, source)

    await react(source, "\U0001f440")
    status_msg = await target.send(
        opening_line or f"{quips.accepted(task)} — warming up {spec.label}…",
        allowed_mentions=NO_PINGS,
    )
    await dispatch(
        target=target,
        status_msg=status_msg,
        requester=requester,
        task=task,
        spec=spec,
        workspace=workspace,
        model=model,
        source=source,
    )
    return None


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


@client.event
async def on_ready() -> None:
    log.info("logged in as %s (%s)", client.user, getattr(client.user, "id", "?"))
    await client.sync_to_connected_guilds()
    log.info("workspace: %s", SETTINGS.workspace)
    log.info(
        "default agent: %s | timeout: %ss | parallel: %s | full_auto: %s",
        SETTINGS.default_agent,
        SETTINGS.timeout,
        SETTINGS.max_parallel,
        SETTINGS.full_auto,
    )
    found = [s.key for s in agents.available_specs()]
    log.info("agent CLIs on PATH: %s", ", ".join(found) if found else "NONE")
    if not found:
        log.warning(
            "No agent CLI found. Install at least one and sign it in, e.g. "
            "`npm i -g @google/gemini-cli` then run `gemini` once to log in."
        )
    if not SETTINGS.access_locked:
        log.warning(
            "No allowlist set: anyone who can see the bot can run an agent on this "
            "machine. Set JERRY_ALLOWED_USERNAMES or JERRY_ALLOWED_USER_IDS."
        )
    else:
        log.info(
            "allowlist — usernames: %s | user ids: %s | roles: %s",
            sorted(SETTINGS.allowed_usernames) or "none",
            sorted(SETTINGS.allowed_user_ids) or "none",
            sorted(SETTINGS.allowed_role_ids) or "none",
        )
    try:
        await client.change_presence(
            activity=discord.CustomActivity(name="@ me to put an agent to work")
        )
    except discord.HTTPException:
        pass


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or client.user is None:
        return
    # Mention-only, by design. Discord puts the bot in `mentions` for an
    # explicit @ and for a reply-ping, which is exactly the set we want.
    if client.user not in message.mentions:
        return
    if not authorized(message.author):
        await message.reply(
            f"You're not on my list, and I take my list seriously. I see you as "
            f"`@{message.author.name}` — pass that to whoever runs me.",
            mention_author=False,
            allowed_mentions=NO_PINGS,
        )
        return

    task = strip_mentions(message.content, client.user.id)
    # Other people's mentions are context, not commands; leave them as text.
    if not task:
        await message.reply(
            f"{quips.empty_prompt()}\nTry: `@{client.user.name} add a /health endpoint "
            "and a test for it` — or `/help` for the full menu.",
            mention_author=False,
            allowed_mentions=NO_PINGS,
        )
        return

    in_thread = isinstance(message.channel, discord.Thread)
    error = await begin(
        channel=message.channel,
        requester=message.author,
        task=task,
        source=message,
        new_thread=not in_thread,
    )
    if error:
        await message.reply(error, mention_author=False, allowed_mentions=NO_PINGS)


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #


async def agent_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for spec in agents.all_specs():
        if current and current not in spec.key and current not in spec.label.lower():
            continue
        tag = "" if spec.available else " (not installed)"
        out.append(app_commands.Choice(name=f"{spec.key} — {spec.label}{tag}", value=spec.key))
    return out[:25]


@tree.command(name="ask", description="Hand a task to a coding agent in the workspace.")
@app_commands.describe(
    prompt="What should the agent actually do?",
    agent="Use a different agent just for this run.",
    thread="Work in a new thread (default) or right here.",
)
@app_commands.autocomplete(agent=agent_autocomplete)
async def ask(
    interaction: discord.Interaction,
    prompt: str,
    agent: str | None = None,
    thread: bool = True,
) -> None:
    if not authorized(interaction.user):
        await interaction.response.send_message(
            "Not on my list, sorry. I don't run code for strangers.", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)
    spec, workspace, model, key = resolve(interaction.channel, agent)
    if spec is None or not spec.available or not workspace.is_dir():
        error = await _preflight_error(spec, key, workspace)
        await interaction.followup.send(error, ephemeral=True)
        return

    status_msg = await interaction.followup.send(
        f"{quips.accepted(prompt)} — taking this to {spec.label}…", wait=True
    )
    channel = interaction.channel
    target: discord.abc.Messageable = channel  # type: ignore[assignment]
    if thread and not isinstance(channel, discord.Thread):
        maybe = await open_workspace_thread(channel, prompt, None)  # type: ignore[arg-type]
        if maybe is not channel:
            target = maybe
            try:
                await status_msg.edit(
                    content=(
                        f"{spec.emoji} Took it to {maybe.mention} so we don't spam the "
                        "channel."
                    ),
                    allowed_mentions=NO_PINGS,
                )
            except discord.HTTPException:
                pass
            status_msg = await target.send(
                f"{quips.accepted(prompt)} — **{interaction.user.display_name}** asked: "
                f"{fmt.tidy_tail(prompt, 300)}",
                allowed_mentions=NO_PINGS,
            )

    await dispatch(
        target=target,
        status_msg=status_msg,
        requester=interaction.user,
        task=prompt,
        spec=spec,
        workspace=workspace,
        model=model,
    )


async def _preflight_error(
    spec: agents.AgentSpec | None, key: str, workspace: Path
) -> str:
    if spec is None:
        return f"{quips.no_agent()} (`{key}` is not one of mine)"
    if not spec.available:
        return (
            f"{quips.no_binary()}\n`{spec.binary}` is not on PATH.\n"
            f"• install: `{spec.install}`\n• sign in: {spec.login}"
        )
    return (
        f"Workspace `{workspace}` isn't a real directory. "
        "Fix it with `/workspace path:<folder>`."
    )


agent_group = app_commands.Group(name="agent", description="Choose who does the work.")


@agent_group.command(name="list", description="Who is on the bench, and who is installed.")
async def agent_list(interaction: discord.Interaction) -> None:
    channel_id, guild_id = context_ids(interaction.channel)
    current = STATE.agent_key(channel_id, guild_id)
    scope = STATE.agent_scope(channel_id, guild_id)
    embed = discord.Embed(
        title="The roster",
        description=fmt.roster_lines(agents.all_specs(), current)[: fmt.EMBED_DESC_LIMIT],
        colour=0x5865F2,
    )
    embed.set_footer(
        text=f"Currently using {current} (set at {scope}). Switch with /agent use."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@agent_group.command(name="use", description="Set the agent for this channel.")
@app_commands.describe(agent="Which agent should handle requests here?")
@app_commands.autocomplete(agent=agent_autocomplete)
async def agent_use(interaction: discord.Interaction, agent: str) -> None:
    spec = agents.get(agent)
    if spec is None:
        await interaction.response.send_message(quips.no_agent(), ephemeral=True)
        return
    channel_id, _ = context_ids(interaction.channel)
    if channel_id is None:
        await interaction.response.send_message(
            "I can't pin a setting to this kind of channel.", ephemeral=True
        )
        return
    STATE.set_channel_agent(channel_id, spec.key)
    note = (
        ""
        if spec.available
        else f"\n⚠️ Heads up: `{spec.binary}` isn't installed here yet. `{spec.install}`"
    )
    await interaction.response.send_message(
        f"{spec.emoji} This channel now belongs to **{spec.label}**. "
        f"_{spec.blurb}_{note}"
    )


@agent_group.command(
    name="server", description="Set the server-wide default agent (needs Manage Server)."
)
@app_commands.describe(agent="The fallback agent for every channel here.")
@app_commands.autocomplete(agent=agent_autocomplete)
async def agent_server(interaction: discord.Interaction, agent: str) -> None:
    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "That one needs Manage Server. Nice try though.", ephemeral=True
        )
        return
    spec = agents.get(agent)
    if spec is None or interaction.guild is None:
        await interaction.response.send_message(quips.no_agent(), ephemeral=True)
        return
    STATE.set_guild_agent(interaction.guild.id, spec.key)
    await interaction.response.send_message(
        f"{spec.emoji} Server default is now **{spec.label}**. "
        "Channels with their own pick keep it."
    )


@agent_group.command(name="reset", description="Forget this channel's agent override.")
async def agent_reset(interaction: discord.Interaction) -> None:
    channel_id, guild_id = context_ids(interaction.channel)
    if channel_id is not None:
        STATE.set_channel_agent(channel_id, None)
    now = STATE.agent_key(channel_id, guild_id)
    await interaction.response.send_message(
        f"Override cleared. Back to `{now}`, as nature intended.", ephemeral=True
    )


tree.add_command(agent_group)


@tree.command(name="model", description="Pin a specific model for this channel.")
@app_commands.describe(name="Model id, or 'clear' to go back to the agent's default.")
async def model_cmd(interaction: discord.Interaction, name: str) -> None:
    channel_id, guild_id = context_ids(interaction.channel)
    if channel_id is None:
        await interaction.response.send_message("Not here, sorry.", ephemeral=True)
        return
    if name.strip().lower() in {"clear", "reset", "default", "none"}:
        STATE.set_channel_model(channel_id, None)
        await interaction.response.send_message(
            "Model override cleared. The agent picks its own favourite again."
        )
        return
    STATE.set_channel_model(channel_id, name.strip())
    spec = agents.get(STATE.agent_key(channel_id, guild_id))
    label = spec.label if spec else "the agent"
    await interaction.response.send_message(
        f"Locked to `{name.strip()}` for this channel. If {label} hates that name, "
        "you'll hear about it in the output."
    )


@tree.command(name="workspace", description="Show or set the folder agents work in.")
@app_commands.describe(
    path="Absolute path to the repo. Leave empty to just see the current one.",
    server_wide="Apply to the whole server instead of this channel.",
)
async def workspace_cmd(
    interaction: discord.Interaction,
    path: str | None = None,
    server_wide: bool = False,
) -> None:
    channel_id, guild_id = context_ids(interaction.channel)
    if path is None:
        current = STATE.workspace(channel_id, guild_id)
        exists = "✔ exists" if current.is_dir() else "✖ missing"
        git = current / ".git"
        kind = "a git repo" if git.exists() else "not a git repo"
        await interaction.response.send_message(
            f"\U0001f4c2 Working in `{current}`\n{exists} · {kind}", ephemeral=True
        )
        return

    if not is_admin(interaction.user):
        await interaction.response.send_message(
            "Moving the workspace needs Manage Server. That's a whole-repo kind of "
            "decision.",
            ephemeral=True,
        )
        return

    target = Path(path.strip().strip('"')).expanduser()
    try:
        target = target.resolve()
    except OSError:
        pass
    if not target.is_dir():
        await interaction.response.send_message(
            f"`{target}` isn't a directory I can see from here.", ephemeral=True
        )
        return

    if server_wide and interaction.guild is not None:
        STATE.set_guild_workspace(interaction.guild.id, str(target))
        where = "this whole server"
    elif channel_id is not None:
        STATE.set_channel_workspace(channel_id, str(target))
        where = "this channel"
    else:
        await interaction.response.send_message("Can't pin that here.", ephemeral=True)
        return

    hint = "" if (target / ".git").exists() else "\n(No `.git` in there. Living dangerously.)"
    await interaction.response.send_message(
        f"\U0001f4c2 Agents in {where} now work in `{target}`.{hint}"
    )


@tree.command(name="status", description="What am I chewing on right now?")
async def status_cmd(interaction: discord.Interaction) -> None:
    active = JOBS.all_active()
    queued = JOBS.all_queued()
    if not active and not queued:
        await interaction.response.send_message(
            "Nothing running. I'm just vibing and watching the file system.",
            ephemeral=True,
        )
        return
    lines = []
    for job in active:
        tail = job.handle.tail(1) if job.handle else ""
        lines.append(
            f"▶ `#{job.job_id}` {job.spec.emoji} {job.spec.label} · "
            f"{fmt.human_duration(job.elapsed)} · for **{job.requester}**\n"
            f" {fmt.tidy_tail(job.task, 90)}"
            + (f"\n > {fmt.tidy_tail(tail, 90)}" if tail else "")
        )
    for job in queued:
        lines.append(
            f"⏸ `#{job.job_id}` {job.spec.label} · waiting "
            f"{fmt.human_duration(job.waited)} · for **{job.requester}**"
        )
    embed = discord.Embed(
        title="Current workload",
        description="\n".join(lines)[: fmt.EMBED_DESC_LIMIT],
        colour=0xFEE75C,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="cancel", description="Stop whatever is running in this channel.")
async def cancel_cmd(interaction: discord.Interaction) -> None:
    if not authorized(interaction.user):
        await interaction.response.send_message("Not on my list.", ephemeral=True)
        return
    channel_id, _ = context_ids(interaction.channel)
    stopped = JOBS.cancel_channel(int(channel_id or 0))
    if not stopped:
        await interaction.response.send_message(
            "Nothing to cancel here. You're cancelling the void.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        f"\U0001f6d1 Pulled the plug on {stopped} job(s). {quips.cancelled()}"
    )


@tree.command(name="reset", description="Forget this conversation's history.")
async def reset_cmd(interaction: discord.Interaction) -> None:
    convo = convo_id_for(interaction.channel)  # type: ignore[arg-type]
    had = STATE.forget(convo)
    await interaction.response.send_message(
        "Memory wiped. We've never met."
        if had
        else "There was nothing to forget. Clean slate already.",
        ephemeral=True,
    )


@tree.command(name="ping", description="Check that I'm alive.")
async def ping_cmd(interaction: discord.Interaction) -> None:
    latency = client.latency * 1000
    await interaction.response.send_message(
        f"\U0001f3d3 `{latency:.0f}ms` — {quips.ping()}", ephemeral=True
    )


@tree.command(name="doctor", description="Check the agent CLIs, logins and workspace.")
async def doctor_cmd(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    channel_id, guild_id = context_ids(interaction.channel)
    workspace = STATE.workspace(channel_id, guild_id)

    async def probe(spec: agents.AgentSpec) -> str:
        if not spec.available:
            return f"✖ `{spec.key}` — not installed. `{spec.install}`"
        try:
            proc = await asyncio.create_subprocess_exec(
                spec.resolved_binary or spec.binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                env=agents.child_env(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            version = agents.clean_output(out.decode("utf-8", "replace")).split("\n")
            version = [v for v in version if v.strip()]
            tag = version[-1][:48] if version else "responded"
        except asyncio.TimeoutError:
            return f"⚠ `{spec.key}` — installed but didn't answer in 20s"
        except Exception as exc:  # noqa: BLE001
            return f"⚠ `{spec.key}` — {type(exc).__name__}"
        return f"✔ `{spec.key}` — `{tag}`"

    rows = await asyncio.gather(*(probe(s) for s in agents.all_specs()))

    git_line = ""
    if (workspace / ".git").exists():
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                str(workspace),
                "status",
                "--short",
                "--branch",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            text = agents.clean_output(out.decode("utf-8", "replace")).split("\n")
            branch = text[0] if text else "?"
            dirty = max(0, len([t for t in text[1:] if t.strip()]))
            git_line = f"\n**Git:** {branch} · {dirty} changed file(s)"
        except Exception:  # noqa: BLE001
            git_line = "\n**Git:** couldn't read status"

    auto = "yolo (no sandbox)" if SETTINGS.full_auto else "auto-edit (sandboxed where supported)"
    if not SETTINGS.access_locked:
        gate = "⚠ wide open — anyone who can see me"
    elif SETTINGS.allowed_usernames or SETTINGS.allowed_user_ids or SETTINGS.allowed_role_ids:
        bits = []
        if SETTINGS.allowed_usernames:
            bits.append(
                "@" + ", @".join(sorted(SETTINGS.allowed_usernames)[:6])
            )
        if SETTINGS.allowed_user_ids:
            bits.append(f"{len(SETTINGS.allowed_user_ids)} user id(s)")
        if SETTINGS.allowed_role_ids:
            bits.append(f"{len(SETTINGS.allowed_role_ids)} role(s)")
        gate = "allowlist on — " + " · ".join(bits)
    else:
        gate = "\U0001f512 locked — an allowlist is set but holds nothing valid"
    body = (
        "\n".join(rows)
        + f"\n\n**Workspace:** `{workspace}` "
        + ("✔" if workspace.is_dir() else "✖ missing")
        + git_line
        + f"\n**Timeout:** {SETTINGS.timeout}s · **Parallel:** {SETTINGS.max_parallel}"
        + f"\n**Approvals:** {auto}\n**Access:** {gate}"
    )
    embed = discord.Embed(
        title="Jerry's annual physical",
        description=body[: fmt.EMBED_DESC_LIMIT],
        colour=0x57F287,
    )
    embed.set_footer(text="Logins live in each CLI, not here. No API keys were harmed.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="help", description="How to boss me around.")
async def help_cmd(interaction: discord.Interaction) -> None:
    channel_id, guild_id = context_ids(interaction.channel)
    current = STATE.agent_key(channel_id, guild_id)
    me = client.user.name if client.user else "jerry"
    embed = discord.Embed(
        title="Jerry, at your service (reluctantly)",
        description=(
            f"**@ me** and say what you want. That's the whole interface.\n"
            f"`@{me} the auth module has no tests, fix that`\n\n"
            "I ignore every message that doesn't mention me, so feel free to "
            "gossip about me in the same channel."
        ),
        colour=0xEB459E,
    )
    embed.add_field(
        name="Doing work",
        value=(
            "`/ask` — same thing, but as a slash command\n"
            "`/cancel` — stop the run in this channel\n"
            "`/status` — what's running right now\n"
            "`/reset` — forget this conversation's history"
        ),
        inline=False,
    )
    embed.add_field(
        name="Choosing a brain",
        value=(
            f"`/agent list` — the roster (currently **{current}**)\n"
            "`/agent use` — pick one for this channel\n"
            "`/agent server` — set the server default\n"
            "`/model` — pin a specific model"
        ),
        inline=False,
    )
    embed.add_field(
        name="Housekeeping",
        value=(
            "`/workspace` — which repo I'm let loose in\n"
            "`/doctor` — what's installed, signed in and broken\n"
            "`/ping` — proof of life"
        ),
        inline=False,
    )
    embed.set_footer(
        text="I edit real files in a real folder. Keep a branch handy and review my work."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    log.exception("slash command blew up", exc_info=error)
    message = f"\U0001f480 That command fell over: `{type(error).__name__}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    client.run(SETTINGS.token, log_handler=None)
