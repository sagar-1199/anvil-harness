"""Textual TUI for anvil-harness.

Layout:
    ┌──────────────────────┬──────────────────────────────────┐
    │ PROJECTS             │  [chat 1][chat 2][+]  (tabs)     │
    │  • my-website        ├──────────────────────────────────┤
    │  • side-project      │  (active chat)                   │
    │  [+ Open project]    │   user: ...                      │
    │                      │   assistant: ...                 │
    │ FILES                │   🔧 Read foo.py                 │
    │  ├ src/              │                                  │
    │  │ ├ app.py          │                                  │
    │  └ pyproject.toml    │                                  │
    │                      │                                  │
    │ CONVERSATIONS        │                                  │
    │  ● first pass        │                                  │
    │    bug fix           ├──────────────────────────────────┤
    │  [+ New chat]        │  > type message…                 │
    └──────────────────────┴──────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text as RichText
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from . import config, detect, memory, storage
from .engine import (
    ClaudeEngine,
    Engine,
    MockEngine,
    TextDelta,
    ToolUseEnd,
    ToolUseStart,
    TurnComplete,
    available_agents,
    get_engine_class,
)
from .models import Conversation, Message, Project


USE_MOCK_ENGINE = False  # flip to True to use the offline mock


# ---------- modals ----------

class AgentPickerModal(ModalScreen[str | None]):
    """List available agents; user picks one. Returns its name or None."""

    DEFAULT_CSS = """
    AgentPickerModal { align: center middle; }
    AgentPickerModal > Vertical {
        background: $panel; padding: 1 2; width: 50; height: auto;
        border: thick $primary;
    }
    AgentPickerModal ListView { height: auto; max-height: 12; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, agents: list[str], current: str) -> None:
        super().__init__()
        self._agents = agents
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Switch agent[/]")
            yield Label(f"[dim]current: {self._current}[/]")
            yield ListView(
                *[
                    ListItem(Label(f"{'● ' if a == self._current else '  '}{a}"))
                    for a in self._agents
                ],
                id="agent-list",
            )
            yield Label("[dim]Enter to select · Esc to cancel[/]")

    def on_mount(self) -> None:
        lv = self.query_one("#agent-list", ListView)
        # pre-select the current agent
        try:
            lv.index = self._agents.index(self._current)
        except ValueError:
            lv.index = 0
        lv.focus()

    @on(ListView.Selected, "#agent-list")
    def _picked(self, event: ListView.Selected) -> None:
        lv = self.query_one("#agent-list", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._agents)):
            self.dismiss(None)
            return
        self.dismiss(self._agents[idx])


