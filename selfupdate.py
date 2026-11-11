"""Drop-in launch-time self-update for pip, pipx, and uv-installed CLIs.

Vendor this single file into any project (it has no dependencies beyond
the standard library) and add two lines at the very top of the entry
point, before any setup, try/finally, or UI:

    from . import selfupdate

    def main():
        if selfupdate.check("my-package"):
            return  # the updater is running; it relaunches this command

How it works: `check` compares the installed version against the newest
stable release on PyPI (hard timeout, fails open). A newer release
spawns a detached updater process and the caller must exit immediately:
Windows locks a running process's executables, so exiting before
upgrading is the one flow that works identically on every platform. The
updater waits for the caller to disappear, runs the installer-native
upgrade command (uv receipt -> `uv tool upgrade`, pipx metadata ->
`pipx upgrade`, else the environment's own pip, else `uv pip`), verifies
the installed version actually advanced, and re-executes the original
command with its arguments. Any failure prints the manual command and
leaves the old installation untouched.

Skipped automatically: repository checkouts and editable installs
(detected by walking up from the caller's file), ephemeral `uvx` /
`pipx run` caches (a one-line note is printed instead), and any failure.
Kill switches: `enabled=False`, or the environment variables
`NO_AUTO_UPDATE` and `<PACKAGE>_NO_AUTO_UPDATE` (uppercased,
non-alphanumerics replaced by underscores).

Keep the host package's `__init__` light: the updater runs this module
by file path precisely so it never imports the host package, and the
launch-time check imports nothing beyond the standard library.
"""

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from importlib import metadata
from pathlib import Path

logger = logging.getLogger("selfupdate")

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
CHECK_TIMEOUT_S = 4.0
UPGRADE_TIMEOUT_S = 180
PARENT_WAIT_TIMEOUT_S = 30.0
EXECUTABLE_SUFFIXES = (".exe", ".bat", ".cmd")


def check(
    package: str,
    *,
    enabled: bool = True,
    index_url: str | None = None,
) -> bool:
    """Update from PyPI when a newer stable release exists.

    `package` is the PyPI/distribution name. `enabled` lets the host
    project wire its own configuration toggle. `index_url` overrides the
    JSON index URL template (must contain `{package}`); PyPI is the
    default.

    Returns True only when an updater was spawned: the caller must then
    exit immediately, before touching its normal work. Every other
    outcome (gated, up to date, check failed, no installer) returns
    False and the launch proceeds. This function never raises.
    """
    try:
        caller_file = sys._getframe(1).f_globals.get("__file__")
        skip = _skip_reason(package, enabled, caller_file)
        if skip:
            logger.debug("Self-update skipped: %s", skip)
            return False
        installed = _installed_version(package)
        if not installed:
            return False
        latest = _latest_version(package, index_url)
        if not latest or not _is_newer(latest, installed):
            return False
        runner = _ephemeral_note(Path(sys.prefix))
        if runner:
            print(
                f"\n  + {package} {latest} is available (this {runner} run uses"
                " a cached copy that refreshes on its own schedule).\n",
                flush=True,
            )
            return False
        command = _upgrade_command(package)
        if command is None:
            print(
                f"\n  ! {package} {latest} is available, but no supported"
                f" installer was found.\n    Update manually with:"
                f" pip install --upgrade {package}\n",
                flush=True,
            )
            return False
        payload = {
            "pid": os.getpid(),
            "package": package,
            "command": command,
            "argv": relaunch_argv(Path(sys.argv[0]).stem),
            "previous": installed,
        }
        if not _spawn_updater(payload):
            print(
                f"\n  ! Could not start the updater. Update manually with:"
                f" {' '.join(command)}\n",
                flush=True,
            )
            return False
        print(
            f"\n  + Updating {package} {installed} -> {latest};"
            " it will relaunch automatically.\n",
            flush=True,
        )
        return True
    except Exception:
        logger.exception(
            "Self-update check failed; continuing with the installed version"
        )
        return False


