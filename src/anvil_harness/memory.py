"""Auto-summarization pipeline.

After each completed assistant turn, the app fires `update_memory(project, convo)`.
That runs Claude one-shot to summarize the current conversation into ~5 bullet
points and writes:
    <project>/.harness/conversations/<id>/summary.md       # per-convo summary
    <project>/.harness/memory.md                            # joined: all summaries

`memory.md` is what new conversations in this project auto-load as system-prompt
context, so opening a fresh chat in the same project carries forward what was
discussed earlier.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import storage
from .models import Conversation, Project


# Throttle: don't re-summarize the same conversation more often than this.
# Memory updates fire after every assistant turn — without throttling that
# means a Claude call per turn just for summarization. 60s + 4-msg window
# keeps the cost bounded without losing significant context across turns.
_MIN_SECONDS_BETWEEN_SUMMARIES = 60.0
_MIN_NEW_MESSAGES_BETWEEN_SUMMARIES = 4
# convo_id -> (last_run_monotonic, last_message_count)
_LAST_RUN: dict[str, tuple[float, int]] = {}


SUMMARY_PROMPT = """Summarize the conversation transcript below into:
  - 1 short title line (max 8 words)
  - 3-6 bullet points covering: the user's goal, what was decided, what was done
    (files touched, commands run), and any open follow-ups

Be terse. Skip pleasantries. Output plain text only, no preamble.

Transcript:
"""


def _format_transcript(convo: Conversation, project: Project, max_chars: int = 8000) -> str:
    lines = []
    for m in storage.load_messages(project, convo):
        snippet = (m.content or "").strip()
        if len(snippet) > 1200:
            snippet = snippet[:1200] + "…"
        lines.append(f"[{m.role}] {snippet}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        # keep the head and tail; drop the middle
        head = text[: max_chars // 2]
        tail = text[-max_chars // 2 :]
        text = head + "\n\n[… middle elided …]\n\n" + tail
    return text


async def _summarize(prompt: str, model: str | None) -> str:
    """One-shot summarization via the SDK's stateless query()."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = ClaudeAgentOptions(
        model=model or None,
        permission_mode="bypassPermissions",
        # no tools needed — pure text completion
        allowed_tools=[],
        # subscription OAuth only — never the API key
        env={"ANTHROPIC_API_KEY": ""},
    )
    parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
        if isinstance(msg, ResultMessage):
            break
    return "".join(parts).strip()


def _should_run(convo: Conversation, current_msg_count: int) -> bool:
    """Throttle: skip if we ran very recently AND not enough new messages."""
    last = _LAST_RUN.get(convo.id)
    if last is None:
        return current_msg_count >= 2  # don't summarize a single-message convo
    last_t, last_count = last
    if (time.monotonic() - last_t) < _MIN_SECONDS_BETWEEN_SUMMARIES and (
        current_msg_count - last_count
    ) < _MIN_NEW_MESSAGES_BETWEEN_SUMMARIES:
        return False
    return True


async def update_memory(project: Project, convo: Conversation, force: bool = False) -> None:
    """Regenerate this convo's summary.md AND rebuild the project's memory.md.

    Throttled by default — call with force=True (e.g. on app shutdown or
    explicit user request) to skip the throttle.
    """
    msg_count = sum(1 for _ in storage.load_messages(project, convo))
    if not force and not _should_run(convo, msg_count):
        return

    transcript = _format_transcript(convo, project)
    if not transcript.strip():
        return

    prompt = SUMMARY_PROMPT + transcript
    try:
        summary = await _summarize(prompt, project.model)
    except Exception:  # noqa: BLE001
        # Don't crash the app if summarization fails — just skip this round
        return
    if not summary.strip():
        return

    # write per-convo summary
    convo.summary_path(project).write_text(summary + "\n")
    _LAST_RUN[convo.id] = (time.monotonic(), msg_count)

    # rebuild project-wide memory.md
    rebuild_project_memory(project)


def rebuild_project_memory(project: Project) -> None:
    """Concat every conversation summary into memory.md, newest first."""
    convos = storage.list_conversations(project)  # already sorted newest-first
    sections: list[str] = []
    for c in convos:
        summary_path = c.summary_path(project)
        if not summary_path.exists():
            continue
        body = summary_path.read_text().strip()
        if not body:
            continue
        sections.append(
            f"## {c.title or c.id}\n"
            f"_{c.id}_\n\n"
            f"{body}"
        )
    if not sections:
        text = ""
    else:
        header = (
            "# Project Memory\n"
            f"_Auto-generated from {len(sections)} conversation summaries. "
            f"Last updated: {datetime.utcnow().isoformat(timespec='seconds')}Z_\n\n"
        )
        text = header + "\n\n---\n\n".join(sections) + "\n"
    project.memory_path.write_text(text)
