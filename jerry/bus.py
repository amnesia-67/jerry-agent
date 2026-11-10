"""The dispatch protocol: master Jerry talking to sub-Jerries over Discord.

Discord is already a message broker that every machine involved is connected
to, which sidesteps the real problem with a master on a laptop: nothing can
open a connection *to* it. Nobody needs a public address, a tunnel or a
database -- both sides are already clients of the same server.

How a job travels:

    master  -- posts in the dispatch channel, @-mentioning one sub
    sub     -- recognises the mention, downloads the envelope, runs the agent
            -- edits its own ack message with progress as it goes
            -- posts a result envelope, @-mentioning master
    master  -- matches it to the waiting job and relays it to the user

**The whole envelope travels as a JSON attachment, not in message text.** A
bot only receives message *content* when the message mentions it (without the
privileged message-content intent), and relying on that for the payload is
fragile. Attachments are always readable, so the text is purely for humans
reading the channel over your shoulder.

Trust, which matters more than anything else here: a sub runs commands on its
owner's machine. It therefore accepts work **only** from the configured master
user id, **only** in the configured dispatch channel, and **only** when
addressed by mention. Anyone else posting a perfect envelope is ignored. That
check lives in ``is_trusted`` and is not optional.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import discord

PROTOCOL = "jerry/1"

KIND_JOB = "job"
KIND_RESULT = "result"
KIND_PING = "ping"
KIND_PONG = "pong"
KIND_CANCEL = "cancel"

ENVELOPE_NAME = "jerry-envelope.json"
# Discord accepts far more, but an agent transcript past this is a sign
# something has gone wrong rather than something worth shipping.
MAX_PAYLOAD_BYTES = 6_000_000


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Envelope:
    kind: str
    id: str = field(default_factory=new_id)
    # job
    job_id: str | None = None
    agent: str | None = None
    model: str | None = None
    timeout: int | None = None
    task: str | None = None
    prompt: str | None = None
    requester: str | None = None
    full_auto: bool = False
    # result
    ok: bool | None = None
    exit_code: int | None = None
    duration: float | None = None
    output: str | None = None
    error: str | None = None
    cancelled: bool = False
    timed_out: bool = False
    # pong
    owner: str | None = None
    agents: list[str] = field(default_factory=list)
    workspace: str | None = None
    host: str | None = None
    version: str = PROTOCOL

    def to_bytes(self) -> bytes:
        body = {k: v for k, v in self.__dict__.items() if v not in (None, [], False)}
        body["kind"] = self.kind
        body["id"] = self.id
        body["version"] = PROTOCOL
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    def as_file(self) -> discord.File:
        return discord.File(io.BytesIO(self.to_bytes()), filename=ENVELOPE_NAME)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Envelope | None":
        try:
            data: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("kind"), str):
            return None
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        clean.setdefault("id", new_id())
        if not isinstance(clean.get("agents"), list):
            clean["agents"] = []
        try:
            return cls(**clean)
        except TypeError:
            return None


def is_trusted(
    message: discord.Message,
    *,
    me: discord.abc.User | None,
    expected_author_id: int,
    dispatch_channel_id: int,
) -> bool:
    """Is this message really from our counterpart, in the right channel?

    A sub-Jerry executes whatever it is told to, on somebody's real computer.
    Everything downstream of this function assumes the sender is who it claims
    to be, so this is the only thing standing between a dispatch channel and
    remote code execution by anyone who can post in it.
    """
    if me is None:
        return False
    if message.author.id != expected_author_id:
        return False
    if message.channel.id != dispatch_channel_id:
        return False
    if message.author.id == me.id:
        return False  # our own messages are not instructions
    return any(u.id == me.id for u in message.mentions)


async def read_envelope(message: discord.Message) -> Envelope | None:
    """Pull the envelope out of the message's attachment."""
    for attachment in message.attachments:
        if attachment.filename != ENVELOPE_NAME:
            continue
        if attachment.size > MAX_PAYLOAD_BYTES:
            return None
        try:
            raw = await attachment.read()
        except (discord.HTTPException, discord.NotFound):
            return None
        return Envelope.from_bytes(raw)
    return None


def job_request(
    *,
    to_id: int,
    job_id: str,
    requester: str,
    agent: str,
    model: str | None,
    timeout: int,
    task: str,
    prompt: str,
    full_auto: bool,
) -> tuple[str, discord.File, Envelope]:
    env = Envelope(
        kind=KIND_JOB,
        job_id=job_id,
        requester=requester,
        agent=agent,
        model=model,
        timeout=timeout,
        task=task,
        prompt=prompt,
        full_auto=full_auto,
    )
    # The mention is load-bearing: it is how the sub knows it is being
    # addressed, and how Discord decides to deliver the message at all.
    text = (
        f"<@{to_id}> `{PROTOCOL}` job `{job_id}` · **{agent}** "
        f"· for `@{requester}`"
    )
    return text, env.as_file(), env


def simple(kind: str, to_id: int, **fields: Any) -> tuple[str, discord.File, Envelope]:
    env = Envelope(kind=kind, **fields)
    return f"<@{to_id}> `{PROTOCOL}` {kind} `{env.id}`", env.as_file(), env
