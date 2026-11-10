"""Sub-Jerry: runs agents on *your* machine, with *your* CLI login.

You run this; your teammates talk to the one master Jerry in Discord. When a
request is yours, master hands it to your sub-Jerry, the agent runs here
against your own checkout and your own subscription, and the answer goes back
through master to the channel.

    python subjerry.py

Configured from .env (see .env.sub.example):

    DISCORD_TOKEN              this sub-bot's own token, not master's
    JERRY_MASTER_ID            master bot's user id -- the ONLY id it obeys
    JERRY_DISPATCH_CHANNEL_ID  the private channel master dispatches in
    JERRY_SUB_OWNER            your Discord handle, for the logs
    JERRY_WORKSPACE            the repo agents may edit on this machine
    JERRY_FULL_AUTO            1 to let agents run commands unattended

Safety: this process runs whatever the agent decides to do, on this computer.
It accepts work only from JERRY_MASTER_ID, only in the dispatch channel, and
only when mentioned. If any of those three is wrong it does nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import socket
import sys
import time
from pathlib import Path

import discord
from dotenv import load_dotenv

from jerry import agents, bus
from jerry.config import OVERRIDES_PATH

load_dotenv()

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
log = logging.getLogger("subjerry")

PROGRESS_EVERY = 10.0  # seconds between edits of the ack message


def _env_int(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
MASTER_ID = _env_int("JERRY_MASTER_ID")
DISPATCH_ID = _env_int("JERRY_DISPATCH_CHANNEL_ID")
OWNER = os.environ.get("JERRY_SUB_OWNER", "").strip().strip("@.").lower()
FULL_AUTO = _env_bool("JERRY_FULL_AUTO", False)

_raw_ws = os.environ.get("JERRY_WORKSPACE", "").strip().strip('"')
WORKSPACE = Path(_raw_ws).expanduser() if _raw_ws else Path.cwd()
try:
    WORKSPACE = WORKSPACE.resolve()
except OSError:
    pass

_missing = [
    name
    for name, value in (
        ("DISCORD_TOKEN", TOKEN),
        ("JERRY_MASTER_ID", MASTER_ID),
        ("JERRY_DISPATCH_CHANNEL_ID", DISPATCH_ID),
    )
    if not value
]
if _missing:
    raise SystemExit(
        "Missing required settings: "
        + ", ".join(_missing)
        + ".\nCopy .env.sub.example to .env and fill it in, or run setup_sub.py."
    )

intents = discord.Intents.default()
NO_PINGS = discord.AllowedMentions.none()


class SubJerry(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.running: dict[str, agents.RunHandle] = {}
        self.cancelled: set[str] = set()

    async def setup_hook(self) -> None:
        for note in agents.load_overrides(OVERRIDES_PATH):
            log.info("agent override: %s", note)


client = SubJerry()


def installed_agents() -> list[str]:
    return [spec.key for spec in agents.available_specs()]


@client.event
async def on_ready() -> None:
    log.info("sub-Jerry up as %s (%s)", client.user, getattr(client.user, "id", "?"))
    log.info("owner: @%s | workspace: %s", OWNER or "(unset)", WORKSPACE)
    log.info("obeying master id %s in channel %s", MASTER_ID, DISPATCH_ID)
    found = installed_agents()
    log.info("agents available here: %s", ", ".join(found) if found else "NONE")
    if not found:
        log.warning("No agent CLI is installed here, so every job will fail.")
    if not WORKSPACE.is_dir():
        log.warning("Workspace %s does not exist; jobs will be refused.", WORKSPACE)
    channel = client.get_channel(DISPATCH_ID)
    if channel is None:
        log.warning(
            "Can't see channel %s. Is this bot invited to the server and allowed "
            "to read that channel?",
            DISPATCH_ID,
        )
    try:
        await client.change_presence(
            activity=discord.CustomActivity(name=f"sub-Jerry for @{OWNER or '?'}")
        )
    except discord.HTTPException:
        pass


@client.event
async def on_message(message: discord.Message) -> None:
    # The only gate that matters. Everything past it is treated as an order.
    if not bus.is_trusted(
        message,
        me=client.user,
        expected_author_id=MASTER_ID,
        dispatch_channel_id=DISPATCH_ID,
    ):
        return

    env = await bus.read_envelope(message)
    if env is None:
        log.warning("message from master had no readable envelope")
        return

    if env.kind == bus.KIND_PING:
        await _pong(message, env)
    elif env.kind == bus.KIND_JOB:
        asyncio.create_task(_handle_job(message, env))
    elif env.kind == bus.KIND_CANCEL:
        _cancel(env)
    else:
        log.info("ignoring envelope kind %r", env.kind)


async def _pong(message: discord.Message, env: bus.Envelope) -> None:
    text, file, _ = bus.simple(
        bus.KIND_PONG,
        MASTER_ID,
        id=env.id,
        owner=OWNER or None,
        agents=installed_agents(),
        workspace=str(WORKSPACE),
        host=f"{socket.gethostname()} ({platform.system()})",
        full_auto=FULL_AUTO,
    )
    await message.channel.send(text, file=file, allowed_mentions=NO_PINGS)
    log.info("answered ping %s", env.id)


def _cancel(env: bus.Envelope) -> None:
    job_id = env.job_id or ""
    self_handle = client.running.get(job_id)
    client.cancelled.add(job_id)
    if self_handle is not None:
        self_handle.kill()
        log.info("cancelled job %s", job_id)


async def _handle_job(message: discord.Message, env: bus.Envelope) -> None:
    job_id = env.job_id or env.id
    spec = agents.get(env.agent)
    started = time.monotonic()

    async def fail(reason: str) -> None:
        text, file, _ = bus.simple(
            bus.KIND_RESULT,
            MASTER_ID,
            job_id=job_id,
            ok=False,
            error=reason,
            duration=time.monotonic() - started,
        )
        await message.channel.send(text, file=file, allowed_mentions=NO_PINGS)
        log.warning("job %s refused: %s", job_id, reason)

    if spec is None:
        await fail(f"this machine doesn't know an agent called `{env.agent}`")
        return
    if not spec.available:
        await fail(
            f"`{spec.binary}` isn't installed on this machine "
            f"(@{OWNER or 'sub'}). Install with `{spec.install}`, then {spec.login}."
        )
        return
    if not WORKSPACE.is_dir():
        await fail(f"workspace `{WORKSPACE}` doesn't exist on this machine")
        return
    if not env.prompt:
        await fail("the job envelope carried no prompt")
        return

    ack = await message.channel.send(
        f"<@{MASTER_ID}> `{bus.PROTOCOL}` claimed `{job_id}` · "
        f"{spec.emoji} {spec.label} starting on @{OWNER or 'this machine'}",
        allowed_mentions=NO_PINGS,
    )

    handle = agents.RunHandle(spec=spec)
    client.running[job_id] = handle
    log.info(
        "job %s: %s for @%s in %s", job_id, spec.key, env.requester or "?", WORKSPACE
    )

    async def ticker() -> None:
        while True:
            await asyncio.sleep(PROGRESS_EVERY)
            tail = handle.tail(1)
            line = (
                f"<@{MASTER_ID}> `{bus.PROTOCOL}` running `{job_id}` · "
                f"{int(handle.elapsed)}s"
            )
            if tail:
                line += f"\n> {tail[:300]}"
            try:
                await ack.edit(content=line[:1900], allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                return

    tick = asyncio.create_task(ticker())
    try:
        result = await agents.run_agent(
            spec,
            env.prompt,
            workspace=WORKSPACE,
            model=env.model,
            # Master asks, but this machine decides: a sub never grants more
            # than its own owner configured.
            full_auto=FULL_AUTO and env.full_auto,
            timeout=float(env.timeout or 900),
            handle=handle,
        )
    except asyncio.CancelledError:
        result = None
    finally:
        tick.cancel()
        client.running.pop(job_id, None)

    was_cancelled = job_id in client.cancelled or handle.killed
    client.cancelled.discard(job_id)

    try:
        await ack.delete()
    except discord.HTTPException:
        pass

    if result is None:
        text, file, _ = bus.simple(
            bus.KIND_RESULT,
            MASTER_ID,
            job_id=job_id,
            ok=False,
            cancelled=True,
            duration=time.monotonic() - started,
        )
    else:
        text, file, _ = bus.simple(
            bus.KIND_RESULT,
            MASTER_ID,
            job_id=job_id,
            ok=result.ok,
            exit_code=result.exit_code,
            output=result.output or None,
            error=result.error,
            duration=result.duration,
            cancelled=was_cancelled,
            timed_out=result.timed_out,
        )

    await message.channel.send(text, file=file, allowed_mentions=NO_PINGS)
    log.info(
        "job %s finished: ok=%s in %.1fs",
        job_id,
        getattr(result, "ok", False),
        time.monotonic() - started,
    )


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
