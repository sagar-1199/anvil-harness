# Packaging Anvil

This directory holds release artifacts for distributing Anvil. They are
templates — placeholder SHAs need to be filled in at release time.

## Release checklist

1. Bump version in `pyproject.toml`.
2. Build wheel + sdist: `uv build`.
3. Upload to PyPI: `uv publish` (needs PYPI_TOKEN).
4. Zip the .app bundle:
   ```bash
   ditto -c -k --keepParent Anvil.app Anvil-<version>.zip
   ```
5. Create a GitHub release with the .zip attached.
6. Update `packaging/homebrew/anvil-harness.rb` and `anvil.rb`:
   - Replace `PLACEHOLDER_SHA256_*` with real hashes
     (`shasum -a 256 <file>`).
   - Run `brew update-python-resources` to regenerate Python `resource`
     blocks if dependencies changed.
7. Push the updated formulas to the `homebrew-anvil` tap repo.

## Creating the Homebrew tap

One-time setup (separate from this repo):

```bash
gh repo create sagar-1199/homebrew-anvil --public --description "Homebrew formulas for Anvil"
git clone https://github.com/sagar-1199/homebrew-anvil
cd homebrew-anvil
mkdir -p Formula Casks
cp /path/to/anvil-harness/packaging/homebrew/anvil-harness.rb Formula/
cp /path/to/anvil-harness/packaging/homebrew/anvil.rb Casks/
git add . && git commit -m "Initial formulas"
git push
```

Users can then install with:

```bash
brew tap sagar-1199/anvil
brew install --cask anvil   # also pulls anvil-harness formula
```
