"""Agent backends.

Every backend is an *interactive CLI coding agent* driven in headless mode.
Auth is whatever that CLI already has on this machine, i.e. a signed-in paid
account (Google AI Pro for Gemini CLI, Claude Pro/Max for Claude Code,
ChatGPT Plus/Pro for Codex, a Copilot subscription for the GitHub CLI, ...).
Jerry never touches API keys; it just shells out to a logged-in binary.

Flags drift between CLI releases, so the whole registry is data and can be
patched at runtime from ``agents.local.json`` (see ``load_overrides``).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
SPINNER_RE = re.compile(r"[⠀-⣿─-╿■-◿]")

# Beyond this many characters the prompt is handed over as a file rather than as
# an argument. Windows command lines cap out around 32k and we would rather not
# discover that limit live on stage.
ARGV_PROMPT_LIMIT = 6000

# How a CLI is told to go read its task from a file instead of from argv.
TASK_FILE_POINTER = (
    "Your full instructions are in the UTF-8 text file at {path}. "
    "Read that file first, then carry out exactly what it says. "
    "The file is scratch space outside the repository; do not edit or commit it."
)


@dataclass(frozen=True)
class AgentSpec:
    key: str
    label: str
    emoji: str
    binary: str
    # argv template. "{prompt}" is replaced by the prompt (and dropped when the
    # prompt is piped on stdin). "{MODEL}" / "{AUTO}" expand to zero or more
    # arguments, in place, so sub-command ordering stays correct.
    argv: tuple[str, ...]
    model_args: tuple[str, ...] = ()
    auto_args: tuple[str, ...] = ()
    full_auto_args: tuple[str, ...] = ()
    default_model: str | None = None
    install: str = ""
    login: str = ""
    blurb: str = ""

    @property
    def resolved_binary(self) -> str | None:
        return shutil.which(self.binary)

    @property
    def available(self) -> bool:
        return self.resolved_binary is not None

    def build_argv(
        self,
        prompt: str,
        *,
        model: str | None = None,
        full_auto: bool = False,
    ) -> list[str]:
        chosen_model = model or self.default_model
        model_args = (
            [a.replace("{model}", chosen_model) for a in self.model_args]
            if chosen_model and self.model_args
            else []
        )
        auto_args = list(self.full_auto_args if full_auto else self.auto_args)

        out: list[str] = [self.binary]
        for token in self.argv:
            if token == "{MODEL}":
                out.extend(model_args)
            elif token == "{AUTO}":
                out.extend(auto_args)
            elif token == "{prompt}":
                out.append(prompt)
            else:
                out.append(token)
        return out


REGISTRY: dict[str, AgentSpec] = {}


def _register(spec: AgentSpec) -> None:
    REGISTRY[spec.key] = spec


_register(
    AgentSpec(
        key="gemini",
        label="Gemini CLI",
        emoji="✨",
        binary="gemini",
        argv=("{AUTO}", "{MODEL}", "-p", "{prompt}"),
        model_args=("-m", "{model}"),
        auto_args=("--approval-mode", "auto_edit"),
        full_auto_args=("--approval-mode", "yolo"),
        install="npm i -g @google/gemini-cli",
        login="run `gemini` once and choose Login with Google (uses your Google AI Pro/Ultra plan)",
        blurb="The house default. Enormous context window, cheerful about big repos.",
    )
)

_register(
    AgentSpec(
        key="claude",
        label="Claude Code",
        emoji="\U0001f9e0",
        binary="claude",
        argv=("{AUTO}", "{MODEL}", "-p", "{prompt}"),
        model_args=("--model", "{model}"),
        auto_args=("--permission-mode", "acceptEdits"),
        full_auto_args=("--dangerously-skip-permissions",),
        install="npm i -g @anthropic-ai/claude-code",
        login="run `claude` once and sign in with your Claude Pro/Max account",
        blurb="Strong multi-file refactors. Reads before it writes.",
    )
)

_register(
    AgentSpec(
        key="codex",
        label="OpenAI Codex CLI",
        emoji="\U0001f9ea",
        binary="codex",
        argv=("exec", "{AUTO}", "{MODEL}", "{prompt}"),
        model_args=("-m", "{model}"),
        auto_args=("--sandbox", "workspace-write"),
        full_auto_args=("--dangerously-bypass-approvals-and-sandbox",),
        install="npm i -g @openai/codex",
        login="run `codex` once and choose Sign in with ChatGPT (Plus/Pro/Team plan)",
        blurb="Sandboxed by default. Good at surgical edits.",
    )
)

_register(
    AgentSpec(
        key="copilot",
        label="GitHub Copilot CLI",
        emoji="\U0001f419",
        binary="copilot",
        argv=("-p", "{prompt}", "{AUTO}", "{MODEL}", "--no-color"),
        model_args=("--model", "{model}"),
        # Copilot headless mode is all-or-nothing on tool approval: without
        # --allow-all-tools it blocks waiting for a human, who never arrives.
        auto_args=("--allow-all-tools",),
        full_auto_args=("--allow-all-tools",),
        install="npm i -g @github/copilot",
        login="run `copilot` then /login with your GitHub Copilot subscription",
        blurb="Knows GitHub. Headless mode trusts every tool, so mind the workspace.",
    )
)

_register(
    AgentSpec(
        key="cursor",
        label="Cursor Agent",
        emoji="\U0001f5b1️",
        binary="cursor-agent",
        argv=("{AUTO}", "{MODEL}", "-p", "{prompt}"),
        model_args=("-m", "{model}"),
        auto_args=("--force",),
        full_auto_args=("--force",),
        install="see cursor.com/cli",
        login="run `cursor-agent login` with your Cursor Pro account",
        blurb="The Cursor brain, minus the editor.",
    )
)

_register(
    AgentSpec(
        key="qwen",
        label="Qwen Code",
        emoji="\U0001f409",
        binary="qwen",
        argv=("{AUTO}", "{MODEL}", "-p", "{prompt}"),
        model_args=("-m", "{model}"),
        auto_args=("--approval-mode", "auto_edit"),
        full_auto_args=("--approval-mode", "yolo"),
        install="npm i -g @qwen-code/qwen-code",
        login="run `qwen` once and sign in with your Qwen account",
        blurb="Gemini-CLI-shaped, different engine. Handy as a tiebreaker.",
    )
)

_register(
    AgentSpec(
        key="opencode",
        label="opencode",
        emoji="\U0001f513",
        binary="opencode",
        argv=("run", "{MODEL}", "{prompt}"),
        model_args=("-m", "{model}"),
        install="npm i -g opencode-ai",
        login="run `opencode auth login` and pick a provider you already pay for",
        blurb="Provider-agnostic. Points at whichever account you already have.",
    )
)

_register(
    AgentSpec(
        key="amp",
        label="Amp",
        emoji="⚡",
        binary="amp",
        argv=("-x", "{prompt}"),
        install="npm i -g @sourcegraph/amp",
        login="run `amp login`",
        blurb="Fast and opinionated.",
    )
)

DEFAULT_AGENT = "gemini"


def load_overrides(path: Path) -> list[str]:
    """Patch or add specs from a JSON file. Returns human-readable notes.

    Shape: {"gemini": {"argv": ["-p", "{prompt}"], "default_model": "..."}}
    An unknown key creates a brand new backend; ``binary`` and ``argv`` are
    then required.
    """
    import json

    notes: list[str] = []
    if not path.exists():
        return notes
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        return [f"could not read {path.name}: {exc}"]
    if not isinstance(data, dict):
        return [f"{path.name} should hold a JSON object"]

    tuple_fields = {"argv", "model_args", "auto_args", "full_auto_args"}
    for key, patch in data.items():
        if not isinstance(patch, dict):
            continue
        clean = {
            k: (tuple(v) if k in tuple_fields and isinstance(v, list) else v)
            for k, v in patch.items()
        }
        clean.pop("key", None)
        base = REGISTRY.get(key)
        if base is None:
            if "binary" not in clean or "argv" not in clean:
                notes.append(f"{key}: new backends need binary and argv")
                continue
            clean.setdefault("label", key)
            clean.setdefault("emoji", "\U0001f916")
            try:
                _register(AgentSpec(key=key, **clean))  # type: ignore[arg-type]
            except TypeError as exc:
                notes.append(f"{key}: {exc}")
                continue
            notes.append(f"{key}: added from {path.name}")
        else:
            try:
                _register(replace(base, **clean))
            except TypeError as exc:
                notes.append(f"{key}: {exc}")
                continue
            notes.append(f"{key}: patched from {path.name}")
    return notes


def get(key: str | None) -> AgentSpec | None:
    if not key:
        return None
    return REGISTRY.get(key.strip().lower())


def all_specs() -> list[AgentSpec]:
    return sorted(
        REGISTRY.values(),
        key=lambda s: (s.key != DEFAULT_AGENT, not s.available, s.key),
    )


def available_specs() -> list[AgentSpec]:
    return [s for s in all_specs() if s.available]


def pick_fallback() -> AgentSpec | None:
    """First available backend, preferring the configured default."""
    preferred = get(DEFAULT_AGENT)
    if preferred is not None and preferred.available:
        return preferred
    avail = available_specs()
    return avail[0] if avail else None


# --------------------------------------------------------------------------- #
# Running one
# --------------------------------------------------------------------------- #

BRIEF = """You are being driven headlessly by a Discord bot named Jerry. A human asked for the task below from a chat window and cannot answer follow-up questions, so make reasonable assumptions, state them, and finish the job.

