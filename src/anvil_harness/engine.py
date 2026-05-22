"""Engines: pluggable agent backends.

The harness is agent-agnostic. Conversations carry an `agent` field
(see :class:`anvil_harness.models.Conversation`) and the app routes each
turn to whichever engine matches that name via the :data:`ENGINES` registry
below.

Built-in engines:
  - ``claude`` — :class:`ClaudeEngine` (claude-agent-sdk, OAuth subscription)
  - ``mock``   — :class:`MockEngine`   (offline echo, for tests / offline dev)
  - ``codex``  — :class:`CodexEngine`  (stub; raises a clear error until wired)

Adding a new engine:
  1. Implement the :class:`Engine` protocol below.
  2. Call :func:`register_engine` near the bottom of this module.
  3. Pick an icon in ``HarnessApp.AGENT_ICONS``.

The persistent transcript (``messages.jsonl``) and the project's ``memory.md``
are both plain text, so switching a conversation between agents mid-stream
just means the new engine reads the same context and continues.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from .models import Conversation, Message, Project


# ---------- event types yielded to the UI ----------

@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseStart:
    tool_id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolUseEnd:
    tool_id: str
    is_error: bool = False
    content: str = ""


@dataclass
class TurnComplete:
    message: Message
    cost_usd: float | None = None


EngineEvent = TextDelta | ToolUseStart | ToolUseEnd | TurnComplete


class Engine(Protocol):
    async def send(
        self,
        project: Project,
        convo: Conversation,
        user_text: str,
    ) -> AsyncIterator[EngineEvent]:
        ...

    async def close(self, convo_id: str) -> None:
        ...


# ---------- mock engine (Phase 1, kept for tests / offline ----------

class MockEngine:
    async def send(
        self,
        project: Project,
        convo: Conversation,
        user_text: str,
    ) -> AsyncIterator[EngineEvent]:
        reply = f"(mock) you said: {user_text}"
        buf = ""
        for word in reply.split(" "):
            chunk = word + " "
            buf += chunk
            await asyncio.sleep(0.04)
            yield TextDelta(text=chunk)
        yield TurnComplete(message=Message(role="assistant", content=buf.rstrip()))

    async def close(self, convo_id: str) -> None:
        return None


# ---------- real Claude engine ----------

BASE_SYSTEM_PROMPT = """You are Claude, the user's coding assistant inside a custom local harness.
You're currently working in a specific project. Use the file/bash tools you have
to read, edit, and run code in this project. Be concise and direct.

