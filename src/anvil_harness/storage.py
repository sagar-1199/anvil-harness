"""Filesystem persistence for projects, conversations, and messages.

Layout:
    ~/.anvil/projects.json                       # central registry
    ~/.anvil/config.toml                         # global user config (see config.py)
    <project>/.harness/project.json              # per-project settings
    <project>/.harness/memory.md                 # rolling summary (Phase 3)
    <project>/.harness/conversations/<id>/
        meta.json
        messages.jsonl                           # append-only, fsynced per line
        summary.md                               # written on convo end (Phase 3)

Migration: if ~/.claude-harness/projects.json exists from the pre-rename
install, it's copied to ~/.anvil/projects.json on first load. Original is
left in place so the user can roll back.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import Conversation, Message, Project, now_iso


HARNESS_HOME = Path.home() / ".anvil"
REGISTRY_PATH = HARNESS_HOME / "projects.json"
LEGACY_HOME = Path.home() / ".claude-harness"  # pre-rename location

# Tiny mtime-keyed caches. Two different harness windows share the same files,
# so we invalidate on every write and on disk-mtime change.
_REGISTRY_CACHE: tuple[float, list[Project]] | None = None
_CONVOS_CACHE: dict[str, tuple[float, list[Conversation]]] = {}


def _ensure_home() -> None:
    HARNESS_HOME.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        legacy = LEGACY_HOME / "projects.json"
        if legacy.exists():
            shutil.copy2(legacy, REGISTRY_PATH)
        else:
            REGISTRY_PATH.write_text(json.dumps({"projects": []}, indent=2))


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s or "untitled"


# ---------- registry ----------

def load_registry() -> list[Project]:
    """Return the projects list, cached by registry mtime."""
    global _REGISTRY_CACHE
    _ensure_home()
    try:
        mtime = REGISTRY_PATH.stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0
    if _REGISTRY_CACHE and _REGISTRY_CACHE[0] == mtime:
        return list(_REGISTRY_CACHE[1])
    raw = json.loads(REGISTRY_PATH.read_text())
    projects = [Project(**p) for p in raw.get("projects", [])]
    _REGISTRY_CACHE = (mtime, projects)
    return list(projects)


def save_registry(projects: list[Project]) -> None:
    global _REGISTRY_CACHE
    _ensure_home()
    REGISTRY_PATH.write_text(
        json.dumps({"projects": [asdict(p) for p in projects]}, indent=2)
    )
    _REGISTRY_CACHE = None  # force re-read on next load


def add_project(
    name: str,
    path: str,
    model: str = "claude-opus-4-7",
    default_agent: str = "claude",
) -> Project:
    projects = load_registry()
    if any(p.name == name for p in projects):
        raise ValueError(f"Project '{name}' already exists in registry.")
    project = Project(
        name=name,
        path=str(Path(path).expanduser().resolve()),
        model=model,
        default_agent=default_agent,
    )
    project.working_dir.mkdir(parents=True, exist_ok=True)
    init_project_dirs(project)
    projects.append(project)
    save_registry(projects)
    return project


def remove_project(name: str) -> None:
    projects = [p for p in load_registry() if p.name != name]
    save_registry(projects)


def get_project(name: str) -> Project | None:
    for p in load_registry():
        if p.name == name:
            return p
    return None


# ---------- per-project ----------

def init_project_dirs(project: Project) -> None:
    project.harness_dir.mkdir(parents=True, exist_ok=True)
    project.conversations_dir.mkdir(parents=True, exist_ok=True)
    settings_path = project.harness_dir / "project.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps(asdict(project), indent=2))
    if not project.memory_path.exists():
        project.memory_path.write_text("")


def list_conversations(project: Project) -> list[Conversation]:
    """Cached by the conversations dir's mtime — bumps when a convo is created
    or a meta is rewritten (append_message → save_conversation_meta)."""
    key = str(project.conversations_dir)
    if not project.conversations_dir.exists():
        _CONVOS_CACHE.pop(key, None)
        return []
    try:
        mtime = project.conversations_dir.stat().st_mtime
    except FileNotFoundError:
        return []
    cached = _CONVOS_CACHE.get(key)
    if cached and cached[0] == mtime:
        return list(cached[1])

    convos: list[Conversation] = []
    for d in project.conversations_dir.iterdir():
        if not d.is_dir():
            continue
        meta = d / "meta.json"
        if not meta.exists():
            continue
        try:
            data = json.loads(meta.read_text())
        except json.JSONDecodeError:
            continue
        convos.append(Conversation(**data))
    convos.sort(key=lambda c: c.last_used_at, reverse=True)
    _CONVOS_CACHE[key] = (mtime, convos)
    return list(convos)


def _invalidate_convos_cache(project: Project) -> None:
    _CONVOS_CACHE.pop(str(project.conversations_dir), None)


def create_conversation(project: Project, title: str | None = None) -> Conversation:
    title = title or "untitled"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base_id = f"{stamp}_{_slug(title)}"
    convo_id = base_id
    # Avoid collisions when two convos are created in the same second with the same title
    i = 2
    while (project.conversations_dir / convo_id).exists():
        convo_id = f"{base_id}-{i}"
        i += 1
    convo = Conversation(id=convo_id, title=title)
    convo.dir(project).mkdir(parents=True, exist_ok=True)
    convo.meta_path(project).write_text(json.dumps(asdict(convo), indent=2))
    convo.messages_path(project).touch()
    _invalidate_convos_cache(project)
    return convo


def save_conversation_meta(project: Project, convo: Conversation) -> None:
    convo.meta_path(project).write_text(json.dumps(asdict(convo), indent=2))
    _invalidate_convos_cache(project)


def append_message(project: Project, convo: Conversation, msg: Message) -> None:
    """Append one message, fsync, and bump last_used_at."""
    path = convo.messages_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    convo.last_used_at = now_iso()
    save_conversation_meta(project, convo)


def load_messages(project: Project, convo: Conversation) -> Iterator[Message]:
    path = convo.messages_path(project)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield Message.from_dict(json.loads(line))
            except json.JSONDecodeError:
                continue


def count_messages(project: Project, convo: Conversation) -> int:
    """Cheap line count — doesn't parse JSON."""
    path = convo.messages_path(project)
    if not path.exists():
        return 0
    n = 0
    with path.open("rb") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def tail_messages(project: Project, convo: Conversation, n: int) -> list[Message]:
    """Return the last `n` messages efficiently. For small files (<256KB) just
    reads the whole thing; for big files, seeks backwards in 16KB chunks until
    we have at least `n` lines."""
    path = convo.messages_path(project)
    if not path.exists():
        return []
    size = path.stat().st_size
    if size <= 256 * 1024:
        msgs = list(load_messages(project, convo))
        return msgs[-n:]

    # tail-read in chunks
    chunk = 16 * 1024
    pos = size
    buf = b""
    lines: list[bytes] = []
    with path.open("rb") as f:
        while pos > 0 and len(lines) <= n + 2:
            read = min(chunk, pos)
            pos -= read
            f.seek(pos)
            buf = f.read(read) + buf
            lines = buf.split(b"\n")
    msgs: list[Message] = []
    for raw in lines[-(n + 1):]:
        s = raw.strip()
        if not s:
            continue
        try:
            msgs.append(Message.from_dict(json.loads(s)))
        except json.JSONDecodeError:
            continue
    return msgs[-n:]


def delete_project(name: str, also_delete_data: bool = False) -> None:
    """Remove from registry. Optionally delete the .harness/ folder too."""
    import shutil
    projects = load_registry()
    target = next((p for p in projects if p.name == name), None)
    if target is None:
        return
    projects = [p for p in projects if p.name != name]
    save_registry(projects)
    if also_delete_data and target.harness_dir.exists():
        shutil.rmtree(target.harness_dir, ignore_errors=True)


def delete_conversation(project: Project, convo: Conversation) -> None:
    import shutil
    d = convo.dir(project)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    _invalidate_convos_cache(project)
