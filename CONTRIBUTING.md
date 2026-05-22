# Contributing to Anvil

Thanks for being here. This guide focuses on the one thing most contributors
ask about first: **how do I add a new agent backend?**

## Add a new engine

Anvil's engine layer is intentionally tiny. To wire up a new agent (Gemini,
Cursor Agent, aider, your own LLM):

### 1. Implement the `Engine` protocol

In `src/anvil_harness/engine.py`, add a class that yields `EngineEvent`s:

```python
class MyAgentEngine:
    async def send(
        self,
        project: Project,
        convo: Conversation,
        user_text: str,
    ) -> AsyncIterator[EngineEvent]:
        # 1. Build context (read project.memory_path, tail prior messages,
        #    whatever your agent needs).
        # 2. Invoke your agent. As text arrives, yield TextDelta(text=...).
        # 3. As tools start/end, yield ToolUseStart / ToolUseEnd.
        # 4. When done, yield TurnComplete(message=Message(...), cost_usd=...).
        ...

    async def close(self, convo_id: str) -> None:
        # Clean up any per-conversation state (sockets, processes, …).
        ...
```

The events you can yield:

- `TextDelta(text)` — partial assistant text. Yield as often as you like;
  the UI renders incrementally.
- `ToolUseStart(tool_id, name, input)` — model started using a tool.
- `ToolUseEnd(tool_id, is_error, content)` — tool finished.
- `TurnComplete(message, cost_usd)` — the turn is done. `message` is the
  full assistant message persisted to the JSONL transcript.

### 2. Register it

At the bottom of `engine.py`:

```python
register_engine("myagent", MyAgentEngine)
```

### 3. Make it discoverable

In `src/anvil_harness/detect.py`, add an entry to `KNOWN_AGENTS`:

```python
"myagent": {
    "label": "My Agent",
    "cli": "myagent-cli",       # binary name to look up via shutil.which
    "install_hint": "npm install -g @example/myagent",
    "install_url": "https://example.com/install",
    "auth_hint": "Run `myagent-cli login`.",
},
```

If your agent uses a custom auth check, add a branch to `_auth_ok()` in
the same file.

### 4. (Optional) Pick an icon

In `src/anvil_harness/app.py`, extend `HarnessApp.AGENT_ICONS`:

```python
AGENT_ICONS = {
    "claude": "🤖",
    "codex":  "🦾",
    "mock":   "🧪",
    "myagent": "🌟",
}
```

That's it. The Welcome wizard and Settings screen pick up the new agent
automatically from `KNOWN_AGENTS`.

## Style

- Keep comments tight — Anvil's repo follows a "write code, not commentary"
  rule. Only add a comment when the *why* isn't obvious from the code.
- No new dependencies unless you can argue the case in the PR description.
  The current dep list is short on purpose; every dep adds install friction
  for non-technical users.
- No telemetry, ever, unless it's behind `Config.telemetry_opt_in` and
  defaults off.

## Running locally

```bash
git clone https://github.com/sagar-1199/anvil-harness
cd anvil-harness
uv sync
uv run anvil
```

To use the mock engine (offline echo, useful for UI work without burning
tokens), set `USE_MOCK_ENGINE = True` in `app.py`. Don't commit that.

## Reporting bugs

Include:

- macOS version and Terminal app (Terminal.app, iTerm2, …)
- Output of `anvil --version` and `which anvil`
- The relevant section of `~/.anvil/config.toml`
- Steps to reproduce
- Screenshot of the TUI if visual

## Community standards

This project follows the [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
Be kind, assume good faith, focus on the work. Report problems by opening a
private issue or emailing the maintainer (see `pyproject.toml`).

## License

By contributing, you agree your contributions are licensed under MIT.
