"""Offline tests for selfupdate.py; run with any Python: python test_selfupdate.py.

Every boundary (index, filesystem markers, process table, installer
subprocess, relaunch) is faked: no network, no real upgrade, no spawn.
The tiny MonkeyPatch/run_suite pair keeps the suite dependency-free.
"""

import inspect
import json
import os
import subprocess
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import selfupdate

_MISSING = object()


class MonkeyPatch:
    """Temporarily override attributes and mapping items; undo() restores."""

    def __init__(self):
        self._undo_stack = []

    def setattr(self, target, name, value):
        previous = getattr(target, name, _MISSING)
        setattr(target, name, value)
        self._undo_stack.append(("attr", target, name, previous))

    def setitem(self, mapping, key, value):
        previous = mapping.get(key, _MISSING)
        mapping[key] = value
        self._undo_stack.append(("item", mapping, key, previous))

    def undo(self):
        for kind, target, key, previous in reversed(self._undo_stack):
            if previous is _MISSING:
                if kind == "attr":
                    delattr(target, key)
                else:
                    del target[key]
            elif kind == "attr":
                setattr(target, key, previous)
            else:
                target[key] = previous
        self._undo_stack.clear()


def run_suite(tests):
    """Run all test functions; exit non-zero when any of them fails."""
    failed = []
    for test in tests:
        patch = MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(test).parameters:
                test(patch)
            else:
                test()
            print(f"PASS {test.__name__}")
        except Exception:
            failed.append(test.__name__)
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
        finally:
            patch.undo()
    if failed:
        print(f"{len(failed)} TEST(S) FAILED: {', '.join(failed)}")
        sys.exit(1)
    print(f"ALL {len(tests)} TESTS PASSED")


PACKAGE = "my-tool"


def test_version_key_orders_releases():
    assert selfupdate._is_newer("2.0.8", "2.0.7")
    assert selfupdate._is_newer("10.0.0", "9.9.9"), "numeric, not lexicographic"
    assert not selfupdate._is_newer("2.0.7", "2.0.7")
    assert not selfupdate._is_newer("2.0.7", "2.0.8")
    assert selfupdate._is_newer("2.0.8", "2.0.8rc1"), "release outranks its rc"


def test_is_stable_release():
    assert selfupdate._is_stable_release("2.0.8")
    assert not selfupdate._is_stable_release("2.0.8rc1")
    assert not selfupdate._is_stable_release("not-a-version")


def test_kill_switch_env_name():
    assert selfupdate._kill_switch_env("my-tool") == "MY_TOOL_NO_AUTO_UPDATE"


def test_skip_reason_global_env(monkeypatch):
    monkeypatch.setitem(os.environ, "NO_AUTO_UPDATE", "1")
    assert selfupdate._skip_reason(PACKAGE, True, None) is not None


def test_skip_reason_package_env(monkeypatch):
    monkeypatch.setitem(os.environ, "MY_TOOL_NO_AUTO_UPDATE", "1")
    assert selfupdate._skip_reason(PACKAGE, True, None) is not None


def test_skip_reason_disabled():
    assert selfupdate._skip_reason(PACKAGE, False, None) is not None


def test_checkout_detection():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        module = root / "src" / "mytool"
        module.mkdir(parents=True)
        (root / "pyproject.toml").write_text("", encoding="utf-8")
        caller = module / "cli.py"
        caller.write_text("", encoding="utf-8")
        assert selfupdate._checkout_root(str(caller)) == root


def test_installed_layout_is_not_a_checkout():
    with tempfile.TemporaryDirectory() as temp_dir:
        site = Path(temp_dir) / "lib" / "site-packages" / "mytool"
        site.mkdir(parents=True)
        (Path(temp_dir) / "pyproject.toml").write_text("", encoding="utf-8")
        caller = site / "cli.py"
        caller.write_text("", encoding="utf-8")
        assert selfupdate._checkout_root(str(caller)) is None, (
            "the walk must stop at the site-packages boundary"
        )


def test_checkout_detection_no_caller_file():
    assert selfupdate._checkout_root(None) is None


def test_latest_version_parses_index(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"info": {"version": "2.0.8"}}).encode()

    monkeypatch.setattr(
        selfupdate.urllib.request, "urlopen", lambda url, timeout: FakeResponse()
    )
    assert selfupdate._latest_version(PACKAGE, None) == "2.0.8"


