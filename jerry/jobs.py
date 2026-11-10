"""Job queue.

One agent at a time per channel (so two people in the same channel cannot
fight over the same working tree), with a global cap across the whole bot.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .agents import AgentSpec, RunHandle

_ids = itertools.count(1)


@dataclass
class Job:
    channel_id: int
    requester: str
    requester_id: int
    task: str
    spec: AgentSpec
    workspace: Path
    model: str | None = None
    job_id: int = field(default_factory=lambda: next(_ids))
    queued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    handle: RunHandle | None = None
    status: str = "queued"  # queued | running | done | failed | cancelled
    # Set when the work is routed to somebody's own machine via a sub-Jerry.
    sub_id: int | None = None
    requester_handle: str = ""
    location: str = ""
    progress: str = ""
    _task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def waited(self) -> float:
        return (self.started_at or time.monotonic()) - self.queued_at

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    def latest(self) -> str:
        """Best progress line available, whether run locally or on a sub."""
        if self.progress:
            return self.progress
        return self.handle.tail(1) if self.handle else ""

    def cancel(self) -> None:
        self.status = "cancelled"
        if self.handle is not None:
            self.handle.kill()
        if self._task is not None:
            self._task.cancel()


class JobManager:
    def __init__(self, max_parallel: int = 2) -> None:
        self._gate = asyncio.Semaphore(max_parallel)
        self._queues: dict[int, deque[tuple[Job, Callable[[Job], Awaitable[None]]]]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._active: dict[int, Job] = {}

    # -------------------------------------------------------------- views --
    def active(self, channel_id: int) -> Job | None:
        return self._active.get(channel_id)

    def queue_depth(self, channel_id: int) -> int:
        return len(self._queues.get(channel_id, ()))

    def all_active(self) -> list[Job]:
        return list(self._active.values())

    def all_queued(self) -> list[Job]:
        return [job for q in self._queues.values() for job, _ in q]

    def busy(self, channel_id: int) -> bool:
        return channel_id in self._active or self.queue_depth(channel_id) > 0

    # ------------------------------------------------------------- submit --
    def submit(self, job: Job, runner: Callable[[Job], Awaitable[None]]) -> int:
        """Enqueue a job. Returns how many jobs are ahead of it in this channel."""
        queue = self._queues.setdefault(job.channel_id, deque())
        ahead = len(queue) + (1 if job.channel_id in self._active else 0)
        queue.append((job, runner))
        worker = self._workers.get(job.channel_id)
        if worker is None or worker.done():
            self._workers[job.channel_id] = asyncio.create_task(
                self._drain(job.channel_id)
            )
        return ahead

    def cancel_channel(self, channel_id: int) -> int:
        """Cancel the running job plus anything queued behind it."""
        cancelled = 0
        queue = self._queues.get(channel_id)
        if queue:
            for job, _ in queue:
                job.status = "cancelled"
            cancelled += len(queue)
            queue.clear()
        running = self._active.get(channel_id)
        if running is not None:
            running.cancel()
            cancelled += 1
        return cancelled

    # ------------------------------------------------------------- worker --
    async def _drain(self, channel_id: int) -> None:
        queue = self._queues.setdefault(channel_id, deque())
        try:
            while queue:
                job, runner = queue.popleft()
                if job.status == "cancelled":
                    continue
                async with self._gate:
                    if job.status == "cancelled":
                        continue
                    job.started_at = time.monotonic()
                    job.status = "running"
                    self._active[channel_id] = job
                    inner = asyncio.create_task(runner(job))
                    job._task = inner
                    try:
                        await inner
                    except asyncio.CancelledError:
                        job.status = "cancelled"
                    except Exception:  # noqa: BLE001 - one bad job must not
                        job.status = "failed"  # take the channel worker down
                    finally:
                        self._active.pop(channel_id, None)
        finally:
            if not queue:
                self._queues.pop(channel_id, None)
            self._workers.pop(channel_id, None)