class WelcomeWizard(ModalScreen[str | None]):
    """First-launch onboarding. Detects installed agent CLIs, lets the user
    pick a default, and writes the choice to ~/.anvil/config.toml.

    Returns the chosen agent name on confirm, or None if the user dismisses
    (we treat dismiss as "ok, I'll use claude" — the safest default — so the
    app is usable even if the user is in a hurry).
    """

    DEFAULT_CSS = """
    WelcomeWizard { align: center middle; }
    WelcomeWizard > Vertical {
        background: $panel; padding: 1 2; width: 80; height: auto;
        border: thick $primary;
    }
    WelcomeWizard ListView { height: auto; max-height: 16; margin: 1 0; }
    WelcomeWizard ListItem { padding: 0 1; }
    WelcomeWizard .wiz-title { text-style: bold; color: $accent; }
    WelcomeWizard .wiz-sub { color: $text-muted; margin-bottom: 1; }
    WelcomeWizard .wiz-foot { color: $text-muted; margin-top: 1; }
    WelcomeWizard Button { width: auto; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Skip")]

    def __init__(self, agents: list[detect.AgentInfo]) -> None:
        super().__init__()
        self._agents = agents
        # Pre-select the first installed agent (or first overall if none).
        self._initial_index = next(
            (i for i, a in enumerate(agents) if a.installed and a.name != "mock"),
            0,
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Welcome to Anvil 🔨", classes="wiz-title")
            yield Label(
                "Pick which coding agent to use as your default. You can "
                "switch any time via Settings (Ctrl+,).",
                classes="wiz-sub",
            )
            yield ListView(
                *[ListItem(Label(self._row(a))) for a in self._agents],
                id="wiz-list",
            )
            yield Label(self._footer(), classes="wiz-foot", id="wiz-hint")
            yield Button("Use this agent", id="wiz-confirm", variant="primary")

    def _row(self, a: detect.AgentInfo) -> str:
        if not a.installed:
            return f"[dim]✗ {a.label} — not installed[/]"
        if a.auth_ok is True:
            status = "[green]✓ installed + logged in[/]"
        elif a.auth_ok is False:
            status = "[yellow]⚠ installed, not logged in[/]"
        else:
            status = "[green]✓ installed[/] [dim](auth status unknown)[/]"
        version = f" [dim]({a.version})[/]" if a.version else ""
        return f"● {a.label}{version}  {status}"

    def _footer(self) -> str:
        return (
            "[dim]↑/↓ select · Enter or click Use · Esc to skip (defaults to claude)[/]"
        )

    def on_mount(self) -> None:
        lv = self.query_one("#wiz-list", ListView)
        lv.index = self._initial_index
        lv.focus()
        self._refresh_hint()

    def _selected(self) -> detect.AgentInfo | None:
        lv = self.query_one("#wiz-list", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._agents)):
            return None
        return self._agents[idx]

    @on(ListView.Highlighted, "#wiz-list")
    def _on_highlight(self, event: ListView.Highlighted) -> None:
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        a = self._selected()
        hint = self.query_one("#wiz-hint", Label)
        if a is None:
            hint.update(self._footer())
            return
        if not a.installed and a.install_hint:
            hint.update(
                f"[yellow]Not installed.[/] Run: [bold]{a.install_hint}[/]  ·  "
                f"docs: {a.install_url}"
            )
        elif a.installed and a.auth_ok is False and a.auth_hint:
            hint.update(f"[yellow]Needs login.[/] {a.auth_hint}")
        else:
            hint.update(self._footer())

    @on(ListView.Selected, "#wiz-list")
    def _on_select(self, event: ListView.Selected) -> None:
        self._confirm()

    @on(Button.Pressed, "#wiz-confirm")
    def _on_confirm(self, event: Button.Pressed) -> None:
        self._confirm()

    def _confirm(self) -> None:
        a = self._selected()
        if a is None:
            self.dismiss(None)
            return
        if not a.installed:
            self.app.notify(
                f"{a.label} isn't installed yet. Install it, then re-run the wizard "
                "from Settings.",
                severity="warning",
            )
            return
        self.dismiss(a.name)


class SettingsScreen(ModalScreen[None]):
    """In-app settings — replaces config-file editing for non-technical users.

    Sections (collapsed into one screen, since today there are few knobs):
      • Default agent (re-uses the wizard's detection logic so install /
        login status is visible)
      • Default model (passes to ClaudeEngine; empty = engine default)
      • About: version + path to ~/.anvil/config.toml so power users can
        still edit by hand if they want

    Writes through to ~/.anvil/config.toml on every change. No save button.
    """

    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    SettingsScreen > Vertical {
        background: $panel; padding: 1 2; width: 84; height: auto; max-height: 90%;
        border: thick $primary;
    }
    SettingsScreen .sec { color: $accent; text-style: bold; margin-top: 1; }
    SettingsScreen .sec-sub { color: $text-muted; margin-bottom: 1; }
    SettingsScreen ListView { height: auto; max-height: 10; }
    SettingsScreen ListItem { padding: 0 1; }
    SettingsScreen Input { margin-top: 1; }
    SettingsScreen Button { width: auto; margin-top: 1; }
    SettingsScreen .foot { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("ctrl+w", "dismiss", "Close", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._agents = detect.detect_all()
        self._cfg = config.load_global()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Settings", classes="sec")

            yield Label("Default agent", classes="sec")
            yield Label(
                "New conversations in new projects use this agent. Existing "
                "conversations are unaffected.",
                classes="sec-sub",
            )
            yield ListView(
                *[ListItem(Label(self._row(a))) for a in self._agents],
                id="settings-agent-list",
            )

            yield Label("Default model", classes="sec")
            yield Label(
                "Passed to Claude Code (ignored by Codex). Leave blank to use "
                "the engine's default. Example: claude-opus-4-7 or claude-sonnet-4-6.",
                classes="sec-sub",
            )
            yield Input(
                value=self._cfg.default_model,
                placeholder="(engine default)",
                id="settings-model",
            )

            yield Label("About", classes="sec")
            yield Label(self._about_text(), classes="sec-sub", id="settings-about")

            yield Label(
                "[dim]Changes save automatically · Esc to close[/]",
                classes="foot",
            )

    def _row(self, a: detect.AgentInfo) -> str:
        marker = "●" if a.name == self._cfg.default_agent else "○"
        if not a.installed:
            return f"[dim]{marker} {a.label} — not installed[/]"
        if a.auth_ok is True:
            tag = "[green]✓[/]"
        elif a.auth_ok is False:
            tag = "[yellow]⚠ login needed[/]"
        else:
            tag = "[dim](auth unknown)[/]"
        return f"{marker} {a.label} {tag}"

    def _about_text(self) -> str:
        try:
            from importlib.metadata import version
            v = version("anvil-harness")
        except Exception:  # noqa: BLE001
            v = "(dev)"
        return (
            f"Anvil {v}\n"
            f"Config: {config.GLOBAL_CONFIG_PATH}\n"
            f"Data:   {storage.HARNESS_HOME}"
        )

    def on_mount(self) -> None:
        lv = self.query_one("#settings-agent-list", ListView)
        try:
            lv.index = next(
                i for i, a in enumerate(self._agents) if a.name == self._cfg.default_agent
            )
        except StopIteration:
            lv.index = 0

    @on(ListView.Selected, "#settings-agent-list")
    def _agent_picked(self, event: ListView.Selected) -> None:
        lv = self.query_one("#settings-agent-list", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._agents)):
            return
        a = self._agents[idx]
        if not a.installed:
            self.app.notify(
                f"{a.label} isn't installed. Run: {a.install_hint}",
                severity="warning",
            )
            return
        self._cfg.default_agent = a.name
        config.save_global(self._cfg)
        # Rerender the list so the ● marker moves.
        for i, item in enumerate(lv.children):
            label = item.query_one(Label)
            label.update(self._row(self._agents[i]))
        self.app.notify(f"Default agent → {a.label}")

    @on(Input.Submitted, "#settings-model")
    def _model_committed(self, event: Input.Submitted) -> None:
        self._cfg.default_model = (event.value or "").strip()
        config.save_global(self._cfg)
        self.app.notify(
            f"Default model → '{self._cfg.default_model or '(engine default)'}'"
        )


class SimpleTextModal(ModalScreen[str | None]):
    """One-line input modal. Returns string on submit, None on Esc."""

    DEFAULT_CSS = """
    SimpleTextModal { align: center middle; }
    SimpleTextModal > Vertical {
        background: $panel; padding: 1 2; width: 60; height: auto;
        border: thick $primary;
    }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, label: str, default: str = "") -> None:
        super().__init__()
        self._label = label
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._label)
            yield Input(value=self._default, id="modal-input")
            yield Label("[dim]Enter to submit · Esc to cancel[/]")

    def on_mount(self) -> None:
        self.query_one("#modal-input", Input).focus()

    @on(Input.Submitted, "#modal-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


# ---------- helpers ----------

def _safe_id(text: str) -> str:
    """Make a string safe to use as a Textual widget id."""
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        else:
            out.append("-")
    s = "".join(out).strip("-")
    return s or "x"


def _project_name_from_path(path: str) -> str:
    base = Path(path).name or "project"
    return _safe_id(base.lower().replace("_", "-"))


def _short(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


class FilteredDirectoryTree(DirectoryTree):
    """DirectoryTree that hides our own metadata + common heavy/noisy folders
    so the sidebar shows what the user actually wants to browse."""

    EXCLUDE_NAMES = {
        ".harness", ".git", "node_modules", ".venv", "venv", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt",
        "dist", "build", ".turbo", ".cache", ".DS_Store",
    }

    def filter_paths(self, paths):  # type: ignore[override]
        return [p for p in paths if p.name not in self.EXCLUDE_NAMES]


class ChatInput(Input):
    """Input with Mac-friendly editing keybindings.

    Terminal.app sends Option+<key> as an Esc-prefixed sequence, which
    Textual surfaces as ``alt+<key>``. Stock Input only binds the ctrl+
    equivalents, so we add the alt+ ones for native macOS muscle memory.
    """

    BINDINGS = [
        # Option+Backspace → delete word to the left
        Binding("alt+backspace", "delete_left_word", show=False),
        # Option+Left/Right → jump by word
        Binding("alt+left", "cursor_left_word", show=False),
        Binding("alt+right", "cursor_right_word", show=False),
        # Option+Delete (forward) → delete word to the right
        Binding("alt+delete", "delete_right_word", show=False),
        # Emacs/readline standards (work in any terminal)
        Binding("ctrl+u", "delete_left_all", show=False),
        Binding("ctrl+k", "delete_right_all", show=False),
        # Copy the input box's current value to the OS clipboard
        Binding("ctrl+shift+c", "copy_value", "Copy input"),
    ]

    def _on_paste(self, event: events.Paste) -> None:
        """Override Textual's single-line truncation.

        Stock Input._on_paste does `event.text.splitlines()[0]` — it inserts
        only the first line. Pasting a paragraph drops everything after the
        first newline (and users typically see only a few words land).

        We flatten newlines to spaces so the entire paste lands as one line.
        For image-only clipboards (no text in the Paste event), defer to the
        app's attach-image action — same flow as Ctrl+Shift+I.
        """
        text = event.text or ""
        if not text.strip():
            # Cmd+V on an image-only clipboard. Terminal.app may or may not
            # actually dispatch a Paste event in this case (depends on macOS
            # version); when it does, treat it as an attach request.
            action = getattr(self.app, "action_attach_image", None)
            if action is not None:
                action()
            event.stop()
            return
        flattened = " ".join(text.splitlines()).strip()
        selection = self.selection
        if selection.is_empty:
            self.insert_text_at_cursor(flattened)
        else:
            self.replace(flattened, *selection)
        event.stop()

    def action_copy_value(self) -> None:
        if not self.value:
            self.app.notify("Input is empty.", severity="warning")
            return
        try:
            # Textual ≥0.74 supports OSC52 clipboard push via the app
            self.app.copy_to_clipboard(self.value)
        except Exception:  # noqa: BLE001
            # Fallback: shell out to pbcopy on macOS
            import subprocess
            try:
                subprocess.run(
                    ["/usr/bin/pbcopy"],
                    input=self.value.encode("utf-8"),
                    check=True,
                    timeout=2,
                )
            except Exception as exc:  # noqa: BLE001
                self.app.notify(f"Copy failed: {exc}", severity="error")
                return
        self.app.notify(f"Copied {len(self.value)} chars to clipboard.")


class MessageWidget(Static):
    """Chat message bubble that copies its raw text on click.

    Mouse-reporting in Textual eats Terminal.app's drag-to-select, and the
    Anvil profile remap steals Cmd+C for the in-app copy action — together
    they make ad-hoc text selection painful. Per-message click-to-copy
    sidesteps both: one click on the bubble pushes the original (pre-render)
    text to the system clipboard via the app's `_push_to_clipboard`.
    """

    def __init__(self, *args, raw_text: str = "", role: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.raw_text = raw_text
        self.role = role

    def set_message(self, rendered, raw: str) -> None:
        """Update displayed renderable and stored raw text together."""
        self.raw_text = raw
        self.update(rendered)

    def on_click(self) -> None:
        text = self.raw_text or ""
        if not text.strip():
            return
        app = self.app
        push = getattr(app, "_push_to_clipboard", None)
        if push is None:
            return
        push(text)
        app.notify(f"Copied {len(text)} chars from message.")


# ---------- main app ----------

class HarnessApp(App):
    CSS = """
    Screen { background: $surface; }

    #sidebar { width: 42; border-right: solid $primary; padding: 0 1; }
    #sidebar Label.section { color: $accent; margin-top: 1; text-style: bold; }
    #project-list, #convo-list { height: auto; max-height: 10; background: $surface; }
    ListItem { padding: 0 1; }
    ListItem.--highlight { background: $primary 50%; }
    #file-tree-host { height: 1fr; min-height: 6; }
    #file-tree { background: $surface; }
    Button { width: 100%; margin-top: 1; }

    #main { width: 1fr; }
    #action-bar { height: 3; padding: 0 1; background: $surface; }
    #active-tab-hint { width: 1fr; padding: 1 1 0 1; color: $text-muted; }
    #btn-close-tab { width: 16; margin: 0 0 0 1; }
    #convo-tabs { height: 1fr; }
    TabPane { padding: 0; }
    .chat-pane { padding: 1 2; height: 1fr; }
    #input-row { height: 3; dock: bottom; }
    #message-input { width: 1fr; height: 3; }
    #btn-paste { width: 14; height: 3; margin: 0 0 0 1; }

    /* Chat messages: distinct left border per role, breathing room,
       background tint so adjacent messages don't bleed together. */
    .msg { padding: 1 2; margin-bottom: 1; background: $panel; }
    .msg-user {
        border-left: thick $success;
        background: $boost;
    }
    .msg-assistant {
        border-left: thick $accent;
    }
    .msg-tool {
        border-left: thick $warning;
        color: $text-muted;
        padding: 0 2;        /* tighter — tool lines are short */
        margin-bottom: 0;    /* let consecutive tools cluster */
        background: $surface;
    }
    .msg-system {
        border-left: thick $primary;
        color: $text-muted;
        background: $surface;
    }
    .msg-error {
        border-left: thick $error;
        color: $error;
        background: $error 10%;
    }

    #empty-main {
        content-align: center middle; color: $text-muted; height: 1fr;
    }
    """

    # Per-agent presentation. Add an entry here when registering a new engine.
    AGENT_ICONS = {
        "claude": "🤖",
        "codex": "🦾",
        "mock":   "🧪",
    }

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_convo", "New chat"),
        Binding("ctrl+o", "open_project", "Open project"),
        Binding("ctrl+shift+o", "new_window", "New window"),
        # ctrl+shift+w to close tab — ctrl+w is reserved by Input for
        # "delete word left" and was stealing keystrokes from the chat box.
        Binding("ctrl+shift+w", "close_tab", "Close tab"),
        # Cmd+W (via Terminal profile remap → ESC+w → alt+w) also closes the
        # active tab. Matches every other Mac app's muscle memory.
        Binding("alt+w", "close_tab", "Close tab", show=False),
        Binding("ctrl+r", "refresh_sidebar", "Refresh"),
        # Ctrl+I literally is Tab in terminals (both send byte 0x09), so the
        # old `ctrl+i` binding was unreachable — Textual saw `tab` and shifted
        # focus instead. Use ctrl+shift+i, and also accept alt+i so the
        # Terminal profile remap (Cmd+I → ESC+i → alt+i) works as a Mac-native
        # shortcut once we add the keyMap entry.
        Binding("ctrl+shift+i", "attach_image", "Attach image"),
        Binding("alt+i", "attach_image", "Attach image", show=False),
        # Smart paste — clipboard image → attach; otherwise pbpaste text.
        # The clickable "📎 Paste" button in the action bar is the most
        # reliable entry point. Keyboard paths are best-effort:
        #   - Ctrl+Shift+V: Terminal.app collapses this to byte 0x16 (same
        #     as Ctrl+V), so Textual's built-in Input.paste handler swallows
        #     it before this binding can fire. Left here for terminals that
        #     send a distinct CSI sequence (iTerm2 with modifyOtherKeys).
        #   - alt+v: fires when Cmd+V is remapped via Anvil.terminal.
        #     Terminal.app caches profiles at launch and dedupes by name on
        #     reopen, so this only kicks in after a full Terminal.app quit
        #     + relaunch — not just an Anvil restart.
        Binding("ctrl+shift+v", "smart_paste", "Paste", show=False),
        Binding("alt+v", "smart_paste", "Paste", show=False),
        Binding("ctrl+s", "save_file", "Save file"),
        Binding("ctrl+shift+m", "switch_agent", "Switch agent"),
        # In-app copy for the focused file editor / input. Cmd+C is left
        # alone so Terminal.app's native copy-selection works (Option-drag
        # to select, then Cmd+C). Per-message click-to-copy on chat bubbles
        # handles the common "grab this response" case.
        Binding("ctrl+shift+c", "copy", "Copy"),
        # Select-all the active file tab (then Cmd+C or Ctrl+Shift+C to copy).
        Binding("ctrl+shift+a", "select_all_file", "Select all"),
        # Settings — Mac convention is Cmd+, which Terminal.app remaps through
        # the Anvil profile to alt+,. We also bind the plain ctrl+, so the
        # binding works without the profile installed.
        Binding("ctrl+comma", "open_settings", "Settings"),
        Binding("alt+comma", "open_settings", "Settings", show=False),
    ]

    # File extensions we recognize as images dragged/pasted into the input.
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".tiff"}

    # Cap on how large a file we'll open in-app. Anything bigger should go to
    # an external editor.
    MAX_OPEN_FILE_BYTES = 1024 * 1024  # 1MB

    # File extension → TextArea language. Falls back to plain text on miss
    # or when the language isn't installed.
    LANG_BY_EXT = {
        ".py": "python", ".pyi": "python",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "javascript", ".tsx": "javascript", ".jsx": "javascript",
        ".json": "json",
        ".md": "markdown", ".markdown": "markdown",
        ".html": "html", ".htm": "html",
        ".css": "css",
        ".toml": "toml",
        ".yaml": "yaml", ".yml": "yaml",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash",
        ".sql": "sql",
        ".xml": "xml",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
    }

    current_project: reactive[Project | None] = reactive(None)

    def __init__(self) -> None:
        super().__init__()
        # One engine instance per agent name; lazily constructed on first use.
        # Routing happens per-conversation via convo.agent → _engine_for().
        self._engine_instances: dict[str, Engine] = {}
        if USE_MOCK_ENGINE:
            self._engine_instances["mock"] = MockEngine()
        # map convo_id -> tab id (so we can switch instead of opening twice)
        self.open_tabs: dict[str, str] = {}
        # map tab id -> Conversation (to resolve active tab back to a convo)
        self.tab_convos: dict[str, Conversation] = {}
        # parallel arrays for ListView selection (avoid duplicate-id issues
        # caused by lv.clear() being async)
        self._sidebar_projects: list[Project] = []
        self._sidebar_convos: list[Conversation] = []
        # track tool-use widgets so we can update them on ToolUseEnd
        # convo_id -> {tool_id: Static}
        self._tool_widgets: dict[str, dict[str, Static]] = {}
        # convo_id -> {tool_id: (name, input)} so we can re-render with status
        self._tool_info: dict[str, dict[str, tuple[str, dict]]] = {}
        # convo_ids whose turn is currently streaming (input disabled while in this set)
        self._busy_convos: set[str] = set()
        # convo_id -> list of pending attachment file paths (sent on next submit)
        self._pending_attachments: dict[str, list[str]] = {}
        # File-tab state: file_path str -> tab_id, and tab_id -> file path
        self.open_file_tabs: dict[str, str] = {}
        self.tab_files: dict[str, Path] = {}
        # tab_id -> True if file has unsaved edits (for the • marker in title)
        self._dirty_files: dict[str, bool] = {}

    # ---------- compose ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("PROJECTS", classes="section")
                yield ListView(id="project-list")
                yield Button("+ Open project", id="btn-open-project", variant="primary")

                yield Label("FILES", classes="section")
                with Vertical(id="file-tree-host"):
                    yield Static("[dim](no project)[/]", id="file-tree-placeholder")

                yield Label("CONVERSATIONS", classes="section")
                yield ListView(id="convo-list")
                yield Button("+ New chat", id="btn-new-chat", variant="success")
            with Vertical(id="main"):
                # Thin action bar above tabs — always visible "× Close" so file
                # tabs (which have no native close affordance) can be closed
                # by mouse, not just keyboard.
                with Horizontal(id="action-bar"):
                    yield Static("", id="active-tab-hint")
                    yield Button("× Close tab", id="btn-close-tab", variant="warning")
                yield TabbedContent(id="convo-tabs")
                yield Static(
                    "Open a project (Ctrl+O) and start a chat (Ctrl+N) to begin.",
                    id="empty-main",
                )
                with Horizontal(id="input-row"):
                    yield ChatInput(
                        placeholder="Type a message and press Enter…",
                        id="message-input",
                    )
                    yield Button("📎 Paste", id="btn-paste", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Anvil"
        self.refresh_projects()
        # Honor ANVIL_OPEN_PATH_B64 — set by a parent window spawning a fresh
        # one via action_open_project so the new window lands on the picked
        # project instead of empty state.
        self._autoload_project_from_env()
        self._update_empty_state()
        # First-launch onboarding. Deferred so the modal opens *after* the
        # main screen has mounted (otherwise the wizard renders against an
        # empty Screen and looks broken).
        if not config.load_global().onboarded:
            self.call_after_refresh(self._show_welcome_wizard)

    def _show_welcome_wizard(self) -> None:
        agents = detect.detect_all()

        def _on_done(picked: str | None) -> None:
            cfg = config.load_global()
            cfg.onboarded = True
            if picked:
                cfg.default_agent = picked
            config.save_global(cfg)
            if picked:
                self.notify(f"Default agent set to '{picked}'. Open a project to start.")

        self.push_screen(WelcomeWizard(agents), _on_done)

    def _autoload_project_from_env(self) -> None:
        import base64
        import os
        encoded = os.environ.pop("ANVIL_OPEN_PATH_B64", "").strip()
        if not encoded:
            return
        try:
            path = base64.b64decode(encoded).decode("utf-8")
            resolved = str(Path(path).expanduser().resolve())
        except Exception:  # noqa: BLE001
            return
        # Already registered? Just select it.
        for p in storage.load_registry():
            if str(p.working_dir) == resolved:
                self.current_project = p
                self.refresh_projects()
                self.refresh_convos()
                self.refresh_file_tree()
                return
        base_name = _project_name_from_path(resolved)
        existing = {p.name for p in storage.load_registry()}
        name = base_name
        i = 2
        while name in existing:
            name = f"{base_name}-{i}"
            i += 1
        try:
            proj = storage.add_project(
                name=name, path=resolved,
                default_agent=config.load_global().default_agent,
            )
        except Exception:  # noqa: BLE001
            return
        self.current_project = proj
        self.refresh_projects()
        self.refresh_convos()
        self.refresh_file_tree()

    # ---------- engine routing ----------

    def _agent_for(self, convo: Conversation) -> str:
        """Resolve the agent name for a conversation, defaulting safely."""
        name = (convo.agent or "").strip()
        if not name and self.current_project:
            name = (self.current_project.default_agent or "").strip()
        return name or "claude"

    def _engine_for(self, convo: Conversation) -> Engine:
        name = self._agent_for(convo)
        if name not in self._engine_instances:
            cls = get_engine_class(name)
            self._engine_instances[name] = cls()
        return self._engine_instances[name]

    def _agent_icon(self, name: str) -> str:
        return self.AGENT_ICONS.get(name, "●")

    async def _close_engine_for_convo(self, convo_id: str) -> None:
        """Close the conversation in WHATEVER engine owns it. Cheaper than
        closing on every engine — we just iterate the few that exist."""
        for engine in self._engine_instances.values():
            try:
                await engine.close(convo_id)
            except Exception:  # noqa: BLE001
                pass

    # ---------- refresh helpers ----------

    def refresh_projects(self) -> None:
        lv = self.query_one("#project-list", ListView)
        # remove via API that's safe to call repeatedly (clear() races append())
        for child in list(lv.children):
            child.remove()
        self._sidebar_projects = list(storage.load_registry())
        for p in self._sidebar_projects:
            prefix = "[b]● [/]" if (self.current_project and self.current_project.name == p.name) else "  "
            lv.append(ListItem(Label(f"{prefix}{p.name}")))

    def refresh_convos(self) -> None:
        lv = self.query_one("#convo-list", ListView)
        for child in list(lv.children):
            child.remove()
        self._sidebar_convos = []
        if not self.current_project:
            return
        self._sidebar_convos = list(storage.list_conversations(self.current_project))
        for c in self._sidebar_convos:
            mark = "● " if c.id in self.open_tabs else "  "
            lv.append(ListItem(Label(f"{mark}{c.title or c.id}")))

    def refresh_file_tree(self) -> None:
        host = self.query_one("#file-tree-host", Vertical)
        for child in list(host.children):
            child.remove()
        if not self.current_project:
            host.mount(Static("[dim](no project)[/]"))
            return
        path = self.current_project.working_dir
        if not path.exists():
            host.mount(Static(f"[dim](missing) {path}[/]"))
            return
        # NB: no explicit id=, otherwise we collide with the still-mounted
        # previous tree when switching projects (remove() is async-deferred).
        host.mount(FilteredDirectoryTree(str(path)))

    def _update_empty_state(self) -> None:
        empty = self.query_one("#empty-main", Static)
        tabs = self.query_one(TabbedContent)
        any_tabs = bool(self.open_tabs or self.open_file_tabs)
        if any_tabs:
            empty.display = False
            tabs.display = True
        else:
            empty.display = True
            tabs.display = False
        self._refresh_action_bar()

    def _refresh_action_bar(self) -> None:
        """Show / hide the action bar and update its hint label."""
        try:
            bar = self.query_one("#action-bar", Horizontal)
            hint = self.query_one("#active-tab-hint", Static)
            btn = self.query_one("#btn-close-tab", Button)
        except Exception:  # noqa: BLE001
            return
        tabs = self.query_one(TabbedContent)
        active = tabs.active
        # Hide the bar whenever nothing is open. Don't trust tabs.active
        # alone — it can hold a stale id after the last pane is removed.
        if not (self.open_tabs or self.open_file_tabs):
            bar.display = False
            return
        bar.display = True
        if active in self.tab_files:
            label = f"📄 {self.tab_files[active].name}  [dim]· Cmd+W or Ctrl+Shift+W to close[/]"
            btn.disabled = False
        elif active in self.tab_convos:
            c = self.tab_convos[active]
            label = f"💬 {_short(c.title or c.id, 40)}  [dim]· Cmd+W or Ctrl+Shift+W to close[/]"
            btn.disabled = False
        else:
            label = ""
            btn.disabled = True
        hint.update(label)

    # ---------- list selection ----------

    @on(ListView.Selected, "#project-list")
    def on_project_selected(self, event: ListView.Selected) -> None:
        lv = self.query_one("#project-list", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._sidebar_projects)):
            return
        p = self._sidebar_projects[idx]
        if self.current_project and self.current_project.name == p.name:
            return
        self.current_project = p
        self._close_all_tabs()
        self.refresh_projects()
        self.refresh_convos()
        self.refresh_file_tree()
        self.notify(f"Project: {p.name}")

    @on(ListView.Selected, "#convo-list")
    def on_convo_selected(self, event: ListView.Selected) -> None:
        lv = self.query_one("#convo-list", ListView)
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._sidebar_convos)):
            return
        self._open_convo_tab(self._sidebar_convos[idx])

    # ---------- tabs ----------

    def _tab_id_for(self, convo: Conversation) -> str:
        return f"tab-{_safe_id(convo.id)}"

    def _chat_id_for(self, convo: Conversation) -> str:
        return f"chat-{_safe_id(convo.id)}"

    def _convo_tab_title(self, convo: Conversation) -> str:
        """Tab title with the conversation's agent icon prefixed."""
        agent = self._agent_for(convo)
        icon = self._agent_icon(agent)
        return f"{icon} {_short(convo.title or convo.id, 22)}"

    def _open_convo_tab(self, convo: Conversation) -> None:
        tabs = self.query_one(TabbedContent)
        if convo.id in self.open_tabs:
            tabs.active = self.open_tabs[convo.id]
            self.query_one("#message-input", Input).focus()
            return
        tab_id = self._tab_id_for(convo)
        # Pre-render the existing transcript as initial children of the VerticalScroll
        # so they're part of the widget tree BEFORE mount — calling chat.mount(...)
        # before the chat itself is attached raises MountError.
        # Cap displayed history to keep tab-open snappy even for huge convos.
        # The full transcript stays on disk and is used to build the engine's
        # system prompt; this is purely a display cap.
        MAX_DISPLAY = 150
        total_count = storage.count_messages(self.current_project, convo)
        display_msgs = storage.tail_messages(self.current_project, convo, MAX_DISPLAY)
        elided = max(0, total_count - len(display_msgs))
        # Ensure existing convos without an agent field get one
        if not (convo.agent or "").strip():
            convo.agent = self._agent_for(convo)
            storage.save_conversation_meta(self.current_project, convo)
        history_widgets: list[Static] = []
        if elided:
            history_widgets.append(
                Static(
                    f"[dim]… {elided} earlier messages elided (full transcript on disk) …[/]",
                    classes="msg msg-system",
                )
            )
        history_widgets.extend(
            MessageWidget(
                self._render_message(m.role, m.content),
                raw_text=m.content or "",
                role=m.role,
                classes=f"msg msg-{m.role}",
            )
            for m in display_msgs
        )
        chat = VerticalScroll(
            *history_widgets,
            id=self._chat_id_for(convo),
            classes="chat-pane",
        )
        pane = TabPane(self._convo_tab_title(convo), chat, id=tab_id)
        tabs.add_pane(pane)
        self.open_tabs[convo.id] = tab_id
        self.tab_convos[tab_id] = convo
        self._tool_widgets.setdefault(convo.id, {})

        # Defer both `active=` and the scroll until after the pane has fully
        # mounted — setting tabs.active immediately races with add_pane and
        # raises "No Tab with id …" on the first frame.
        def _activate() -> None:
            try:
                tabs.active = tab_id
            except Exception:  # noqa: BLE001
                pass
            chat.scroll_end(animate=False)
            self._refresh_action_bar()

        self.call_after_refresh(_activate)
        self.refresh_convos()
        self._update_empty_state()
        self.query_one("#message-input", Input).focus()

    def _close_all_tabs(self) -> None:
        tabs = self.query_one(TabbedContent)
        # conversation tabs
        for cid, tid in list(self.open_tabs.items()):
            try:
                tabs.remove_pane(tid)
            except Exception:  # noqa: BLE001
                pass
            asyncio.create_task(self._close_engine_for_convo(cid))
        self.open_tabs.clear()
        self.tab_convos.clear()
        self._tool_widgets.clear()
        self._tool_info.clear()
        # file tabs (they're project-scoped — closing project = closing files)
        for path_key, tid in list(self.open_file_tabs.items()):
            try:
                tabs.remove_pane(tid)
            except Exception:  # noqa: BLE001
                pass
        self.open_file_tabs.clear()
        self.tab_files.clear()
        self._dirty_files.clear()
        self._update_empty_state()

    def action_close_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        active = tabs.active
        if not active:
            return
        # file tab?
        if active in self.tab_files:
            if self._dirty_files.get(active):
                # don't lose unsaved edits silently
                self.notify(
                    "Unsaved changes — press Ctrl+S to save first, "
                    "or close again to discard.",
                    severity="warning",
                )
                self._dirty_files[active] = False  # arm "discard on next press"
                return
            path = self.tab_files.pop(active, None)
            self.open_file_tabs.pop(str(path), None) if path else None
            self._dirty_files.pop(active, None)
            try:
                tabs.remove_pane(active)
            except Exception:  # noqa: BLE001
                pass
            self._update_empty_state()
            return
        # conversation tab
        for cid, tid in list(self.open_tabs.items()):
            if tid == active:
                try:
                    tabs.remove_pane(active)
                finally:
                    self.open_tabs.pop(cid, None)
                    self.tab_convos.pop(active, None)
                    self._tool_widgets.pop(cid, None)
                    self._tool_info.pop(cid, None)
                    asyncio.create_task(self._close_engine_for_convo(cid))
                break
        self.refresh_convos()
        self._update_empty_state()

    def _active_convo(self) -> Conversation | None:
        tabs = self.query_one(TabbedContent)
        if not tabs.active:
            return None
        return self.tab_convos.get(tabs.active)

    def _active_file_tab(self) -> str | None:
        """Returns the active tab id IF it's a file tab, else None."""
        tabs = self.query_one(TabbedContent)
        active = tabs.active
        if active and active in self.tab_files:
            return active
        return None

    # ---------- file tabs (IDE-style) ----------

    @on(DirectoryTree.FileSelected)
    def on_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self._open_file_tab(Path(str(event.path)))

    def _open_file_tab(self, path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError as exc:
            self.notify(f"Can't open: {exc}", severity="error")
            return
        if not resolved.is_file():
            self.notify(f"Not a file: {resolved}", severity="warning")
            return

        key = str(resolved)
        tabs = self.query_one(TabbedContent)
        if key in self.open_file_tabs:
            tab_id = self.open_file_tabs[key]
            self.call_after_refresh(lambda: setattr(tabs, "active", tab_id))
            return

        try:
            size = resolved.stat().st_size
        except OSError as exc:
            self.notify(f"Stat failed: {exc}", severity="error")
            return
        if size > self.MAX_OPEN_FILE_BYTES:
            kb = size // 1024
            self.notify(
                f"File too large ({kb} KB > {self.MAX_OPEN_FILE_BYTES // 1024} KB cap). "
                "Open it in an external editor.",
                severity="warning",
            )
            return

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Read failed: {exc}", severity="error")
            return

        tab_id = f"file-{_safe_id(key)}"
        editor_id = f"editor-{_safe_id(key)}"
        lang = self.LANG_BY_EXT.get(resolved.suffix.lower())

        # code_editor() = nicer defaults (line numbers, monospace theme)
        try:
            editor = TextArea.code_editor(
                content,
                language=lang,
                soft_wrap=False,
                show_line_numbers=True,
                id=editor_id,
            )
        except Exception:  # noqa: BLE001
            # Language pack not installed — fall back to plain text
            editor = TextArea.code_editor(
                content, soft_wrap=False, show_line_numbers=True, id=editor_id
            )

        pane = TabPane(
            self._file_tab_title(resolved, dirty=False),
            editor,
            id=tab_id,
        )
        tabs.add_pane(pane)
        self.open_file_tabs[key] = tab_id
        self.tab_files[tab_id] = resolved
        self._dirty_files[tab_id] = False
        self._update_empty_state()

        def _activate() -> None:
            try:
                tabs.active = tab_id
            except Exception:  # noqa: BLE001
                pass
            self._refresh_action_bar()

        self.call_after_refresh(_activate)

    @staticmethod
    def _file_tab_title(path: Path, dirty: bool) -> str:
        marker = "● " if dirty else ""
        return f"{marker}📄 {path.name}"

    @on(TextArea.Changed)
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Mark the tab dirty on any edit."""
        # find which file tab owns this TextArea
        editor_id = event.text_area.id or ""
        # editor_id format: editor-<safe_id_of_path>
        if not editor_id.startswith("editor-"):
            return
        # locate matching file tab
        for tid, path in self.tab_files.items():
            if editor_id == f"editor-{_safe_id(str(path))}":
                if not self._dirty_files.get(tid):
                    self._dirty_files[tid] = True
                    self._set_file_tab_title(tid, path, dirty=True)
                return

    def _set_file_tab_title(self, tab_id: str, path: Path, dirty: bool) -> None:
        tabs = self.query_one(TabbedContent)
        try:
            tab = tabs.get_tab(tab_id)
            tab.label = self._file_tab_title(path, dirty)
        except Exception:  # noqa: BLE001
            pass

    def action_save_file(self) -> None:
        tab_id = self._active_file_tab()
        if not tab_id:
            return
        path = self.tab_files[tab_id]
        editor = self.query_one(f"#editor-{_safe_id(str(path))}", TextArea)
        try:
            path.write_text(editor.text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Save failed: {exc}", severity="error")
            return
        self._dirty_files[tab_id] = False
        self._set_file_tab_title(tab_id, path, dirty=False)
        self.notify(f"Saved {path.name}")

    def _active_file_editor(self) -> TextArea | None:
        tab_id = self._active_file_tab()
        if not tab_id:
            return None
        path = self.tab_files[tab_id]
        try:
            return self.query_one(f"#editor-{_safe_id(str(path))}", TextArea)
        except Exception:  # noqa: BLE001
            return None

    def action_select_all_file(self) -> None:
        editor = self._active_file_editor()
        if editor is None:
            self.notify("No file tab active.", severity="warning")
            return
        # Move cursor to end with shift held → selects from current pos to end;
        # then move home with shift held → extends to the start. Net = select all.
        try:
            editor.select_all()  # newer Textual
        except AttributeError:
            # Fallback for older API
            doc_end = editor.document.end
            editor.selection = ((0, 0), doc_end)
        self.notify("Selected entire file. Ctrl+Shift+C to copy.")

    def action_copy(self) -> None:
        """Smart copy: file tab → selected_text or whole file; ChatInput → its value."""
        # If a file editor is active, copy its selection or all of it
        editor = self._active_file_editor()
        text = ""
        source = ""
        if editor is not None:
            text = (editor.selected_text or editor.text) or ""
            source = "file"
        else:
            focused = self.focused
            if isinstance(focused, ChatInput):
                text = focused.value or ""
                source = "input"
        if not text:
            self.notify("Nothing to copy.", severity="warning")
            return
        self._push_to_clipboard(text)
        self.notify(f"Copied {len(text)} chars from {source}.")

    def _push_to_clipboard(self, text: str) -> None:
        # Try Textual's OSC52 path first (works inside the running terminal)
        try:
            self.copy_to_clipboard(text)
        except Exception:  # noqa: BLE001
            pass
        # Always also push to the macOS system clipboard via pbcopy so paste
        # works in any other Mac app — OSC52 only fills the terminal's buffer
        # in some setups.
        try:
            import subprocess
            subprocess.run(
                ["/usr/bin/pbcopy"],
                input=text.encode("utf-8"),
                check=True,
                timeout=2,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------- message rendering ----------

    # Role → (icon, label, accent color name from Rich palette)
    ROLE_PRESENTATION = {
        "user":       ("👤", "You",    "green"),
        "assistant":  ("🤖", "Claude", "cyan"),
        "tool":       ("🔧", "Tool",   "yellow"),
        "system":     ("ℹ️ ", "System", "blue"),
        "error":      ("⚠️ ", "Error",  "red"),
    }

    def _mount_message(
        self,
        chat: VerticalScroll,
        role: str,
        content: str,
        *,
        raw: str | None = None,
    ) -> "MessageWidget":
        """Mount a chat bubble. `raw` is the text copied on click; defaults to
        `content`. Pass `raw=""` for placeholders ("▋ thinking…") so a stray
        click doesn't copy the spinner."""
        raw_text = content if raw is None else raw
        widget = MessageWidget(
            self._render_message(role, content),
            raw_text=raw_text,
            role=role,
            classes=f"msg msg-{role}",
        )
        chat.mount(widget)
        self._maybe_scroll_end(chat)
        return widget

    def _mount_tool(self, chat: VerticalScroll, tool_id: str, name: str, inp: dict) -> Static:
        widget = Static(
            self._render_tool(name, inp, status="running"),
            classes="msg msg-tool",
        )
        chat.mount(widget)
        self._maybe_scroll_end(chat)
        return widget

    def _render_message(self, role: str, content: str, *, finalized: bool = True):
        """Build a Rich renderable for one chat message.

        - `user` / `system` / `error`: plain text (no markup risk from user input).
        - `assistant`: when `finalized=True`, render as Markdown so headings,
          bold, lists, and ```fenced code blocks``` look right; when streaming
          mid-turn (`finalized=False`), keep it plain so we don't reflow the
          layout on every token.
        """
        icon, label, color = self.ROLE_PRESENTATION.get(role, ("•", role, "white"))
        header = RichText(f"{icon} {label}", style=f"bold {color}")

        body_text = content or ""
        if role == "assistant" and finalized and body_text.strip():
            body = RichMarkdown(body_text, code_theme="monokai", inline_code_lexer="text")
        else:
            # Plain text — preserves whitespace, no markdown surprises
            body = RichText(body_text)

        if not body_text:
            return Group(header)

        # Discoverability hint that the message is click-to-copy. Skip while
        # streaming (would flicker) and on roles that aren't worth copying.
        show_copy_hint = finalized and body_text.strip() and role in ("user", "assistant")
        if show_copy_hint:
            footer = RichText("⧉ copy", style="dim italic")
            return Group(header, RichText(""), body, RichText(""), footer)
        return Group(header, RichText(""), body)

    def _render_tool(self, name: str, inp: dict, *, status: str = "running") -> RichText:
        """Compact one-line tool indicator with status marker."""
        marker_map = {
            "running": ("⋯", "yellow"),
            "ok":      ("✓", "green"),
            "error":   ("✗", "red"),
        }
        marker, color = marker_map.get(status, ("•", "yellow"))
        summary = self._tool_summary(name, inp)
        t = RichText()
        t.append(f" {marker} ", style=f"bold {color}")
        t.append(f"{name}", style="bold")
        if summary:
            t.append(f"  {summary}", style="dim")
        return t

    @staticmethod
    def _maybe_scroll_end(chat: VerticalScroll) -> None:
        """Only auto-scroll if the user is already near the bottom — don't
        yank them away when they've scrolled up to read."""
        try:
            max_y = chat.max_scroll_y
        except Exception:  # noqa: BLE001
            max_y = 0
        # 2-row tolerance so streaming text doesn't jitter
        if chat.scroll_y >= max_y - 2:
            chat.scroll_end(animate=False)

    @staticmethod
    def _tool_summary(name: str, inp: dict) -> str:
        # short single-line summary of the tool input
        if name in {"Read", "Write", "Edit"}:
            return str(inp.get("file_path", ""))[:80]
        if name == "Bash":
            return _short(str(inp.get("command", "")), 80)
        if name in {"Glob", "Grep"}:
            return _short(str(inp.get("pattern", "")), 80)
        if name == "WebFetch":
            return str(inp.get("url", ""))[:80]
        # fallback: first short field
        try:
            j = json.dumps(inp)
            return _short(j, 80)
        except Exception:  # noqa: BLE001
            return ""

    # ---------- input ----------

    @on(Input.Submitted, "#message-input")
    async def on_submit(self, event: Input.Submitted) -> None:
        raw = event.value
        project = self.current_project
        # If the active tab is a file, gently nudge the user to switch back.
        if self._active_file_tab():
            self.notify(
                "Active tab is a file viewer. Switch to a chat tab to send a message.",
                severity="warning",
            )
            return
        convo = self._active_convo()
        if not (project and convo):
            self.notify("Open a project and start a chat first.", severity="warning")
            return
        # Don't let the user fire a second message while the previous turn is
        # still streaming — the SDK doesn't queue them and the UI would race.
        if convo.id in self._busy_convos:
            self.notify("Wait for the current response to finish.", severity="warning")
            return

        # Detect image paths typed/dragged into the input box. Terminal.app pastes
        # dragged file paths as text (often single-quoted) — we extract them and
        # treat them as attachments.
        extra_attachments, residual = self._extract_image_paths(raw)
        attachments = list(self._pending_attachments.get(convo.id, [])) + extra_attachments
        text = residual.strip()

        # If the user pasted/dragged a path-looking thing but nothing matched,
        # surface the reason — silent fail was the worst part of the prior UX.
        if not extra_attachments:
            reason = self._diagnose_path_input(raw)
            if reason:
                self.notify(reason, severity="warning", timeout=8)
                return

        if not text and not attachments:
            return

        event.input.value = ""
        inp = self.query_one("#message-input", Input)
        inp.disabled = True
        self._busy_convos.add(convo.id)
        self._pending_attachments[convo.id] = []  # consumed

        chat = self.query_one(f"#{self._chat_id_for(convo)}", VerticalScroll)

        # User message we persist + render includes attachment markers so it
        # looks the same when reopened later.
        display_lines = []
        for a in attachments:
            display_lines.append(f"📎 {Path(a).name}")
        if text:
            display_lines.append(text)
        display_text = "\n".join(display_lines)

        user_msg = Message(role="user", content=display_text)
        storage.append_message(project, convo, user_msg)
        self._mount_message(chat, "user", display_text)

        # What we actually send to Claude: prepend a directive asking it to Read
        # the image files. Claude's Read tool supports images and will load them
        # as multimodal content automatically.
        if attachments:
            attach_block = "\n".join(f"- {a}" for a in attachments)
            send_text = (
                "I've attached the following image(s) — please use the Read tool "
                "to view each one before responding:\n"
                f"{attach_block}\n\n{text}".rstrip()
            )
        else:
            send_text = text

        # "thinking" placeholder so the UI doesn't look frozen while waiting
        # for the first token. Replaced in-place on the first TextDelta.
        # raw="" so a stray click on the spinner placeholder copies nothing.
        assistant_widget = self._mount_message(chat, "assistant", "▋ thinking…", raw="")
        buf = ""
        last_flush = time.monotonic()
        error_text: str | None = None
        try:
            engine = self._engine_for(convo)
            async for ev in engine.send(project, convo, send_text):
                if isinstance(ev, TextDelta):
                    buf += ev.text
                    # Batch UI updates — flushing on every token kills FPS on
                    # long responses. 30ms cap is below the human flicker
                    # threshold and saves ~95% of widget.update() calls.
                    # During streaming we render as plain text (finalized=False)
                    # so Rich isn't re-parsing partial markdown on every flush.
                    now = time.monotonic()
                    if now - last_flush >= 0.03:
                        assistant_widget.set_message(
                            self._render_message("assistant", buf, finalized=False),
                            buf,
                        )
                        self._maybe_scroll_end(chat)
                        last_flush = now
                elif isinstance(ev, ToolUseStart):
                    # finalize text-so-far before the tool line so it's not stuck
                    # in "still streaming" plain-text mode.
                    if buf:
                        assistant_widget.set_message(
                            self._render_message("assistant", buf), buf
                        )
                    w = self._mount_tool(chat, ev.tool_id, ev.name, ev.input)
                    self._tool_widgets.setdefault(convo.id, {})[ev.tool_id] = w
                    self._tool_info.setdefault(convo.id, {})[ev.tool_id] = (ev.name, ev.input)
                    # Each tool starts a new assistant message bubble so the
                    # response after the tool isn't appended to the pre-tool text.
                    buf = ""
                    assistant_widget = self._mount_message(chat, "assistant", "", raw="")
                elif isinstance(ev, ToolUseEnd):
                    w = self._tool_widgets.get(convo.id, {}).get(ev.tool_id)
                    info = self._tool_info.get(convo.id, {}).get(ev.tool_id)
                    if w is not None and info is not None:
                        name, inp_dict = info
                        status = "error" if ev.is_error else "ok"
                        w.update(self._render_tool(name, inp_dict, status=status))
                elif isinstance(ev, TurnComplete):
                    if ev.message.content:
                        storage.append_message(project, convo, ev.message)
                    asyncio.create_task(self._update_memory_safe(project, convo))
        except Exception as exc:  # noqa: BLE001
            error_text = f"engine error: {exc}"
        finally:
            # Final flush — promote streaming-plain to finalized-markdown
            if buf:
                assistant_widget.set_message(
                    self._render_message("assistant", buf), buf
                )
                self._maybe_scroll_end(chat)
            inp.disabled = False
            self._busy_convos.discard(convo.id)
            inp.focus()

        if error_text:
            self._mount_message(chat, "error", error_text)

        if not buf.strip() and not error_text:
            assistant_widget.set_message(
                self._render_message("assistant", "(no response)"),
                "",
            )

        self.refresh_convos()

    async def _update_memory_safe(self, project: Project, convo: Conversation) -> None:
        try:
            await memory.update_memory(project, convo)
        except Exception:  # noqa: BLE001
            # never let summarization break the app
            pass

    # ---------- image attachments ----------

    def _extract_image_paths(self, raw: str) -> tuple[list[str], str]:
        """Pull recognizable image-file paths out of the input text.

        Handles paths that may be quoted (Terminal.app wraps dragged paths in
        single quotes) or contain spaces escaped as `\\ `. Returns (paths, leftover_text).
        """
        if not raw:
            return [], ""
        # Try the whole stripped input as a single path first (common drag-drop case)
        candidate = raw.strip().strip("'\"")
        candidate = candidate.replace("\\ ", " ")
        if self._is_image_path(candidate):
            return [candidate], ""
        # Otherwise split on whitespace and try each token; un-escape spaces is
        # too risky here without a real tokenizer, so we just take quoted runs
        # and bare tokens.
        import shlex
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = raw.split()
        paths: list[str] = []
        leftover: list[str] = []
        for tok in tokens:
            if self._is_image_path(tok):
                paths.append(tok)
            else:
                leftover.append(tok)
        return paths, " ".join(leftover)

    def _is_image_path(self, p: str) -> bool:
        if not p:
            return False
        try:
            path = Path(p).expanduser()
        except (OSError, ValueError):
            return False
        return (
            path.is_absolute()
            and path.suffix.lower() in self.IMAGE_EXTS
            and path.is_file()
        )

    def _diagnose_path_input(self, raw: str) -> str | None:
        """If `raw` looks like the user tried to attach a file but it didn't
        match, return a human-readable reason. Returns None if `raw` isn't
        path-like enough to bother diagnosing.

        Covers the silent-fail cases: wrong extension, file not found,
        TCC-blocked read (looks like "doesn't exist" from Python's POV),
        relative path, etc.
        """
        if not raw:
            return None
        candidate = raw.strip().strip("'\"").replace("\\ ", " ")
        if not (candidate.startswith("/") or candidate.startswith("~/")):
            return None
        try:
            path = Path(candidate).expanduser()
        except (OSError, ValueError):
            return f"Couldn't parse path: {candidate}"
        if not path.is_absolute():
            return f"Not an absolute path: {candidate}"
        if not path.exists():
            return (
                f"File not found at {path}. If it exists, Terminal may lack "
                "Files/Folders permission for that folder (System Settings → "
                "Privacy & Security → Files and Folders → Terminal)."
            )
        if not path.is_file():
            return f"Not a regular file: {path}"
        if path.suffix.lower() not in self.IMAGE_EXTS:
            return (
                f"Unrecognized image extension '{path.suffix}'. "
                f"Supported: {', '.join(sorted(self.IMAGE_EXTS))}"
            )
        return None  # everything looks fine — shouldn't reach here

    def action_attach_image(self) -> None:
        """Ctrl+Shift+I: grab an image from the macOS clipboard, save it to the
        project's .harness/attachments/ folder, queue it for the next submit."""
        if not self.current_project:
            self.notify("Open a project first.", severity="warning")
            return
        convo = self._active_convo()
        if convo is None:
            self.notify("Open a chat first.", severity="warning")
            return
        self.run_worker(self._attach_image_flow(self.current_project, convo), exclusive=False)

    def action_smart_paste(self) -> None:
        """Cmd+V: attach if clipboard has image; otherwise insert text from
        pbpaste at the cursor (newlines flattened to spaces for the single-
        line ChatInput). Driven by Terminal-profile remap because Terminal.app
        doesn't dispatch a native Paste event for image-only clipboards."""
        project = self.current_project
        convo = self._active_convo()
        self.run_worker(self._smart_paste_flow(project, convo), exclusive=False)

    async def _smart_paste_flow(
        self, project: Project | None, convo: Conversation | None
    ) -> None:
        # Try image first — only viable when a project + chat are open.
        if project is not None and convo is not None:
            saved = await self._try_attach_image_from_clipboard(project, convo)
            if saved is not None:
                n = len(self._pending_attachments[convo.id])
                self.notify(
                    f"📎 attached ({n} pending) — type a message and Enter to send."
                )
                return
        # Fall through to text paste via pbpaste.
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/pbpaste",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            text = stdout.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Paste failed: {exc}", severity="error")
            return
        if not text:
            # No text and no image. Be honest about it.
            self.notify("Clipboard is empty.", severity="warning")
            return
        target = self.focused
        if not isinstance(target, ChatInput):
            try:
                target = self.query_one("#message-input", ChatInput)
            except Exception:  # noqa: BLE001
                self.notify("Focus the chat input first.", severity="warning")
                return
            target.focus()
        flattened = " ".join(text.splitlines())
        selection = target.selection
        if selection.is_empty:
            target.insert_text_at_cursor(flattened)
        else:
            target.replace(flattened, *selection)

    async def _try_attach_image_from_clipboard(
        self, project: Project, convo: Conversation
    ) -> Path | None:
        """Extract a PNG from the macOS clipboard into the project's attachments
        folder and register it as pending. Returns the saved path, or None if
        no image is available. Caller decides how to react to None (notify the
        user vs. silently fall through to text paste)."""
        target_dir = project.harness_dir / "attachments"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"clip-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        script = (
            'try\n'
            '  set imgData to the clipboard as «class PNGf»\n'
            f'  set fileRef to open for access POSIX file "{target}" with write permission\n'
            '  write imgData to fileRef\n'
            '  close access fileRef\n'
            '  return "ok"\n'
            'on error\n'
            '  return "no_image"\n'
            'end try\n'
        )
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        result = stdout.decode().strip()
        if result != "ok" or not target.exists():
            # Clean up zero-byte file AppleScript may have left on error.
            try:
                if target.exists() and target.stat().st_size == 0:
                    target.unlink()
            except OSError:
                pass
            return None
        self._pending_attachments.setdefault(convo.id, []).append(str(target))
        return target

    async def _attach_image_flow(self, project: Project, convo: Conversation) -> None:
        saved = await self._try_attach_image_from_clipboard(project, convo)
        if saved is None:
            self.notify("No image in clipboard.", severity="warning")
            return
        n = len(self._pending_attachments[convo.id])
        self.notify(
            f"📎 attached ({n} pending) — type a message and Enter to send."
        )

    # ---------- buttons / actions ----------

    @on(Button.Pressed, "#btn-open-project")
    def on_btn_open_project(self, event: Button.Pressed) -> None:
        self.action_open_project()

    @on(Button.Pressed, "#btn-new-chat")
    def on_btn_new_chat(self, event: Button.Pressed) -> None:
        self.action_new_convo()

    @on(Button.Pressed, "#btn-close-tab")
    def on_btn_close_tab(self, event: Button.Pressed) -> None:
        self.action_close_tab()

    @on(Button.Pressed, "#btn-paste")
    def on_btn_paste(self, event: Button.Pressed) -> None:
        """Mouse-driven entry point — works even when the Terminal profile
        cache has the wrong keymap and Cmd+V isn't intercepted."""
        self.action_smart_paste()

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Keep the action-bar hint label in sync with the active tab."""
        self._refresh_action_bar()

    def action_refresh_sidebar(self) -> None:
        self.refresh_projects()
        self.refresh_convos()
        self.refresh_file_tree()

    def action_open_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def action_new_window(self) -> None:
        """Spawn another Anvil instance in a new Terminal window.

        Lets you work on two projects side-by-side without losing this one's
        state. Each window has independent tabs/engine; they share only the
        on-disk registry and conversation files.
        """
        self._spawn_terminal_running("exec anvil", note="New window opening…")

    def action_open_project(self) -> None:
        # push_screen_wait + async subprocess need a worker context
        self.run_worker(self._open_project_flow(), exclusive=True)

    async def _open_project_flow(self) -> None:
        path = await self._pick_folder()
        if not path:
            return
        resolved = str(Path(path).expanduser().resolve())

        # If a project is already open in this window, spawn a fresh window
        # for the picked path instead of clobbering current state. Matches the
        # Mac-app norm (Finder, VS Code) where "Open" preserves the existing
        # window. Sidebar clicks remain the in-window switch path.
        if self.current_project is not None:
            current_path = str(self.current_project.working_dir)
            if current_path == resolved:
                self.notify(
                    f"'{self.current_project.name}' is already open in this window.",
                    severity="warning",
                )
                return
            self._spawn_window_with_project(resolved)
            return

        # First project for this window — open in place.
        for p in storage.load_registry():
            if str(p.working_dir) == resolved:
                self.current_project = p
                self._close_all_tabs()
                self.refresh_projects()
                self.refresh_convos()
                self.refresh_file_tree()
                self.notify(f"Switched to existing project '{p.name}'")
                return
        base = _project_name_from_path(path)
        existing = {p.name for p in storage.load_registry()}
        name = base
        i = 2
        while name in existing:
            name = f"{base}-{i}"
            i += 1
        try:
            proj = storage.add_project(
                name=name, path=path,
                default_agent=config.load_global().default_agent,
            )
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Error: {exc}", severity="error")
            return
        self.current_project = proj
        self._close_all_tabs()
        self.refresh_projects()
        self.refresh_convos()
        self.refresh_file_tree()
        self.notify(f"Opened project '{proj.name}' at {proj.path}")

    def _spawn_window_with_project(self, path: str) -> None:
        """Spawn a fresh Anvil window targeting `path`. The new instance reads
        ANVIL_OPEN_PATH_B64 in on_mount and auto-selects the project. Base64
        sidesteps AppleScript/shell escaping for paths with spaces or quotes.
        """
        import base64
        encoded = base64.b64encode(path.encode("utf-8")).decode("ascii")
        # b64 alphabet is [A-Za-z0-9+/=] — safe in shell + AppleScript with no escaping.
        cmd = f"ANVIL_OPEN_PATH_B64={encoded} exec anvil"
        self._spawn_terminal_running(cmd, note=f"Opening '{Path(path).name}' in a new window…")

    def _spawn_terminal_running(self, cmd: str, note: str) -> None:
        """Open a new Terminal window and run `cmd`, applying the Anvil profile
        if it exists. `cmd` should be a shell-ready string (typically prefixed
        with `exec` so the shell process replaces itself)."""
        import subprocess
        script = (
            'tell application "Terminal" to set newTab to do script '
            f'"clear && {cmd}"\n'
            'try\n'
            '  tell application "Terminal" to set current settings of newTab to settings set "Anvil"\n'
            'end try'
        )
        try:
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", script,
                 "-e", 'tell application "Terminal" to activate'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.notify(note)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Couldn't open new window: {exc}", severity="error")

    def action_new_convo(self) -> None:
        if not self.current_project:
            self.notify("Open a project first (Ctrl+O).", severity="warning")
            return
        self.run_worker(self._new_convo_flow(), exclusive=True)

    async def _new_convo_flow(self) -> None:
        title = await self.push_screen_wait(
            SimpleTextModal("Conversation title (blank = auto):", "")
        )
        if title is None:
            return
        if not title.strip():
            title = datetime.now().strftime("chat %H:%M")
        convo = storage.create_conversation(self.current_project, title)
        # New convo inherits the project's default agent
        convo.agent = (self.current_project.default_agent or "claude").strip() or "claude"
        storage.save_conversation_meta(self.current_project, convo)
        self._open_convo_tab(convo)

    # ---------- agent switching ----------

    def action_switch_agent(self) -> None:
        if not self.current_project:
            self.notify("Open a project first.", severity="warning")
            return
        convo = self._active_convo()
        if convo is None:
            self.notify("Open a conversation first.", severity="warning")
            return
        self.run_worker(self._switch_agent_flow(self.current_project, convo), exclusive=True)

    async def _switch_agent_flow(self, project: Project, convo: Conversation) -> None:
        current = self._agent_for(convo)
        choice = await self.push_screen_wait(
            AgentPickerModal(available_agents(), current)
        )
        if not choice or choice == current:
            return
        # Cleanly close the engine that was holding this convo so the new
        # one can take over with a fresh client.
        await self._close_engine_for_convo(convo.id)
        convo.agent = choice
        storage.save_conversation_meta(project, convo)
        # Re-title the open tab to reflect the new agent
        tab_id = self.open_tabs.get(convo.id)
        if tab_id:
            try:
                tab = self.query_one(TabbedContent).get_tab(tab_id)
                tab.label = self._convo_tab_title(convo)
            except Exception:  # noqa: BLE001
                pass
        self.refresh_convos()
        self.notify(
            f"Switched to {choice}. The next message uses {choice} — "
            "prior transcript stays in context."
        )

    # ---------- folder picker (native macOS) ----------

    async def _pick_folder(self) -> str | None:
        """Pop a native macOS folder picker via osascript."""
        script = 'POSIX path of (choose folder with prompt "Choose project folder")'
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/osascript",
            "-e",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return None
        path = stdout.decode().strip().rstrip("/")
        return path or None

    # ---------- shutdown ----------

    async def on_unmount(self) -> None:
        for engine in self._engine_instances.values():
            close_all = getattr(engine, "close_all", None)
            if close_all is not None:
                try:
                    await close_all()
                except Exception:  # noqa: BLE001
                    pass


def run() -> None:
    HarnessApp().run()