def _installed_version(package: str) -> str | None:
    """The installed distribution version, or None outside a real install."""
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def _version_key(version: str) -> tuple:
    """Dotted-numeric sort key; a bare release outranks its prereleases."""
    key = []
    for piece in version.strip().split("."):
        match = re.match(r"^(\d+)(.*)$", piece)
        if match:
            number, suffix = match.group(1), match.group(2)
            key.append((1, int(number), suffix == "", suffix))
        else:
            key.append((0, 0, False, piece))
    return tuple(key)


def _is_stable_release(version: str) -> bool:
    """Only dotted-numeric releases are auto-update targets."""
    return bool(re.fullmatch(r"\d+(\.\d+)*", version.strip()))


def _is_newer(candidate: str, installed: str) -> bool:
    """Whether the candidate strictly outranks the installed version."""
    return _version_key(candidate) > _version_key(installed)


def _latest_version(package: str, index_url: str | None) -> str | None:
    """The newest stable version on the index, or None on any failure.

    The check runs on the launch path, so it fails open and fast: a slow
    or unreachable index must never hold the user's launch hostage.
    """
    url = (index_url or PYPI_JSON_URL).format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=CHECK_TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
        version = data.get("info", {}).get("version")
    except Exception as exc:
        logger.debug("Update check failed: %s", exc)
        return None
    if not isinstance(version, str) or not _is_stable_release(version):
        return None
    return version


def _kill_switch_env(package: str) -> str:
    """The per-package opt-out variable, e.g. MY_TOOL_NO_AUTO_UPDATE."""
    return re.sub(r"[^A-Z0-9]", "_", package.upper()) + "_NO_AUTO_UPDATE"


def _checkout_root(caller_file: str | None) -> Path | None:
    """The repository root when the caller runs from a checkout.

    Walking up from the calling module finds a pyproject.toml within a
    few hops in a source checkout (src or flat layout), while an
    installed package's walk hits its site-packages boundary first.
    Editable installs keep their code in the checkout, so they count as
    checkouts too.
    """
    if not caller_file:
        return None
    path = Path(caller_file).resolve().parent
    for _ in range(4):
        if path.name in ("site-packages", "dist-packages"):
            return None
        if (path / "pyproject.toml").is_file():
            return path
        if path.parent == path:
            return None
        path = path.parent
    return None


def _skip_reason(package: str, enabled: bool, caller_file: str | None) -> str | None:
    """Why the update check must not run, or None when it may."""
    if os.environ.get("NO_AUTO_UPDATE") or os.environ.get(_kill_switch_env(package)):
        return "disabled by environment"
    if not enabled:
        return "disabled by the caller"
    checkout = _checkout_root(caller_file)
    if checkout:
        return f"running from a checkout ({checkout})"
    return None


def _ephemeral_note(prefix: Path) -> str | None:
    """The ephemeral-runner name for a cache-resident prefix, else None."""
    parts = [part.lower() for part in prefix.parts]
    if "uv" in parts and any(part.startswith("environments") for part in parts):
        return "uvx"
    if "pipx" in parts and "cache" in parts:
        return "pipx run"
    return None


def _pip_available() -> bool:
    """Whether this environment can run pip (uv tool venvs ship without it)."""
    return importlib.util.find_spec("pip") is not None


def _upgrade_command(package: str) -> list[str] | None:
    """The installer-native upgrade command for this environment.

    pipx and uv tool installs are detected by the receipt each manager
    writes into the environment; their own upgrade commands replay the
    recorded install options. A plain environment falls back to its pip,
    or to uv pip when it has no pip of its own.
    """
    prefix = Path(sys.prefix)
    if (prefix / "uv-receipt.toml").is_file():
        uv = shutil.which("uv")
        return [uv, "tool", "upgrade", package] if uv else None
    if (prefix / "pipx_metadata.json").is_file():
        pipx = shutil.which("pipx")
        if pipx:
            return [pipx, "upgrade", package]
    if _pip_available():
        return [sys.executable, "-m", "pip", "install", "--upgrade", package]
    uv = shutil.which("uv")
    if uv:
        return [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--upgrade",
            package,
        ]
    return None


