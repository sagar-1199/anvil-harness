# Changelog

All notable changes to Anvil land here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

## [0.1.1] — 2026-05-22

### Fixed
- PyPI metadata URLs now point at the real repo (0.1.0 was published with a
  stale `your-org` placeholder in the wheel's `Project-URL` fields). 0.1.0
  has been yanked on PyPI; install 0.1.1 or later.

## [0.1.0] — 2026-05-22

First public release.

### Added
- Textual TUI with projects, multi-tab conversations, file tree, in-app editor.
- Agent-agnostic engine layer with three backends out of the box:
  - **Claude Code** (`claude-agent-sdk`, OAuth subscription — no API key).
  - **OpenAI Codex CLI** (subprocess bridge, stateless per turn).
  - **Mock** (offline echo, useful for UI work without burning tokens).
- First-launch wizard that detects installed agent CLIs and persists the
  user's pick to `~/.anvil/config.toml`.
- In-app Settings screen (`Ctrl+,` / `Cmd+,`) for changing default agent +
  model later. No config-file editing required.
- Mac-native shortcuts via the bundled `Anvil.terminal` profile
  (Cmd+C, Cmd+W, Cmd+, all reach the TUI).
- Portable `Anvil.app` launcher that resolves the `anvil` binary on PATH
  (works with `pipx`, `brew`, and dev installs).
- Cross-conversation memory: `<project>/.harness/memory.md` is loaded into
  every new chat as durable context.
- Homebrew formula + cask templates and `pipx install anvil-harness`
  distribution.

### Migration
- Legacy data at `~/.claude-harness/projects.json` is automatically copied
  to `~/.anvil/projects.json` on first launch. Original is left in place
  so you can roll back.

[Unreleased]: https://github.com/sagar-1199/anvil-harness/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/sagar-1199/anvil-harness/releases/tag/v0.1.1
[0.1.0]: https://github.com/sagar-1199/anvil-harness/releases/tag/v0.1.0