When the user asks an open-ended question, suggest 1-2 concrete next steps
rather than long explanations. Reach for tools rather than describing what you
would do — actually do it.
"""


def _build_system_prompt(project: Project, prior_messages: list[Message]) -> str:
    parts = [BASE_SYSTEM_PROMPT.strip()]
    parts.append(f"Project working directory: {project.working_dir}")

    memory_text = ""
    if project.memory_path.exists():
        memory_text = project.memory_path.read_text().strip()
    if memory_text:
        parts.append("---")
        parts.append("PROJECT MEMORY (auto-generated summaries from prior conversations in this project — load this as context):")
        parts.append(memory_text)

    if prior_messages:
        parts.append("---")
        parts.append(
            f"This conversation already has {len(prior_messages)} prior messages "
            "(from a previous session). Recent excerpt:"
        )
        for m in prior_messages[-12:]:
            snippet = (m.content or "").strip()
            if len(snippet) > 800:
                snippet = snippet[:800] + "…"
            parts.append(f"[{m.role}] {snippet}")

    return "\n\n".join(parts)


class ClaudeEngine:
    """Persistent ClaudeSDKClient per conversation, lifetime = tab lifetime."""

    def __init__(self) -> None:
        # convo_id -> client
        self._clients: dict[str, Any] = {}

    async def _ensure(self, project: Project, convo: Conversation) -> Any:
        if convo.id in self._clients:
            return self._clients[convo.id]
        # lazy import to keep startup cheap if user only uses MockEngine
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        from . import storage
        # Tail-read instead of slurping the whole JSONL — we only use last 12 msgs.
        prior = storage.tail_messages(project, convo, n=14)
        # don't double-count the just-appended user message
        if prior and prior[-1].role == "user":
            prior_for_prompt = prior[:-1]
        else:
            prior_for_prompt = prior

        system_prompt = _build_system_prompt(project, prior_for_prompt)

        # Force the spawned `claude` to use the user's subscription OAuth, not the
        # API. We blank out ANTHROPIC_API_KEY in the child env so even if the user
        # later sets one for some other tool, this app stays on the subscription.
        options = ClaudeAgentOptions(
            cwd=str(project.working_dir),
            system_prompt=system_prompt,
            model=project.model or None,
            permission_mode="bypassPermissions",  # personal harness — auto-allow tools
            include_partial_messages=True,
            setting_sources=["project"],  # pick up any .claude/settings.json in the project
            env={"ANTHROPIC_API_KEY": ""},
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        self._clients[convo.id] = client
        return client

    async def send(
        self,
        project: Project,
        convo: Conversation,
        user_text: str,
    ) -> AsyncIterator[EngineEvent]:
        client = await self._ensure(project, convo)
        # lazy import types for isinstance checks
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        await client.query(user_text)
        full_text = ""
        cost = None

        async for msg in client.receive_response():
            if isinstance(msg, StreamEvent):
                # partial streaming events for text/thinking
                ev = getattr(msg, "event", None) or {}
                etype = ev.get("type")
                if etype == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        chunk = delta.get("text", "")
                        if chunk:
                            full_text += chunk
                            yield TextDelta(text=chunk)
                continue

            if isinstance(msg, AssistantMessage):
                # AssistantMessage arrives at the end of a content block. Tool uses
                # surface here; text blocks were already streamed above.
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        yield ToolUseStart(
                            tool_id=block.id,
                            name=block.name,
                            input=block.input or {},
                        )
                continue

            if isinstance(msg, UserMessage):
                # Tool *results* come back as a synthetic UserMessage from the SDK
                # (Claude → tool → SDK injects result as user content). This is
                # how we know a tool finished, so the inline "🔧 …" line gets a ✓.
                content = getattr(msg, "content", None) or []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            yield ToolUseEnd(
                                tool_id=getattr(block, "tool_use_id", ""),
                                is_error=bool(getattr(block, "is_error", False)),
                                content=str(getattr(block, "content", "")),
                            )
                continue

            if isinstance(msg, ResultMessage):
                cost = getattr(msg, "total_cost_usd", None) or getattr(msg, "cost_usd", None)
                # don't break — let the SDK iterator drain naturally so aclose()
                # doesn't race with an in-flight yield on shutdown.

        yield TurnComplete(
            message=Message(role="assistant", content=full_text.strip()),
            cost_usd=cost,
        )

    async def close(self, convo_id: str) -> None:
        client = self._clients.pop(convo_id, None)
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def close_all(self) -> None:
        for cid in list(self._clients.keys()):
            await self.close(cid)


# ---------- Codex engine ----------

class CodexEngine:
    """OpenAI Codex CLI bridge.

    Codex CLI exposes `-q --full-auto` for non-interactive completion. Each
    turn is a fresh subprocess — Codex doesn't expose a persistent client
    library, so we rebuild context per turn from the project's stored history.

    Limitations vs ClaudeEngine:
      - No token-level streaming (we stream stdout line-by-line as TextDelta,
        which the user sees as "chunks land as soon as Codex writes them").
      - Tool calls happen inside the Codex process and aren't surfaced as
        ToolUseStart/ToolUseEnd events — they show up as text in the output.
      - No cost reporting (Codex CLI doesn't print one).

    Auth: relies on `codex login` having been run by the user. If auth is
    missing, Codex writes a clear error to stderr which we surface verbatim.
    """

    CLI_NAME = "codex"

    async def send(
        self,
        project: Project,
        convo: Conversation,
        user_text: str,
    ) -> AsyncIterator[EngineEvent]:
        import shutil
        from . import storage

        cli_path = shutil.which(self.CLI_NAME)
        if cli_path is None:
            msg = (
                "Codex CLI isn't installed.\n\n"
                "Install it with: npm install -g @openai/codex\n"
                "Then run `codex login` once to authenticate.\n\n"
                "You can switch this conversation's agent via Ctrl+Shift+M."
            )
            yield TurnComplete(message=Message(role="assistant", content=msg))
            return

        # Rebuild context per turn — Codex CLI is stateless per invocation.
        prior = storage.tail_messages(project, convo, n=14)
        if prior and prior[-1].role == "user":
            prior = prior[:-1]  # don't double-count the just-appended turn
        prompt = self._compose_prompt(project, prior, user_text)

        proc = await asyncio.create_subprocess_exec(
            cli_path, "-q", "--full-auto", prompt,
            cwd=str(project.working_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        full_text = ""
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(1024)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            full_text += text
            yield TextDelta(text=text)

        # Drain stderr so the OS pipe doesn't fill and block proc.wait().
        err_bytes = await proc.stderr.read() if proc.stderr else b""
        await proc.wait()

        if proc.returncode != 0 and not full_text.strip():
            err = err_bytes.decode("utf-8", errors="replace").strip()
            msg = f"Codex exited with code {proc.returncode}.\n\n{err or '(no error output)'}"
            yield TurnComplete(message=Message(role="assistant", content=msg))
            return

        yield TurnComplete(message=Message(role="assistant", content=full_text.strip()))

    async def close(self, convo_id: str) -> None:
        # Stateless — each send() is its own subprocess. Nothing to clean up.
        return None

    def _compose_prompt(
        self, project: Project, prior: list[Message], user_text: str
    ) -> str:
        """Stitch prior turns + project memory + the new user message into a
        single prompt string. Codex is stateless across invocations, so this
        is the only context it sees."""
        parts: list[str] = []
        parts.append(
            f"You are working inside the project at: {project.working_dir}"
        )
        memory_text = ""
        if project.memory_path.exists():
            memory_text = project.memory_path.read_text().strip()
        if memory_text:
            parts.append("PROJECT MEMORY (carry forward):\n" + memory_text)
        if prior:
            convo_lines = []
            for m in prior[-8:]:
                snippet = (m.content or "").strip()
                if len(snippet) > 800:
                    snippet = snippet[:800] + "…"
                convo_lines.append(f"[{m.role}] {snippet}")
            parts.append("RECENT CONVERSATION:\n" + "\n".join(convo_lines))
        parts.append("USER:\n" + user_text)
        return "\n\n---\n\n".join(parts)


# ---------- registry ----------

ENGINES: dict[str, type[Engine]] = {}


def register_engine(name: str, cls: type[Engine]) -> None:
    """Make an engine selectable from `Conversation.agent`."""
    ENGINES[name] = cls


def get_engine_class(name: str) -> type[Engine]:
    cls = ENGINES.get(name)
    if cls is None:
        # Don't crash an existing conversation if its `agent` value is
        # unknown — fall back to claude and let the UI flag it.
        cls = ENGINES.get("claude", ClaudeEngine)
    return cls


def available_agents() -> list[str]:
    return list(ENGINES.keys())


# Built-ins.
register_engine("claude", ClaudeEngine)
register_engine("codex", CodexEngine)
register_engine("mock", MockEngine)