def relaunch_argv(entry_point: str, argv: list[str] | None = None) -> list[str]:
    """Build the argv that re-executes the given entry point.

    Preference order: the current entry point (an entry-point executable
    is re-executed directly, a script through the current interpreter),
    the globally installed shim (any PATH entry outside the active
    environment), `uv run` for checkouts without a resolvable entry
    point, and finally the current interpreter with the original
    arguments.
    """
    argv = list(sys.argv if argv is None else argv)
    argv0 = Path(argv[0])
    candidate = argv0 if argv0.is_file() else argv0.with_suffix(".exe")
    if candidate.is_file():
        if candidate.suffix.lower() in EXECUTABLE_SUFFIXES:
            return [str(candidate), *argv[1:]]
        return [sys.executable, str(candidate), *argv[1:]]
    venv_root = Path(sys.prefix).resolve()
    found = shutil.which(entry_point)
    if found and not Path(found).resolve().is_relative_to(venv_root):
        return [found, *argv[1:]]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", entry_point, *argv[1:]]
    return [sys.executable, str(argv0), *argv[1:]]


def _updater_path() -> list[str]:
    """How to execute this module standalone: file path, or -m fallback.

    Running by file path keeps the updater free of the host package's
    import-time cost and failure surface; the -m form covers zipimports
    and frozen apps, where __file__ does not exist on disk.
    """
    module_file = globals().get("__file__")
    if module_file and Path(module_file).is_file():
        return [str(Path(module_file).resolve())]
    spec_name = getattr(globals().get("__spec__"), "name", None) or __name__
    return ["-m", spec_name]


def _spawn_updater(payload: dict) -> bool:
    """Start the detached updater process; False when it cannot start.

    The updater is removed from the terminal's interrupt delivery (its
    own session on POSIX, its own process group on Windows): a Ctrl+C
    meant for the exiting launcher must never reach it mid-upgrade,
    where an interrupted install can leave the environment broken.
    """
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, *_updater_path(), json.dumps(payload)], **kwargs
        )
        return True
    except OSError as exc:
        logger.warning("Could not start the updater: %s", exc)
        return False


def _pid_alive(pid: int) -> bool:
    """Whether a process with this PID exists (best-effort, cross-platform)."""
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_parent(pid: int, timeout_s: float = PARENT_WAIT_TIMEOUT_S) -> None:
    """Block until the launching process exits.

    The upgrade must never touch files a running instance still holds
    open: Windows refuses to replace a live process's executables and
    native modules. A timed-out wait proceeds anyway: the upgrade
    command then fails on its own terms and the updater reports it.
    """
    deadline = time.monotonic() + timeout_s
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def _relaunch(argv: list[str]) -> None:
    """Hand the terminal to a fresh process running the updated version."""
    if sys.platform == "win32":
        sys.exit(subprocess.call(argv))
    os.execv(argv[0], argv)


def _updater_main(argv: list[str]) -> int:
    """Updater entry point: wait for the parent, upgrade, verify, relaunch."""
    try:
        payload = json.loads(argv[1])
        pid = int(payload["pid"])
        package = payload["package"]
        command = payload["command"]
        previous = payload["previous"]
        relaunch = payload["argv"]
    except (IndexError, ValueError, KeyError, TypeError):
        print("  ! Updater called without a valid payload.", flush=True)
        return 2
    _wait_for_parent(pid)
    print(f"  + Running: {' '.join(command)}", flush=True)
    try:
        completed = subprocess.run(command, check=False, timeout=UPGRADE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ! The update could not run: {exc}", flush=True)
        print(f"    Update manually with: pip install --upgrade {package}", flush=True)
        return 1
    if completed.returncode != 0:
        print(
            f"  ! The update command failed (exit {completed.returncode}).", flush=True
        )
        print(f"    Update manually with: pip install --upgrade {package}", flush=True)
        return 1
    installed = _installed_version(package)
    if not installed or not _is_newer(installed, previous):
        print(
            f"  ! The update did not land (still"
            f" {installed or 'unknown'}); not relaunching.",
            flush=True,
        )
        return 1
    print(f"  + Updated to {installed}; relaunching.\n", flush=True)
    _relaunch(relaunch)
    return 0


if __name__ == "__main__":
    sys.exit(_updater_main(sys.argv))
