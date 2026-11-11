# self-update

Single-file, zero-dependency self-update on launch for Python CLIs.
Works on Windows, Linux, and macOS with pip, pipx, and uv
installations.

## Usage

Install it, then add two lines at the very top of your entry point,
**before** any setup, try/finally, or UI:

```
pip install self-update
```

```python
import selfupdate

def main():
    if selfupdate.check("my-package"):
        return  # the updater is running; it relaunches this command
    # ... your normal startup ...
```

`"my-package"` is your app's PyPI/distribution name. That is all.
Alternatively, vendor `selfupdate.py` into your project — it has no
dependencies beyond the standard library.

## What it does

1. On launch, compares the installed version against the newest stable
   release on PyPI (4 s hard timeout; any failure launches normally).
2. A newer release spawns a detached updater and your process exits.
   Exiting first is deliberate: Windows locks a running process's
   executables, and this flow works identically everywhere.
3. The updater waits for your process to disappear, runs the
   installer-native upgrade command (`uv tool upgrade`, `pipx upgrade`,
   or the environment's own pip), verifies the installed version
   actually advanced, and re-executes the original command with its
   arguments.
4. Any failure prints the manual command and leaves the old install
   untouched — the updater never relaunches a failed upgrade.

## Skipped automatically

- **Repository checkouts and editable installs** (found by walking up
  from the caller's file to a `pyproject.toml`; the walk stops at
  `site-packages`).
- **Ephemeral `uvx` / `pipx run` caches** — a one-line note is printed
  instead; those refresh on their own schedule.
- **Kill switches**: pass `enabled=False` (wire your own config toggle
  to it), or set `NO_AUTO_UPDATE` / `<PACKAGE>_NO_AUTO_UPDATE`
  (uppercased, non-alphanumerics become underscores) in the environment.

## Optional parameters

```python
selfupdate.check(
    "my-package",
    enabled=True,          # your own config toggle goes here
    index_url=None,        # JSON index template with {package}; PyPI default
)
```

## Caveats

- Only dotted-numeric releases (`2.0.8`) are update targets;
  pre-releases are never chased.
- The upgrade command inherits the terminal, so its progress output is
  visible; the updater is detached from Ctrl+C delivery so an
  interruption cannot break a half-written install.
- Two instances updating simultaneously serialize on the installer's
  own lock; the second finds nothing to do.
- Keep your package's `__init__.py` light — good practice anyway; the
  updater runs this module by file path and never imports your package.

## Tests

`python test_selfupdate.py` — 39 offline tests, no network, no spawn.

## License

MIT — see [LICENSE](LICENSE).
