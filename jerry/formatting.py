"""Turning agent output into something Discord will actually accept."""

from __future__ import annotations

import io
import re
from typing import Iterable

import discord

MESSAGE_LIMIT = 2000
SAFE_LIMIT = 1900  # leaves room for a prefix line and a code fence
EMBED_DESC_LIMIT = 4096

NOISE_PREFIXES = (
    "loaded cached credentials",
    "data collection is disabled",
    "warning: ",
    "deprecationwarning",
    "(node:",
    "debugger attached",
)


def strip_noise(text: str) -> str:
    """Drop the usual CLI boilerplate so the useful part leads."""
    keep: list[str] = []
    for line in text.split("\n"):
        low = line.strip().lower()
        if any(low.startswith(p) for p in NOISE_PREFIXES):
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def chunk(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split text for Discord without slicing words (or fences) in half."""
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 3:
            cut = window.rfind("\n")
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return [balance_fences(part) for part in out]


def balance_fences(part: str) -> str:
    """Close a code fence that a chunk boundary left hanging open."""
    if part.count("```") % 2 == 1:
        return part + "\n```"
    return part


def as_file(text: str, name: str = "jerry-output.md") -> discord.File:
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=name)


def tidy_tail(text: str, limit: int = 160) -> str:
    """One short line of progress chatter, safe to drop into a message."""
    line = " ".join((text or "").split())
    line = re.sub(r"[`*_~|]", "", line)
    if len(line) > limit:
        line = line[: limit - 1] + "…"
    return line


def roster_lines(specs: Iterable, current: str | None = None) -> str:
    rows: list[str] = []
    for spec in specs:
        mark = "✔" if spec.available else "✖"
        here = " ← **in use here**" if spec.key == current else ""
        rows.append(f"{mark} {spec.emoji} `{spec.key}` — {spec.label}{here}")
        rows.append(f"  _{spec.blurb or 'No notes.'}_")
        if not spec.available:
            rows.append(f"  install: `{spec.install}`")
    return "\n".join(rows)
