# PyPI publishing checklist

Goal: `pip install auto-self-update` works from PyPI, from a clean
one-command build. Check items off top to bottom; the release section
repeats for every future version.

## 1. Packaging metadata (`pyproject.toml`)

- [x] `name = "auto-self-update"` â€” verified available on PyPI
      (2026-11-11); the plainer `self-update` normalizes to the taken
      `selfupdate` under PEP 503 and is rejected as too similar
- [x] `version` â€” 0.9.0; single source of truth lives here (the module
      itself has no `__version__`; runtime reads
      `importlib.metadata.version`, so no dual bookkeeping)
- [x] `description` â€” "Single-file, zero-dependency self-update on
      launch for Python CLIs."
- [x] `authors` â€” Julynx <julicabria@outlook.com>
- [x] `license = "MIT"` (SPDX string) + `license-files = ["LICENSE"]`
- [x] `requires-python = ">=3.10"` (PEP 604 unions in signatures)
- [x] `readme = "README.md"`
- [x] `keywords` â€” self-update, updater, cli, pip, pipx, uv
- [x] `classifiers` â€” Beta status, OS-independent (Win/Linux/macOS),
      Python 3.10â€“3.14, Software Development, Utilities
- [x] `[project.urls]` â€” Homepage, Repository, Issues â†’ github.com/Julynx/self-update
- [x] `[build-system]` setuptools + `[tool.setuptools] py-modules = ["selfupdate"]`

## 2. Build hygiene

- [x] `.gitignore` â€” `dist/`, `build/`, `__pycache__/`, `*.egg-info/`, `.venv/`
- [x] `uv build` produces sdist + wheel
- [x] Wheel contents: exactly `selfupdate.py` + dist-info â€” no tests,
      no README-as-code, no `__pycache__`
- [x] sdist includes `selfupdate.py`, `pyproject.toml`, `README.md`, `LICENSE`
- [x] `uvx twine check dist/*` passes (metadata renders on PyPI)

## 3. Pre-publish verification

- [x] `python test_selfupdate.py` â€” 39/39 green
- [x] Install the built wheel into a scratch venv
- [x] In the scratch venv: `import selfupdate` works, and
      `importlib.metadata.version("auto-self-update")` returns the version
      (this is exactly what the module's own `_installed_version` uses â€”
      a mismatch here silently disables the feature's version read)
- [x] Run the test suite once against the *installed* copy, not the
      repo file â€” 39/39 green

## 4. First upload (one-time)

- [ ] PyPI account: create an API token scoped to the `self-update`
      project (after the first upload creates it; before that, an
      account-wide token)
- [ ] Optional dry run: TestPyPI (`uv publish --publish-url
      https://test.pypi.org/legacy/`), install from TestPyPI once
- [ ] `uv publish` (or `twine upload dist/*`) with the token
- [ ] Verify on pypi.org/project/auto-self-update: description renders,
      classifiers correct, links work
- [ ] `pip install auto-self-update` into a fresh venv from real PyPI;
      import smoke test

## 5. Release bookkeeping (every version)

- [ ] Bump `version` in `pyproject.toml`
- [ ] Rebuild, re-check (sections 2â€“3)
- [ ] Commit, tag `v<version>`, push tag
- [ ] `gh release create v<version>` with short notes (optional but cheap)
- [ ] Publish to PyPI
- [ ] Post-publish install check from real PyPI

## Known non-goals

- No CI for v1 (single-file module, test suite runs anywhere in seconds)
- No `__version__` attribute (metadata is the single source)
- No entry points (the module is a library, not a command)
