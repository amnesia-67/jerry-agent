"""Master-side routing: hand a job to somebody's sub-Jerry and wait for it.

Keeps a future per outstanding job, keyed by job id, and resolves it when the
matching result envelope arrives in the dispatch channel. Progress arrives as
edits to the sub's own ack message, which costs nothing extra to watch.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

import discord

from . import bus

log = logging.getLogger("jerry.dispatch")

# How long after a job's own timeout we keep waiting before giving up on the
# sub entirely. Covers process start-up, the upload and Discord's latency.
GRACE_SECONDS = 90.0

PROGRESS_RE = re.compile(r"running `([0-9a-f]{6,32})`(?:\s*·\s*(\d+)s)?")


@dataclass
class Pending:
    job_id: str
    sub_id: int
    owner: str
    future: "asyncio.Future[bus.Envelope]"
    started: float = field(default_factory=time.monotonic)
    progress: str = ""
    claimed: bool = False
    # Called with each progress line, so the caller's status message can show
    # it without having to guess which pending job a line belongs to.
    sink: "Callable[[str], None] | None" = None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class Router:
    def __init__(
        self,
        client: discord.Client,
        *,
        dispatch_channel_id: int,
        subs: dict[str, int],
    ) -> None:
        self.client = client
        self.dispatch_channel_id = dispatch_channel_id
        self.subs = subs
        self.pending: dict[str, Pending] = {}
        self._pongs: dict[str, list[bus.Envelope]] = {}

    # ------------------------------------------------------------- config --
    @property
    def enabled(self) -> bool:
        return bool(self.dispatch_channel_id and self.subs)

    def sub_for(self, handle: str) -> int | None:
        return self.subs.get((handle or "").strip().strip("@.").lower())

    def owner_of(self, sub_id: int) -> str:
        for handle, ident in self.subs.items():
            if ident == sub_id:
                return handle
        return str(sub_id)

    def channel(self) -> discord.abc.Messageable | None:
        if not self.dispatch_channel_id:
            return None
        found = self.client.get_channel(self.dispatch_channel_id)
        return found if isinstance(found, discord.abc.Messageable) else None

    # ------------------------------------------------------------ inbound --
    def trusted_sub(self, message: discord.Message) -> int | None:
        """The sub id this message is really from, or None."""
        if message.channel.id != self.dispatch_channel_id:
            return None
        if self.client.user is not None and message.author.id == self.client.user.id:
            return None
        if message.author.id not in self.subs.values():
            return None
        return message.author.id

    async def handle_message(self, message: discord.Message) -> bool:
        """Consume a dispatch-channel message. True if it was ours."""
        sub_id = self.trusted_sub(message)
        if sub_id is None:
            return False

        env = await bus.read_envelope(message)
        if env is None:
            # An ack or progress edit carries no envelope; still ours.
            self._note_progress(message)
            return True

        if env.kind == bus.KIND_PONG:
            self._pongs.setdefault(env.id, []).append(env)
            return True

        if env.kind == bus.KIND_RESULT:
            job_id = env.job_id or ""
            waiting = self.pending.get(job_id)
            if waiting is None:
                log.info("result for unknown job %s (late or already timed out)", job_id)
                return True
            if not waiting.future.done():
                waiting.future.set_result(env)
            return True

        return True

    def _note_progress(self, message: discord.Message) -> None:
        match = PROGRESS_RE.search(message.content or "")
        if match is None:
            return
        waiting = self.pending.get(match.group(1))
        if waiting is None:
            return
        waiting.claimed = True
        lines = (message.content or "").split("\n")
        tail = next((l[1:].strip() for l in lines if l.startswith(">")), "")
        if tail:
            waiting.progress = tail

    async def handle_edit(self, message: discord.Message) -> bool:
        if self.trusted_sub(message) is None:
            return False
        self._note_progress(message)
        return True

    # ----------------------------------------------------------- outbound --
    async def ping(self, window: float = 6.0) -> list[bus.Envelope]:
        """Ask every configured sub to identify itself. Collects for `window`."""
        channel = self.channel()
        if channel is None or not self.subs:
            return []
        env = bus.Envelope(kind=bus.KIND_PING)
        mentions = " ".join(f"<@{i}>" for i in sorted(set(self.subs.values())))
        self._pongs[env.id] = []
        await channel.send(
            f"{mentions} `{bus.PROTOCOL}` ping `{env.id}`",
            file=env.as_file(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.sleep(window)
        return self._pongs.pop(env.id, [])

    async def run(
        self,
        *,
        sub_id: int,
        requester: str,
        agent: str,
        model: str | None,
        timeout: int,
        task: str,
        prompt: str,
        full_auto: bool,
        progress_sink: "Callable[[str], None] | None" = None,
    ) -> tuple[bus.Envelope | None, Pending]:
        """Dispatch and wait. Envelope is None if the sub never answered."""
        channel = self.channel()
        job_id = bus.new_id()
        loop = asyncio.get_running_loop()
        entry = Pending(
            job_id=job_id,
            sub_id=sub_id,
            owner=self.owner_of(sub_id),
            future=loop.create_future(),
            sink=progress_sink,
        )

        if channel is None:
            return (
                bus.Envelope(
                    kind=bus.KIND_RESULT,
                    job_id=job_id,
                    ok=False,
                    error=(
                        "I can't see the dispatch channel. Check "
                        "JERRY_DISPATCH_CHANNEL_ID and that I can read it."
                    ),
                ),
                entry,
            )

        self.pending[job_id] = entry
        text, file, _ = bus.job_request(
            to_id=sub_id,
            job_id=job_id,
            requester=requester,
            agent=agent,
            model=model,
            timeout=timeout,
            task=task,
            prompt=prompt,
            full_auto=full_auto,
        )
        try:
            await channel.send(
                text, file=file, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            self.pending.pop(job_id, None)
            return (
                bus.Envelope(
                    kind=bus.KIND_RESULT,
                    job_id=job_id,
                    ok=False,
                    error=f"couldn't post to the dispatch channel: {exc}",
                ),
                entry,
            )

        try:
            env = await asyncio.wait_for(entry.future, timeout=timeout + GRACE_SECONDS)
            return env, entry
        except asyncio.TimeoutError:
            return None, entry
        except asyncio.CancelledError:
            await self.cancel(job_id)
            raise
        finally:
            self.pending.pop(job_id, None)

    async def cancel(self, job_id: str) -> None:
        channel = self.channel()
        waiting = self.pending.get(job_id)
        if channel is None or waiting is None:
            return
        text, file, _ = bus.simple(
            bus.KIND_CANCEL, waiting.sub_id, job_id=job_id
        )
        try:
            await channel.send(
                text, file=file, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            pass