def test_latest_version_rejects_prerelease(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"info": {"version": "2.1.0rc1"}}).encode()

    monkeypatch.setattr(
        selfupdate.urllib.request, "urlopen", lambda url, timeout: FakeResponse()
    )
    assert selfupdate._latest_version(PACKAGE, None) is None


def test_latest_version_network_failure(monkeypatch):
    def boom(url, timeout):
        raise OSError("no network")

    monkeypatch.setattr(selfupdate.urllib.request, "urlopen", boom)
    assert selfupdate._latest_version(PACKAGE, None) is None


def test_ephemeral_note():
    uvx = Path("/home/u/.cache/uv/environments-v2/tool-abc")
    assert selfupdate._ephemeral_note(uvx) == "uvx"
    pipx_run = Path("/home/u/.local/share/pipx/cache/tool-abc")
    assert selfupdate._ephemeral_note(pipx_run) == "pipx run"
    assert selfupdate._ephemeral_note(Path("/home/u/.venv")) is None


def _prefix_with(monkeypatch, marker: str | None):
    temp_dir = tempfile.TemporaryDirectory()
    prefix = Path(temp_dir.name)
    if marker:
        (prefix / marker).write_text("", encoding="utf-8")
    monkeypatch.setattr(selfupdate.sys, "prefix", str(prefix))
    return temp_dir


def test_upgrade_command_uv_tool(monkeypatch):
    holder = _prefix_with(monkeypatch, "uv-receipt.toml")
    with holder:
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
        assert selfupdate._upgrade_command(PACKAGE) == [
            "/bin/uv",
            "tool",
            "upgrade",
            PACKAGE,
        ]


def test_upgrade_command_pipx(monkeypatch):
    holder = _prefix_with(monkeypatch, "pipx_metadata.json")
    with holder:
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: f"/bin/{name}")
        assert selfupdate._upgrade_command(PACKAGE) == [
            "/bin/pipx",
            "upgrade",
            PACKAGE,
        ]


def test_upgrade_command_pipx_falls_through_without_binary(monkeypatch):
    holder = _prefix_with(monkeypatch, "pipx_metadata.json")
    with holder:
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: True)
        command = selfupdate._upgrade_command(PACKAGE)
        assert command[1:] == ["-m", "pip", "install", "--upgrade", PACKAGE]


def test_upgrade_command_plain_pip(monkeypatch):
    holder = _prefix_with(monkeypatch, None)
    with holder:
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: True)
        assert selfupdate._upgrade_command(PACKAGE) == [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            PACKAGE,
        ]


def test_upgrade_command_uv_pip_last_resort(monkeypatch):
    holder = _prefix_with(monkeypatch, None)
    with holder:
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: False)
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: "/bin/uv")
        assert selfupdate._upgrade_command(PACKAGE) == [
            "/bin/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--upgrade",
            PACKAGE,
        ]


def test_upgrade_command_none(monkeypatch):
    holder = _prefix_with(monkeypatch, None)
    with holder:
        monkeypatch.setattr(selfupdate, "_pip_available", lambda: False)
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
        assert selfupdate._upgrade_command(PACKAGE) is None


def _update_available(monkeypatch):
    monkeypatch.setattr(selfupdate, "_skip_reason", lambda *a: None)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda p: "1.0.0")
    monkeypatch.setattr(selfupdate, "_latest_version", lambda p, u: "1.1.0")
    monkeypatch.setattr(selfupdate, "_ephemeral_note", lambda prefix: None)


def test_check_happy_path(monkeypatch):
    _update_available(monkeypatch)
    spawned = []
    monkeypatch.setattr(selfupdate, "_upgrade_command", lambda p: ["fake", "upgrade"])
    monkeypatch.setattr(
        selfupdate, "_spawn_updater", lambda payload: spawned.append(payload) or True
    )
    monkeypatch.setattr(selfupdate, "relaunch_argv", lambda name: ["shim"])
    out = StringIO()
    with redirect_stdout(out):
        assert selfupdate.check(PACKAGE) is True
    payload = spawned[0]
    assert payload["command"] == ["fake", "upgrade"]
    assert payload["argv"] == ["shim"]
    assert payload["previous"] == "1.0.0"
    assert payload["package"] == PACKAGE
    assert "1.0.0 -> 1.1.0" in out.getvalue()


