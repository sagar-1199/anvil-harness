# Homebrew cask for Anvil.app — the macOS launcher bundle.
#
# Anvil's CLI is distributed via PyPI (pipx install anvil-harness). The
# .app bundle is a launcher that finds `anvil` on PATH and runs it in
# Terminal.app or iTerm.
#
# We ship the cask alone (no formula) for v0.1.1 — bundling 40+ transitive
# Python deps as brew resources is brittle and out of proportion for a
# personal-scale project. The caveats nudge users to run pipx first if
# they haven't.
#
# Once published, users install with:
#
#   pipx install anvil-harness
#   brew tap sagar-1199/anvil
#   brew install --cask anvil
#
cask "anvil" do
  version "0.1.1"
  sha256 "eb62fd5ed9289994e108cfc014c4bc92b2c1c76d9de7ebc9d7dfba33a5324bce"

  url "https://github.com/sagar-1199/anvil-harness/releases/download/v#{version}/Anvil-#{version}.zip"
  name "Anvil"
  desc "Local TUI for any coding agent (Claude Code, Codex, …)"
  homepage "https://github.com/sagar-1199/anvil-harness"

  app "Anvil.app"

  caveats <<~EOS
    Anvil.app is a launcher — it expects the `anvil` CLI to be on PATH.
    If you haven't installed it yet:

      pipx install anvil-harness

    Then launch Anvil from Spotlight or `open -a Anvil`.

    Note: this .app is ad-hoc signed. On first launch macOS may say it
    "cannot be opened because Apple cannot check it for malicious software."
    Right-click → Open to bypass, or run:

      xattr -dr com.apple.quarantine /Applications/Anvil.app
  EOS

  zap trash: [
    "~/.anvil",
    "~/Library/Preferences/com.sagar.anvil.plist",
  ]
end
