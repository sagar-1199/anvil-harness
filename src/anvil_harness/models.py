from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


Role = Literal["user", "assistant", "system", "tool"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Project:
    name: str
    path: str
    added_at: str = field(default_factory=now_iso)
    model: str = "claude-opus-4-7"
    # Which agent backend new conversations in this project default to.
    # Conversations can override (see Conversation.agent).
    default_agent: str = "claude"

    @property
    def working_dir(self) -> Path:
        return Path(self.path).expanduser()

    @property
    def harness_dir(self) -> Path:
        return self.working_dir / ".harness"

    @property
    def conversations_dir(self) -> Path:
        return self.harness_dir / "conversations"

    @property
    def memory_path(self) -> Path:
        return self.harness_dir / "memory.md"


@dataclass
class Conversation:
    id: str
    title: str
    created_at: str = field(default_factory=now_iso)
    last_used_at: str = field(default_factory=now_iso)
    attached: list[str] = field(default_factory=list)
    # Which engine backend serves this conversation. Defaults to claude;
    # override per-conversation if you want a different agent.
    agent: str = "claude"

    def dir(self, project: Project) -> Path:
        return project.conversations_dir / self.id

    def messages_path(self, project: Project) -> Path:
        return self.dir(project) / "messages.jsonl"

    def meta_path(self, project: Project) -> Path:
        return self.dir(project) / "meta.json"

    def summary_path(self, project: Project) -> Path:
        return self.dir(project) / "summary.md"


@dataclass
class Message:
    role: Role
    content: str
    ts: str = field(default_factory=now_iso)
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content", ""),
            ts=d.get("ts", now_iso()),
            tool_name=d.get("tool_name"),
            tool_input=d.get("tool_input"),
            tool_result=d.get("tool_result"),
        )