def test_check_up_to_date(monkeypatch):
    monkeypatch.setattr(selfupdate, "_skip_reason", lambda *a: None)
    monkeypatch.setattr(selfupdate, "_installed_version", lambda p: "1.1.0")
    monkeypatch.setattr(selfupdate, "_latest_version", lambda p, u: "1.1.0")
    assert selfupdate.check(PACKAGE) is False


def test_check_ephemeral_note(monkeypatch):
    _update_available(monkeypatch)
    monkeypatch.setattr(selfupdate, "_ephemeral_note", lambda prefix: "uvx")
    out = StringIO()
    with redirect_stdout(out):
        assert selfupdate.check(PACKAGE) is False
    assert "uvx" in out.getvalue()


def test_check_no_installer(monkeypatch):
    _update_available(monkeypatch)
    monkeypatch.setattr(selfupdate, "_upgrade_command", lambda p: None)
    out = StringIO()
    with redirect_stdout(out):
        assert selfupdate.check(PACKAGE) is False
    assert f"pip install --upgrade {PACKAGE}" in out.getvalue()


def test_check_spawn_failure(monkeypatch):
    _update_available(monkeypatch)
    monkeypatch.setattr(selfupdate, "_upgrade_command", lambda p: ["fake"])
    monkeypatch.setattr(selfupdate, "_spawn_updater", lambda payload: False)
    monkeypatch.setattr(selfupdate, "relaunch_argv", lambda name: ["shim"])
    out = StringIO()
    with redirect_stdout(out):
        assert selfupdate.check(PACKAGE) is False
    assert "manually" in out.getvalue()


def test_check_never_raises(monkeypatch):
    def boom(*a):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(selfupdate, "_skip_reason", boom)
    assert selfupdate.check(PACKAGE) is False


