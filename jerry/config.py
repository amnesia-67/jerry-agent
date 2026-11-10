"""Settings: environment at boot, per-guild/per-channel overrides on disk."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import agents

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "jerry.state.json"
OVERRIDES_PATH = ROOT / "agents.local.json"

HISTORY_TURNS = 6  # how many prior exchanges travel with a follow-up


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except ValueError:
        return default


def _env_ids(name: str) -> set[int]:
    out: set[int] = set()
    for chunk in os.environ.get(name, "").replace(",", " ").split():
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def _env_has_content(*names: str) -> bool:
    """True if any of these vars was set to something other than whitespace.

    Access control reads this rather than the parsed id set, so a typo in an
    allowlist locks everyone out instead of silently opening the bot to the
    whole server.
    """
    return any(os.environ.get(n, "").strip() for n in names)


def normalize_username(raw: str) -> str:
    """Fold a Discord handle to a comparable form.

    Handles are lowercase, and people copy them with a leading `@` or a
    stray `.` in front. Discord does not allow a handle to start or end with
    a period, so trimming those is safe and saves a confusing lockout.
    """
    return raw.strip().strip("@.").strip().lower()


def _env_names(name: str) -> set[str]:
    out: set[str] = set()
    for chunk in os.environ.get(name, "").replace(",", " ").split():
        folded = normalize_username(chunk)
        if folded:
            out.add(folded)
    return out


@dataclass
class Settings:
    """Boot-time configuration, all from the environment."""

    token: str
    workspace: Path
    default_agent: str
    timeout: int
    max_parallel: int
    full_auto: bool
    guild_ids: set[int]
    allowed_user_ids: set[int]
    allowed_role_ids: set[int]
    allowed_usernames: set[str]
    access_locked: bool
    use_threads: bool

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "DISCORD_TOKEN is missing. Put it in .env next to bot.py "
                "(see .env.example)."
            )

        raw_ws = os.environ.get("JERRY_WORKSPACE", "").strip().strip('"')
        workspace = Path(raw_ws).expanduser() if raw_ws else ROOT
        try:
            workspace = workspace.resolve()
        except OSError:
            pass

        default_agent = os.environ.get("JERRY_DEFAULT_AGENT", "").strip().lower()
        if not agents.get(default_agent):
            default_agent = agents.DEFAULT_AGENT

        return cls(
            token=token,
            workspace=workspace,
            default_agent=default_agent,
            timeout=max(30, _env_int("JERRY_TIMEOUT", 900)),
            max_parallel=max(1, _env_int("JERRY_MAX_PARALLEL", 2)),
            full_auto=_env_bool("JERRY_FULL_AUTO", False),
            guild_ids=_env_ids("JERRY_GUILD_IDS"),
            allowed_user_ids=_env_ids("JERRY_ALLOWED_USER_IDS"),
            allowed_role_ids=_env_ids("JERRY_ALLOWED_ROLE_IDS"),
            allowed_usernames=_env_names("JERRY_ALLOWED_USERNAMES"),
            access_locked=_env_has_content(
                "JERRY_ALLOWED_USER_IDS",
                "JERRY_ALLOWED_ROLE_IDS",
                "JERRY_ALLOWED_USERNAMES",
            ),
            use_threads=_env_bool("JERRY_USE_THREADS", True),
        )


class State:
    """Tiny JSON-backed store for runtime overrides and thread memory.

    Scopes, most specific first: channel -> guild -> boot settings.
    """

    def __init__(self, settings: Settings, path: Path = STATE_PATH) -> None:
        self.settings = settings
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"guilds": {}, "channels": {}, "memory": {}}
        self.load()

    # ---------------------------------------------------------------- io ---
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt state file is not fatal
            return
        if isinstance(loaded, dict):
            for key in ("guilds", "channels", "memory"):
                if isinstance(loaded.get(key), dict):
                    self._data[key] = loaded[key]

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
            tmp.replace(self.path)

    # ------------------------------------------------------------ scopes ---
    def _bucket(self, kind: str, ident: int | None) -> dict[str, Any]:
        if ident is None:
            return {}
        return self._data[kind].setdefault(str(ident), {})

    def _set(self, kind: str, ident: int, key: str, value: Any) -> None:
        bucket = self._bucket(kind, ident)
        if value is None:
            bucket.pop(key, None)
        else:
            bucket[key] = value
        self.save()

    def _lookup(self, key: str, channel_id: int | None, guild_id: int | None) -> Any:
        chan = self._bucket("channels", channel_id).get(key)
        if chan is not None:
            return chan
        return self._bucket("guilds", guild_id).get(key)

    # ------------------------------------------------------------ agents ---
    def agent_key(self, channel_id: int | None, guild_id: int | None) -> str:
        found = self._lookup("agent", channel_id, guild_id)
        if isinstance(found, str) and agents.get(found):
            return found
        return self.settings.default_agent

    def agent_scope(self, channel_id: int | None, guild_id: int | None) -> str:
        if self._bucket("channels", channel_id).get("agent"):
            return "this channel"
        if self._bucket("guilds", guild_id).get("agent"):
            return "this server"
        return "the default"

    def set_channel_agent(self, channel_id: int, key: str | None) -> None:
        self._set("channels", channel_id, "agent", key)

    def set_guild_agent(self, guild_id: int, key: str | None) -> None:
        self._set("guilds", guild_id, "agent", key)

    # ------------------------------------------------------------- model ---
    def model(self, channel_id: int | None, guild_id: int | None) -> str | None:
        found = self._lookup("model", channel_id, guild_id)
        return found if isinstance(found, str) and found else None

    def set_channel_model(self, channel_id: int, model: str | None) -> None:
        self._set("channels", channel_id, "model", model)

    # --------------------------------------------------------- workspace ---
    def workspace(self, channel_id: int | None, guild_id: int | None) -> Path:
        found = self._lookup("workspace", channel_id, guild_id)
        if isinstance(found, str) and found:
            return Path(found)
        return self.settings.workspace

    def set_guild_workspace(self, guild_id: int, path: str | None) -> None:
        self._set("guilds", guild_id, "workspace", path)

    def set_channel_workspace(self, channel_id: int, path: str | None) -> None:
        self._set("channels", channel_id, "workspace", path)

    # ----------------------------------------------------------- memory ---
    def history(self, convo_id: int) -> list[str]:
        entries = self._data["memory"].get(str(convo_id), [])
        return [e for e in entries if isinstance(e, str)]

    def remember(self, convo_id: int, who: str, task: str, answer: str) -> None:
        entries = self._data["memory"].setdefault(str(convo_id), [])
        entries.append(
            f"- {who} asked: {_squash(task, 400)}\n  you replied: {_squash(answer, 700)}"
        )
        del entries[:-HISTORY_TURNS]
        self.save()

    def forget(self, convo_id: int) -> bool:
        existed = str(convo_id) in self._data["memory"]
        self._data["memory"].pop(str(convo_id), None)
        self.save()
        return existed


def _squash(text: str, limit: int) -> str:
    one_line = " ".join((text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"
