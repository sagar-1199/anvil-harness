"""Global + per-project user config.

Two layers, simplest-wins:

  ~/.anvil/config.toml              global, set by the wizard / Settings screen
  <project>/.harness/config.toml    per-project override (only fields that differ)

`load()` returns a `Config` with project values overlaying global ones.
`save_global()` rewrites the global TOML in-place; `save_project_override()`
writes only the keys that differ from global, so per-project files stay small
and the user can see at a glance what they've customized.

Schema is intentionally small. Resist the urge to add knobs — every option
the wizard / Settings screen has to explain is friction for non-technical
users.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

# Python's stdlib has tomllib for reading but no writer — keep the writer
# tiny and inline rather than pulling in tomli-w just for this.

CONFIG_DIR = Path.home() / ".anvil"
GLOBAL_CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class Config:
    # Which agent new conversations default to. Must be a key in
    # engine.ENGINES at load time, otherwise the engine resolver falls
    # back to claude.
    default_agent: str = "claude"

    # Which agents the user wants surfaced in the picker. Empty means
    # "all registered engines"; only useful if a user wants to hide the
    # mock engine, or hide one they haven't authenticated yet.
    enabled_agents: list[str] = field(default_factory=list)

    # Default model passed to ClaudeEngine. Per-project Project.model
    # overrides this when set. Empty = let the engine pick its default.
    default_model: str = ""

    # True once the user has clicked through the first-launch wizard.
    # Drives whether app.py opens WelcomeWizard on mount.
    onboarded: bool = False

    # Opt-in only — Anvil does not collect telemetry today. Reserved
    # for a future "help improve Anvil" toggle, off by default.
    telemetry_opt_in: bool = False


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + rename so a crash mid-write can't truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _to_toml(data: dict[str, Any]) -> str:
    """Serialize the Config dict to TOML.

    Restricted to the shapes we actually use (str, bool, list[str]). Keep
    this private — if you need richer TOML, switch to `tomli-w` instead of
    extending here.
    """
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key} = "{escaped}"')
        elif isinstance(value, list):
            parts = []
            for item in value:
                escaped = str(item).replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'"{escaped}"')
            lines.append(f"{key} = [{', '.join(parts)}]")
        else:
            # Skip unknown types rather than raise — keeps the writer
            # forward-compatible if someone adds a field by accident.
            continue
    return "\n".join(lines) + "\n"


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Drop keys not in Config; coerce types loosely."""
    valid = {f.name for f in fields(Config)}
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in valid:
            continue
        out[k] = v
    return out


def load_global() -> Config:
    if not GLOBAL_CONFIG_PATH.exists():
        return Config()
    try:
        with GLOBAL_CONFIG_PATH.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # Corrupt config shouldn't brick the app. Fall back to defaults; the
        # user can re-run the wizard or fix the file.
        return Config()
    return Config(**_coerce(raw))


def save_global(cfg: Config) -> None:
    _atomic_write(GLOBAL_CONFIG_PATH, _to_toml(asdict(cfg)))


def project_config_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / "config.toml"


def load_project_override(project_dir: Path) -> dict[str, Any]:
    path = project_config_path(project_dir)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return _coerce(tomllib.load(f))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_project_override(project_dir: Path, overrides: dict[str, Any]) -> None:
    """Write only fields that differ from the global default. Empty dict
    deletes the override file (the project follows global)."""
    path = project_config_path(project_dir)
    if not overrides:
        if path.exists():
            path.unlink()
        return
    _atomic_write(path, _to_toml(overrides))


def load(project_dir: Path | None = None) -> Config:
    """Load global config with project overrides layered on top."""
    cfg = load_global()
    if project_dir is None:
        return cfg
    overrides = load_project_override(project_dir)
    if not overrides:
        return cfg
    merged = asdict(cfg)
    merged.update(overrides)
    return Config(**_coerce(merged))


def mark_onboarded() -> None:
    """Convenience for the wizard exit path."""
    cfg = load_global()
    cfg.onboarded = True
    save_global(cfg)
