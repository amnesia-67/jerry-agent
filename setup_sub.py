"""Setup for a sub-Jerry. Run this on YOUR machine.

    python setup_sub.py            interactive setup, asks what it needs
    python setup_sub.py --check    verify an existing setup, asks nothing

The interactive mode creates the virtualenv, installs dependencies, asks for
the handful of things only you know, writes your .env, and prints the lines to
send back to whoever runs master Jerry.

``--check`` prompts for nothing and exits non-zero if anything is wrong, so it
works unattended -- useful when a coding agent is doing the setup for you, or
in a terminal you can't type into.

Deliberately depends on nothing but the standard library, so it works before
the virtualenv exists.
"""

from __future__ import annotations

import base64
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
VENV = ROOT / ".venv"
MIN_PYTHON = (3, 10)

# View Channel, Send Messages, Add Reactions, Embed Links, Attach Files,
# Read Message History, Create Public Threads, Send Messages in Threads.
INVITE_PERMISSIONS = 309237763136


def say(text: str = "") -> None:
    print(text, flush=True)


def head(text: str) -> None:
    say()
    say(text)
    say("-" * len(text))


def die(text: str) -> "None":
    say()
    say(f"[stopped] {text}")
    raise SystemExit(1)


def ask(prompt: str, default: str = "", *, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            got = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            die("No input available. Run this in a terminal you can type into.")
        got = got or default
        if got or not required:
            return got
        say("  ...that one is required.")


def ask_yes(prompt: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        got = input(f"{prompt} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not got:
        return default
    return got.startswith("y")


def ask_int(prompt: str) -> int:
    while True:
        raw = ask(prompt)
        digits = "".join(c for c in raw if c.isdigit())
        if digits and len(digits) >= 17:
            return int(digits)
        say("  ...that doesn't look like a Discord id (17+ digits).")


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def bot_user_id(token: str) -> int | None:
    """A bot token's first segment is its application id, which for a bot is
    also its user id. Saves everyone hunting through the dev portal."""
    first = token.split(".", 1)[0]
    padded = first + "=" * (-len(first) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("ascii")
    except Exception:  # noqa: BLE001
        return None
    return int(decoded) if decoded.isdigit() else None


# --------------------------------------------------------------------------- #


def check_python() -> None:
    head("1. Checking Python")
    if sys.version_info < MIN_PYTHON:
        die(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is needed; this is "
            f"{sys.version_info.major}.{sys.version_info.minor}."
        )
    say(f"  Python {sys.version.split()[0]}  ok")


def build_venv() -> Path:
    head("2. Virtualenv and dependencies")
    python = venv_python()
    if python.exists():
        say(f"  reusing {VENV.name}")
    else:
        say(f"  creating {VENV.name} ...")
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV)], capture_output=True, text=True
        )
        if result.returncode != 0:
            die(f"could not create the virtualenv:\n{result.stdout}{result.stderr}")
    say("  installing discord.py and python-dotenv ...")
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        die(f"pip install failed:\n{result.stdout}{result.stderr}")
    say("  dependencies installed  ok")
    return python


def pick_agent() -> "tuple[str, str, str, str]":
    """Returns (key, label, install command, login instructions)."""
    sys.path.insert(0, str(ROOT))
    from jerry import agents  # noqa: PLC0415 - needs ROOT on the path first

    head("3. Which agent will run on this machine?")
    specs = agents.all_specs()
    for index, spec in enumerate(specs, start=1):
        mark = "installed" if spec.available else "not installed"
        say(f"  {index:>2}. {spec.key:<12} {spec.label:<20} ({mark})")
    say()
    while True:
        raw = ask("  Number, or an agent name", "1")
        if raw.isdigit() and 1 <= int(raw) <= len(specs):
            spec = specs[int(raw) - 1]
            break
        found = agents.get(raw)
        if found is not None:
            spec = found
            break
        say("  ...not one of those.")
    return spec.key, spec.label, spec.install, spec.login


def pick_workspace() -> Path:
    head("4. Which folder may agents edit on this machine?")
    say("  This is your own checkout. It is the only place the agent works.")
    say()
    while True:
        raw = ask("  Path to the repo", str(Path.home() / "jerry-workspace"))
        path = Path(raw.strip().strip('"')).expanduser()
        if path.is_dir():
            return path.resolve()
        say(f"  {path} doesn't exist.")
        url = ask(
            "  git URL to clone there (blank to just create the folder)",
            "",
            required=False,
        )
        if url:
            if shutil.which("git") is None:
                say("  git isn't installed, so I can't clone.")
                continue
            say(f"  cloning into {path} ...")
            result = subprocess.run(
                ["git", "clone", url, str(path)], capture_output=True, text=True
            )
            if result.returncode != 0:
                say(f"  clone failed:\n{result.stderr.strip()[:400]}")
                continue
        else:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                say(f"  couldn't create it: {exc}")
                continue
        return path.resolve()


def collect() -> dict[str, str]:
    head("5. Discord details")
    say("  You need your OWN bot, separate from master Jerry:")
    say("    discord.com/developers/applications -> New Application -> Bot")
    say("    -> Reset Token -> copy it. Nobody else should ever see this token.")
    say()
    token = getpass.getpass("  Your sub-bot token (hidden as you type): ").strip()
    if not token or token.count(".") < 2:
        die("that doesn't look like a bot token.")

    handle = ask(
        "  Your Discord @handle (Settings > Account, no @)"
    ).strip().strip("@.").lower()
    say()
    say("  Ask whoever runs master Jerry for these two ids.")
    say("  (Discord > Settings > Advanced > Developer Mode, then right-click.)")
    master_id = ask_int("  Master Jerry's bot user id")
    channel_id = ask_int("  The private dispatch channel id")

    return {
        "token": token,
        "handle": handle,
        "master_id": str(master_id),
        "channel_id": str(channel_id),
    }


def write_env(values: dict[str, str], workspace: Path, agent: str, full_auto: bool) -> None:
    head("6. Writing .env")
    if ENV_PATH.exists():
        if not ask_yes(f"  {ENV_PATH.name} exists. Overwrite it?", False):
            # Path(".env").with_suffix() would give ".env.env.bak"; name it plainly.
            backup = ENV_PATH.parent / ".env.backup"
            shutil.copy2(ENV_PATH, backup)
            say(f"  kept a copy at {backup.name}; writing the new one anyway.")
    body = f"""# sub-Jerry on @{values['handle']}'s machine. Written by setup_sub.py.
# This file holds a bot token. It is gitignored; keep it that way.
DISCORD_TOKEN={values['token']}

JERRY_SUB_OWNER={values['handle']}
JERRY_MASTER_ID={values['master_id']}
JERRY_DISPATCH_CHANNEL_ID={values['channel_id']}

JERRY_WORKSPACE={workspace}
JERRY_DEFAULT_AGENT={agent}
JERRY_TIMEOUT=900

# 1 lets agents run shell commands here unattended. 0 means they can write
# files but commands get denied, and some agents exit 0 having skipped them.
JERRY_FULL_AUTO={'1' if full_auto else '0'}
"""
    ENV_PATH.write_text(body, encoding="utf-8")
    say(f"  wrote {ENV_PATH}")


def main() -> None:
    say("=" * 64)
    say(" sub-Jerry setup")
    say(" Runs coding agents on THIS machine, with YOUR CLI login,")
    say(" for requests that come through the one shared Jerry in Discord.")
    say("=" * 64)

    check_python()
    python = build_venv()
    agent_key, agent_label, install_cmd, login_hint = pick_agent()
    workspace = pick_workspace()

    head("Permissions")
    say("  Full auto lets the agent run shell commands here with no sandbox")
    say("  and no prompting: builds, tests, git, anything. Without it the")
    say("  agent can still edit files, but commands are refused.")
    say()
    full_auto = ask_yes("  Enable full auto on this machine?", False)

    values = collect()
    write_env(values, workspace, agent_key, full_auto)

    head("7. Agent CLI")
    sys.path.insert(0, str(ROOT))
    from jerry import agents  # noqa: PLC0415

    spec = agents.get(agent_key)
    if spec is not None and spec.available:
        say(f"  {agent_label} is already installed here  ok")
    else:
        say(f"  {agent_label} is not installed.")
        say(f"  install:  {install_cmd}")
        if install_cmd.startswith("npm ") and ask_yes("  Run that now?", True):
            if shutil.which("npm") is None:
                say("  npm isn't on PATH. Install Node.js, then run it yourself.")
            else:
                result = subprocess.run(install_cmd, shell=True)
                if result.returncode != 0:
                    say("  that didn't work; run it by hand.")
    say()
    say(f"  Then sign in once:  {login_hint}")
    say("  Headless runs reuse that login, so it only has to happen once.")

    uid = bot_user_id(values["token"])
    head("8. Invite your bot to the server")
    if uid:
        say(
            "  https://discord.com/api/oauth2/authorize"
            f"?client_id={uid}&scope=bot&permissions={INVITE_PERMISSIONS}"
        )
    else:
        say("  Couldn't read the application id from that token; use the")
        say("  OAuth2 > URL Generator in the developer portal instead.")
    say("  It only needs access to the dispatch channel.")

    head("9. Send these to whoever runs master Jerry")
    if uid:
        say(f"  JERRY_SUBS entry:   {values['handle']}:{uid}")
    say(f"  Agent on this box:  {agent_key}")

    head("Start it")
    rel = venv_python().relative_to(ROOT) if venv_python().is_relative_to(ROOT) else python
    say(f"  {rel} subjerry.py")
    say()
    say("  Leave it running. It only answers master Jerry, only in the")
    say("  dispatch channel, and only when master mentions it.")
    say()


# --------------------------------------------------------------------------- #
# --check: verify without asking anything
# --------------------------------------------------------------------------- #

REQUIRED_KEYS = (
    "DISCORD_TOKEN",
    "JERRY_SUB_OWNER",
    "JERRY_MASTER_ID",
    "JERRY_DISPATCH_CHANNEL_ID",
    "JERRY_WORKSPACE",
    "JERRY_DEFAULT_AGENT",
)


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser, so --check needs no third-party import."""
    found: dict[str, str] = {}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found[key.strip()] = value.strip().strip('"')
    return found


def run_check() -> int:
    say("sub-Jerry self-check")
    say("=" * 64)
    problems: list[str] = []

    def ok(label: str, detail: str = "") -> None:
        say(f"  [ok]   {label}" + (f"  {detail}" if detail else ""))

    def bad(label: str, fix: str) -> None:
        say(f"  [FAIL] {label}")
        say(f"         fix: {fix}")
        problems.append(label)

    # 1. virtualenv and dependencies
    python = venv_python()
    if not python.exists():
        bad("virtualenv missing", f"{sys.executable} -m venv .venv")
    else:
        probe = subprocess.run(
            [str(python), "-c", "import discord, dotenv; print(discord.__version__)"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            ok("dependencies", f"discord.py {probe.stdout.strip()}")
        else:
            bad(
                "dependencies not installed",
                f"{python} -m pip install -r requirements.txt",
            )

    # 2. .env contents
    values = read_env_file(ENV_PATH)
    if not values:
        bad(".env missing or empty", "copy .env.sub.example to .env and fill it in")
    else:
        missing = [k for k in REQUIRED_KEYS if not values.get(k)]
        if missing:
            bad(f".env missing: {', '.join(missing)}", "fill those in")
        else:
            ok(".env", f"owner @{values['JERRY_SUB_OWNER']}")

        for key in ("JERRY_MASTER_ID", "JERRY_DISPATCH_CHANNEL_ID"):
            raw = values.get(key, "")
            if raw and (not raw.isdigit() or len(raw) < 17):
                bad(f"{key} is not a Discord id", "17+ digits, no quotes")

        token = values.get("DISCORD_TOKEN", "")
        if token:
            uid = bot_user_id(token)
            if uid is None:
                bad("DISCORD_TOKEN looks malformed", "re-copy it from the portal")
            else:
                ok("bot token parses", f"this sub's bot user id is {uid}")
                if str(uid) == values.get("JERRY_MASTER_ID"):
                    bad(
                        "this token IS master's",
                        "each person needs their OWN bot, not master's token",
                    )

    # 3. workspace
    raw_ws = values.get("JERRY_WORKSPACE", "")
    if raw_ws:
        workspace = Path(raw_ws).expanduser()
        if workspace.is_dir():
            git = " (git repo)" if (workspace / ".git").exists() else " (not a git repo)"
            ok("workspace", f"{workspace}{git}")
        else:
            bad(f"workspace {workspace} does not exist", "create or clone it")

    # 4. the agent itself
    agent_key = values.get("JERRY_DEFAULT_AGENT", "")
    if agent_key:
        sys.path.insert(0, str(ROOT))
        from jerry import agents  # noqa: PLC0415

        spec = agents.get(agent_key)
        if spec is None:
            bad(f"unknown agent {agent_key!r}", "pick one from /agent list")
        elif not spec.available:
            bad(f"{spec.label} is not installed", spec.install)
        else:
            probe = subprocess.run(
                [spec.resolved_binary or spec.binary, "--version"],
                capture_output=True,
                text=True,
                env={**os.environ, "NO_COLOR": "1"},
            )
            if probe.returncode == 0:
                ok(spec.label, (probe.stdout or probe.stderr).strip().splitlines()[-1][:40])
            else:
                bad(
                    f"{spec.label} is on PATH but won't run",
                    "it may be a stub; install the real CLI",
                )

    say("=" * 64)
    if problems:
        say(f"{len(problems)} problem(s) to fix. Nothing was changed.")
        say()
        say("Note: this check cannot tell whether the agent CLI is signed in.")
        say("That needs a browser and a human. Run the CLI once by hand.")
        return 1

    say("All checks passed. Start it with:")
    say(f"  {venv_python()} subjerry.py")
    say()
    say("This does NOT prove the CLI is signed in -- that needs a browser and a")
    say("human. If jobs come back with an auth error, run the CLI once by hand.")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        raise SystemExit(run_check())
    try:
        main()
    except KeyboardInterrupt:
        say("\n[cancelled] nothing was written.")
        raise SystemExit(130)
