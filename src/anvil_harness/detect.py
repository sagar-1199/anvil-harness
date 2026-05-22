"""Detect which agent CLIs are installed + auth'd on the user's machine.

The wizard + Settings screen call `detect_all()` to render an honest picture:
"Claude Code is installed and logged in", "Codex is installed but not logged
in", "Gemini isn't installed — here's how to get it".

Each AgentInfo carries a small bundle the UI can render directly. No nested
logic in the templates.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


# Per-agent metadata. Add an entry here to make a new backend discoverable.
# install_url is shown in the wizard as a one-click link.
KNOWN_AGENTS: dict[str, dict[str, str]] = {
    "claude": {
        "label": "Claude Code",
        "cli": "claude",
        "install_hint": "npm install -g @anthropic-ai/claude-code",
        "install_url": "https://docs.anthropic.com/en/docs/claude-code/setup",
        "auth_hint": "Run `claude` once in a terminal and complete the OAuth flow.",
    },
    "codex": {
        "label": "OpenAI Codex CLI",
        "cli": "codex",
        "install_hint": "npm install -g @openai/codex",
        "install_url": "https://github.com/openai/codex",
        "auth_hint": "Run `codex login` and complete the OAuth flow.",
    },
    "mock": {
        # Offline echo backend — always available, no install. Useful for
        # demoing Anvil or hacking on UI without burning real tokens.
        "label": "Mock (offline echo)",
        "cli": "",
        "install_hint": "",
        "install_url": "",
        "auth_hint": "",
    },
}


@dataclass
class AgentInfo:
    name: str             # registry key (matches engine.ENGINES)
    label: str            # human-readable
    installed: bool       # CLI is on PATH (or always-on for mock)
    path: str | None      # absolute path to CLI binary, or None
    version: str | None   # CLI --version output (first line), or None
    auth_ok: bool | None  # True/False if detectable, None if we can't tell
    install_hint: str     # shell command the wizard offers to run
    install_url: str      # docs link
    auth_hint: str        # what to do if installed but not auth'd


def detect_one(name: str) -> AgentInfo:
    meta = KNOWN_AGENTS.get(name)
    if meta is None:
        return AgentInfo(
            name=name, label=name, installed=False, path=None, version=None,
            auth_ok=None, install_hint="", install_url="", auth_hint="",
        )

    # Mock is always "installed" — it's pure Python.
    if name == "mock":
        return AgentInfo(
            name=name, label=meta["label"], installed=True, path=None,
            version="builtin", auth_ok=True,
            install_hint="", install_url="", auth_hint="",
        )

    cli = meta["cli"]
    path = shutil.which(cli) if cli else None
    installed = path is not None
    version: str | None = None
    auth_ok: bool | None = None

    if installed and path:
        version = _version_of(path)
        auth_ok = _auth_ok(name, path)

    return AgentInfo(
        name=name,
        label=meta["label"],
        installed=installed,
        path=path,
        version=version,
        auth_ok=auth_ok,
        install_hint=meta["install_hint"],
        install_url=meta["install_url"],
        auth_hint=meta["auth_hint"],
    )


def detect_all() -> list[AgentInfo]:
    """Return one AgentInfo per known agent, in registry order."""
    return [detect_one(name) for name in KNOWN_AGENTS]


def _version_of(cli_path: str) -> str | None:
    """Best-effort `--version`. Returns the first non-empty stripped line, or
    None if the binary doesn't support --version or hangs."""
    try:
        proc = subprocess.run(
            [cli_path, "--version"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or proc.stderr or "").strip()
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line[:120]  # don't bloat the UI with long banners
    return None


def _auth_ok(name: str, cli_path: str) -> bool | None:
    """Per-agent auth check. Returns None if we genuinely can't tell — better
    to show "auth status unknown" than to falsely red-flag a working install.
    """
    if name == "claude":
        # Claude Code stores OAuth tokens under ~/.claude/.credentials.json (or
        # in the macOS Keychain for some installs). Presence of either is a
        # strong "logged in" signal — but absence doesn't prove "logged out"
        # because the Keychain path is opaque. Be honest: True or None.
        creds = Path.home() / ".claude" / ".credentials.json"
        if creds.exists() and creds.stat().st_size > 0:
            return True
        return None
    if name == "codex":
        # Two valid auth shapes depending on Codex CLI version:
        #   • Newer (OAuth): ~/.codex/auth.json
        #   • Older (API key): OPENAI_API_KEY env var
        # Either one is "logged in" enough to try a run; if both are absent
        # we return None (unknown) rather than False — the run-time error
        # message is a better teacher than a wizard red flag.
        import os
        creds = Path.home() / ".codex" / "auth.json"
        if creds.exists() and creds.stat().st_size > 0:
            return True
        if os.environ.get("OPENAI_API_KEY"):
            return True
        return None
    return None