Working rules:
- The repository is at: {workspace}
- It may be large and barely touched. Orient yourself first (layout, build files, entry points) before editing anything.
- Never ask the human a question. If something is ambiguous, pick the most conventional option, say so, and keep going.
- Prefer small, reviewable changes. Do not reformat files you did not need to touch.
- Do not commit, push, or open pull requests unless the task explicitly asks for it.
- Finish with a short summary: what changed, which files, and anything you deliberately skipped. Keep it under 250 words, it is going into a chat message and not a design doc.
"""

HISTORY_HEADER = "Earlier in this Discord conversation (oldest first):"


@dataclass
class AgentResult:
    ok: bool
    output: str
    exit_code: int | None
    duration: float
    argv: list[str]
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None


@dataclass
class RunHandle:
    """Live view of a running agent, for progress reporting and cancellation."""

    spec: AgentSpec
    started: float = field(default_factory=time.monotonic)
    lines: list[str] = field(default_factory=list)
    proc: asyncio.subprocess.Process | None = None
    killed: bool = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def tail(self, n: int = 1) -> str:
        meaningful = [line for line in self.lines if line.strip()]
        return "\n".join(meaningful[-n:])

    def kill(self) -> None:
        self.killed = True
        _kill_tree(self.proc)


def _kill_tree(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    if sys.platform == "win32":
        # proc.kill() would leave the agent's own children (node, git, ...) alive.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
            return
        except Exception:  # noqa: BLE001 - fall through to a plain kill
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def clean_output(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [SPINNER_RE.sub("", line).rstrip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return "\n".join(out).strip()


def build_prompt(task: str, workspace: Path, history: Iterable[str] = ()) -> str:
    parts = [BRIEF.format(workspace=workspace)]
    hist = [h for h in history if h.strip()]
    if hist:
        parts.append(HISTORY_HEADER + "\n" + "\n".join(hist))
    parts.append("Task:\n" + task.strip())
    return "\n\n".join(parts)


def _write_task_file(prompt: str) -> Path:
    """Park the prompt in a temp file so it never touches a command line."""
    import tempfile

    folder = Path(tempfile.gettempdir()) / "jerry-tasks"
    folder.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        prefix="task-", suffix=".md", dir=str(folder), text=False
    )
    with os.fdopen(handle, "wb") as fh:
        fh.write(prompt.encode("utf-8"))
    return Path(name)


def _discard(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def is_batch_shim(binary: str) -> bool:
    """True for a Windows .cmd/.bat wrapper, e.g. npm's `gemini.CMD`.

    CreateProcess runs these through cmd.exe, which re-parses the command line.
    A prompt containing a double quote, `&` or `%` would then be mangled or, in
    the worst case, executed. Anything we exec this way gets its prompt out of
    argv and into a file.
    """
    return sys.platform == "win32" and binary.lower().endswith((".cmd", ".bat"))


def argv_hostile(text: str) -> bool:
    """Characters we refuse to put on a cmd.exe command line."""
    return any(ch in text for ch in '"%\r\n') or "^" in text


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "TERM": "dumb",
            "CI": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    return env


async def run_agent(
    spec: AgentSpec,
    prompt: str,
    *,
    workspace: Path,
    model: str | None = None,
    full_auto: bool = False,
    timeout: float = 900.0,
    handle: RunHandle | None = None,
    on_line: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run one headless agent turn and collect its output."""
    handle = handle or RunHandle(spec=spec)
    started = time.monotonic()

    binary = spec.resolved_binary
    if binary is None:
        return AgentResult(
            ok=False,
            output="",
            exit_code=None,
            duration=0.0,
            argv=[spec.binary],
            error=(
                f"`{spec.binary}` is not on PATH. Install it with `{spec.install}`, "
                f"then {spec.login}."
            ),
        )

    # The prompt is arbitrary text from a chat message. Keep it off the command
    # line whenever the command line cannot carry it safely.
    task_file: Path | None = None
    argv_prompt = prompt
    if len(prompt) > ARGV_PROMPT_LIMIT or is_batch_shim(binary):
        task_file = _write_task_file(prompt)
        pointer = TASK_FILE_POINTER.format(path=task_file)
        if argv_hostile(str(task_file)):
            # A temp path we cannot quote safely. Vanishingly rare; bail loudly
            # rather than build a command line we do not trust.
            task_file.unlink(missing_ok=True)
            return AgentResult(
                False,
                "",
                None,
                time.monotonic() - started,
                [binary],
                error=(
                    "Refusing to run: the temp path for this task contains "
                    "characters I cannot pass safely on Windows. Set TMP to a "
                    "plain path like C:\\jerry-tmp."
                ),
            )
        argv_prompt = pointer

    argv = spec.build_argv(argv_prompt, model=model, full_auto=full_auto)
    argv[0] = binary

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=child_env(),
        )
    except FileNotFoundError:
        _discard(task_file)
        return AgentResult(
            False,
            "",
            None,
            time.monotonic() - started,
            argv,
            error=f"could not start `{spec.binary}`",
        )
    except OSError as exc:
        _discard(task_file)
        return AgentResult(
            False, "", None, time.monotonic() - started, argv, error=str(exc)
        )

    handle.proc = proc

    async def pump() -> None:
        assert proc.stdout is not None
        while True:
            try:
                raw = await proc.stdout.readline()
            except ValueError:
                continue  # absurdly long single line; keep reading
            if not raw:
                break
            line = clean_output(raw.decode("utf-8", "replace"))
            if not line:
                continue
            handle.lines.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - progress must never break a run
                    pass
            if len(handle.lines) > 4000:
                del handle.lines[:2000]

    pump_task = asyncio.create_task(pump())
    timed_out = False
    cancelled = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
    except asyncio.CancelledError:
        cancelled = True
        _kill_tree(proc)
        raise
    finally:
        if cancelled or timed_out:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass
        _discard(task_file)

    output = clean_output("\n".join(handle.lines))
    code = proc.returncode
    return AgentResult(
        ok=(code == 0 and not timed_out),
        output=output,
        exit_code=code,
        duration=time.monotonic() - started,
        argv=argv,
        timed_out=timed_out,
        cancelled=handle.killed,
    )