def test_spawn_updater_detaches_from_terminal_signals(monkeypatch):
    captured = []

    class FakePopen:
        def __init__(self, args, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(selfupdate.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(selfupdate.sys, "platform", "linux")
    assert selfupdate._spawn_updater({"pid": 1})
    assert captured[-1].get("start_new_session") is True
    monkeypatch.setattr(selfupdate.sys, "platform", "win32")
    assert selfupdate._spawn_updater({"pid": 1})
    assert captured[-1].get("creationflags", 0) & subprocess.CREATE_NEW_PROCESS_GROUP


def test_spawn_updater_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(selfupdate.subprocess, "Popen", boom)
    assert selfupdate._spawn_updater({"pid": 1}) is False


def test_updater_path_prefers_the_module_file():
    invocation = selfupdate._updater_path()
    assert invocation == [str(Path(selfupdate.__file__).resolve())], (
        "running by file path keeps the host package out of the updater"
    )


def test_wait_for_parent_returns_when_gone(monkeypatch):
    states = iter([True, True, False])
    monkeypatch.setattr(selfupdate, "_pid_alive", lambda pid: next(states, False))
    selfupdate._wait_for_parent(1234)


def test_wait_for_parent_times_out(monkeypatch):
    monkeypatch.setattr(selfupdate, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(selfupdate.time, "sleep", lambda s: None)
    selfupdate._wait_for_parent(1234, timeout_s=0.05)


def _updater_patches(monkeypatch, returncode=0, installed="1.1.0"):
    monkeypatch.setattr(selfupdate, "_wait_for_parent", lambda pid: None)
    monkeypatch.setattr(
        selfupdate.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=returncode),
    )
    monkeypatch.setattr(selfupdate, "_installed_version", lambda p: installed)
    relaunched = []
    monkeypatch.setattr(selfupdate, "_relaunch", relaunched.append)
    return relaunched


def _payload(**overrides):
    payload = {
        "pid": 4321,
        "package": PACKAGE,
        "command": ["fake", "upgrade"],
        "argv": ["shim"],
        "previous": "1.0.0",
    }
    payload.update(overrides)
    return payload


def test_updater_main_success(monkeypatch):
    relaunched = _updater_patches(monkeypatch)
    out = StringIO()
    with redirect_stdout(out):
        code = selfupdate._updater_main(["updater", json.dumps(_payload())])
    assert code == 0
    assert relaunched == [["shim"]]
    assert "relaunching" in out.getvalue()


def test_updater_main_newer_release_mid_update(monkeypatch):
    relaunched = _updater_patches(monkeypatch, installed="1.2.0")
    out = StringIO()
    with redirect_stdout(out):
        code = selfupdate._updater_main(["updater", json.dumps(_payload())])
    assert code == 0
    assert relaunched == [["shim"]], (
        "a release published between check and upgrade is still a success"
    )


def test_updater_main_upgrade_failure(monkeypatch):
    relaunched = _updater_patches(monkeypatch, returncode=1)
    out = StringIO()
    with redirect_stdout(out):
        code = selfupdate._updater_main(["updater", json.dumps(_payload())])
    assert code == 1
    assert relaunched == [], "a failed upgrade never relaunches"
    assert "manually" in out.getvalue()


def test_updater_main_command_cannot_run(monkeypatch):
    monkeypatch.setattr(selfupdate, "_wait_for_parent", lambda pid: None)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=1)

    monkeypatch.setattr(selfupdate.subprocess, "run", boom)
    relaunched = []
    monkeypatch.setattr(selfupdate, "_relaunch", relaunched.append)
    assert selfupdate._updater_main(["updater", json.dumps(_payload())]) == 1
    assert relaunched == []


def test_updater_main_version_did_not_advance(monkeypatch):
    relaunched = _updater_patches(monkeypatch, installed="1.0.0")
    code = selfupdate._updater_main(["updater", json.dumps(_payload())])
    assert code == 1
    assert relaunched == [], "a version that did not advance never relaunches"


def test_updater_main_bad_payload():
    assert selfupdate._updater_main(["updater"]) == 2
    assert selfupdate._updater_main(["updater", "not json"]) == 2
    assert selfupdate._updater_main(["updater", json.dumps({"pid": 1})]) == 2
    assert selfupdate._updater_main(["updater", json.dumps(["not", "a", "dict"])]) == 2


def test_relaunch_argv_prefers_the_running_shim():
    with tempfile.TemporaryDirectory() as temp_dir:
        shim = Path(temp_dir) / "my-tool.exe"
        shim.write_text("", encoding="utf-8")
        argv = selfupdate.relaunch_argv("my-tool", [str(shim), "--flag"])
        assert argv == [str(shim), "--flag"]


def test_relaunch_argv_script_goes_through_the_interpreter():
    with tempfile.TemporaryDirectory() as temp_dir:
        script = Path(temp_dir) / "my-tool"
        script.write_text("", encoding="utf-8")
        argv = selfupdate.relaunch_argv("my-tool", [str(script)])
        assert argv == [sys.executable, str(script)]


def test_relaunch_argv_falls_back_to_path_shim(monkeypatch):
    with tempfile.TemporaryDirectory() as temp_dir:
        shim = Path(temp_dir) / "my-tool"
        monkeypatch.setattr(selfupdate.shutil, "which", lambda name: str(shim))
        argv = selfupdate.relaunch_argv("my-tool", ["nonexistent-name"])
        assert argv == [str(shim)]


def main():
    """Run every test and report a summary."""
    run_suite(
        [
            test_version_key_orders_releases,
            test_is_stable_release,
            test_kill_switch_env_name,
            test_skip_reason_global_env,
            test_skip_reason_package_env,
            test_skip_reason_disabled,
            test_checkout_detection,
            test_installed_layout_is_not_a_checkout,
            test_checkout_detection_no_caller_file,
            test_latest_version_parses_index,
            test_latest_version_rejects_prerelease,
            test_latest_version_network_failure,
            test_ephemeral_note,
            test_upgrade_command_uv_tool,
            test_upgrade_command_pipx,
            test_upgrade_command_pipx_falls_through_without_binary,
            test_upgrade_command_plain_pip,
            test_upgrade_command_uv_pip_last_resort,
            test_upgrade_command_none,
            test_check_happy_path,
            test_check_up_to_date,
            test_check_ephemeral_note,
            test_check_no_installer,
            test_check_spawn_failure,
            test_check_never_raises,
            test_spawn_updater_detaches_from_terminal_signals,
            test_spawn_updater_failure,
            test_updater_path_prefers_the_module_file,
            test_wait_for_parent_returns_when_gone,
            test_wait_for_parent_times_out,
            test_updater_main_success,
            test_updater_main_newer_release_mid_update,
            test_updater_main_upgrade_failure,
            test_updater_main_command_cannot_run,
            test_updater_main_version_did_not_advance,
            test_updater_main_bad_payload,
            test_relaunch_argv_prefers_the_running_shim,
            test_relaunch_argv_script_goes_through_the_interpreter,
            test_relaunch_argv_falls_back_to_path_shim,
        ]
    )


if __name__ == "__main__":
    main()
