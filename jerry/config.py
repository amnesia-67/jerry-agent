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


def _env_subs(name: str) -> dict[str, int]:
    """Parse ``handle:sub_bot_user_id`` pairs.

    Whose jobs run on whose machine. A person with a sub-Jerry here has their
    work routed to it; anybody else is run by master locally.

        JERRY_SUBS=a.o.luzhnov:1570000000000000001, papai0.3:1570000000000000002
    """
    out: dict[str, int] = {}
    for entry in os.environ.get(name, "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        handle_part, id_part = entry.rsplit(":", 1)
        handle = normalize_username(handle_part)
        try:
            sub_id = int(id_part.strip())
        except ValueError:
            continue
        if handle and sub_id:
            out[handle] = sub_id
    return out


ALL_AGENTS = "*"


def _env_user_agents(name: str) -> dict[str, set[str]]:
    """Parse per-person model access.

    ``handle:agent`` entries, comma separated, several agents joined by ``|``
    or ``/``. ``*`` means every backend. Names are resolved through the agent
    aliases, so ``papai0.3:chatgpt`` records ``codex``.

        JERRY_USER_AGENTS=a.o.luzhnov:grok, papai0.3:chatgpt, sam:claude|grok
    """
    out: dict[str, set[str]] = {}
    raw = os.environ.get(name, "")
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        handle_part, agent_part = entry.split(":", 1)
        handle = normalize_username(handle_part)
        if not handle:
            continue
        keys: set[str] = set()
        for token in agent_part.replace("/", "|").replace(" ", "|").split("|"):
            token = token.strip().lower()
            if not token:
                continue
            if token == ALL_AGENTS:
                keys.add(ALL_AGENTS)
                continue
            resolved = agents.canonical(token)
            # Keep an unknown name rather than dropping it: silently widening
            # someone's access because of a typo would be the wrong failure.
            keys.add(resolved or token)
        if keys:
            out.setdefault(handle, set()).update(keys)
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
    user_agents: dict[str, set[str]]
    access_locked: bool
    use_threads: bool
    dispatch_channel_id: int
    subs: dict[str, int]

    # ------------------------------------------------------------ access ---
    def permitted_agents(self, handle: str) -> set[str] | None:
        """Which backends this person may use. None means no restriction.

        A handle in JERRY_ALLOWED_USERNAMES is unrestricted. A handle in
        JERRY_USER_AGENTS is limited to the backends listed against it. With
        no allowlist configured at all, nobody is restricted.
        """
        folded = normalize_username(handle)
        if folded in self.allowed_usernames:
            return None
        grant = self.user_agents.get(folded)
        if grant is not None:
            if ALL_AGENTS in grant:
                return None
            return set(grant)
        if not self.access_locked:
            return None
        return set()  # known to the gate by id/role only, or not allowed

    def may_use(self, handle: str, agent_key: str) -> bool:
        allowed = self.permitted_agents(handle)
        if allowed is None:
            return True
        return (agents.canonical(agent_key) or agent_key) in allowed

    def known_handle(self, handle: str) -> bool:
        folded = normalize_username(handle)
        return folded in self.allowed_usernames or folded in self.user_agents

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
            user_agents=_env_user_agents("JERRY_USER_AGENTS"),
            access_locked=_env_has_content(
                "JERRY_ALLOWED_USER_IDS",
                "JERRY_ALLOWED_ROLE_IDS",
                "JERRY_ALLOWED_USERNAMES",
                "JERRY_USER_AGENTS",
            ),
            use_threads=_env_bool("JERRY_USE_THREADS", True),
            dispatch_channel_id=_env_int("JERRY_DISPATCH_CHANNEL_ID", 0),
            subs=_env_subs("JERRY_SUBS"),
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

    def agent_key_for(
        self, handle: str, channel_id: int | None, guild_id: int | None
    ) -> tuple[str, str]:
        """The agent this person gets here, and a note if it wasn't the default.

        The channel/server default wins when they're allowed to use it.
        Otherwise they fall back to a backend they do have, preferring one
        that's actually installed -- so somebody granted only Grok doesn't
        have to know what the channel default is.
        """
        base = self.agent_key(channel_id, guild_id)
        allowed = self.settings.permitted_agents(handle)
        if allowed is None or base in allowed:
            return base, ""
        if not allowed:
            return base, "none"
        ordered = sorted(
            allowed,
            key=lambda key: (
                not getattr(agents.get(key), "available", False),
                key,
            ),
        )
        pick = ordered[0]
        return pick, (
            f"`{base}` isn't on your list, so I used **{pick}**"
        )

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
