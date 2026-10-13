import stat
import subprocess
import threading
from pathlib import Path

import pytest

from mt5_mcp import launcher
from mt5_mcp.bridge_client import BridgeClient
from mt5_mcp.config import Config
from mt5_mcp.launcher import LaunchSpec

from .conftest import TOKEN, FakeBridge, make_config


def test_ensure_token_creates_private_file(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "bridge.token"
    token = launcher.ensure_token(path)
    assert len(token) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert launcher.ensure_token(path) == token


def test_ensure_token_tightens_permissions_and_rejects_empty(tmp_path: Path) -> None:
    path = tmp_path / "t"
    path.write_text("abc\n")
    path.chmod(0o644)
    assert launcher.ensure_token(path) == "abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("\n")
    with pytest.raises(launcher.LauncherError):
        launcher.ensure_token(path)


def test_build_launch_spec(tmp_path: Path) -> None:
    config = make_config(tmp_path, 18812, MT5_ENABLE_TRADING="1", MT5_BRIDGE_PROJECT="Z:\\proj")
    spec = launcher.build_launch_spec(config, "sekret", {"PATH": "/usr/bin"})
    assert spec.argv == (
        "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64",
        "C:\\mt5-bridge\\bin\\uv.exe",
        "run",
        "--frozen",
        "--no-dev",
        "--project",
        "Z:\\proj",
        "python",
        "-m",
        "mt5_bridge",
    )
    env = spec.env
    assert env["PATH"] == "/usr/bin"
    assert env["MT5_BRIDGE_TOKEN"] == "sekret"
    assert env["MT5_BRIDGE_ALLOW_TRADING"] == "1"
    assert env["MT5_BRIDGE_PORT"] == "18812"
    assert env["UV_PROJECT_ENVIRONMENT"] == "C:\\mt5-bridge\\venv"
    assert env["UV_PYTHON_INSTALL_DIR"] == "C:\\mt5-bridge\\python"
    assert env["UV_CACHE_DIR"] == "C:\\mt5-bridge\\cache"
    assert env["UV_PYTHON_DOWNLOADS"] == "never"
    assert env["WINEDLLOVERRIDES"] == "mscoree=;winedbg.exe=d" and env["WINEDEBUG"] == "-all"
    assert env["DYLD_FALLBACK_LIBRARY_PATH"].endswith("wine/lib/external")
    assert env["WINEPREFIX"] == str(config.wineprefix)
    assert env["MT5_TERMINAL_PATH"] == config.terminal_path


def test_trading_flag_off_by_default(tmp_path: Path) -> None:
    spec = launcher.build_launch_spec(make_config(tmp_path, 1), "t", {})
    assert spec.env["MT5_BRIDGE_ALLOW_TRADING"] == "0"


class FakeProcess:
    def __init__(self, exit_code: int | None = None, pid: int = 4242) -> None:
        self.exit_code = exit_code
        self.pid = pid
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code or 0


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def run_ensure(config: Config, **kwargs):
    clock = Clock()
    # Never let a test signal a real process group.
    kwargs.setdefault("terminate", lambda _p: None)
    return launcher.ensure_bridge(
        config,
        TOKEN,
        make_client=lambda: BridgeClient(
            config.bridge_host, config.bridge_port, TOKEN, connect_timeout=0.3
        ),
        base_env={},
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )


def never_spawn(_spec: LaunchSpec, _log: Path) -> FakeProcess:
    raise AssertionError("must not spawn")


def test_ensure_returns_when_bridge_already_up(bridge: FakeBridge, config: Config) -> None:
    run_ensure(config, spawn=never_spawn, probe_terminal=lambda: False)


def test_ensure_detects_token_mismatch(bridge: FakeBridge, config: Config) -> None:
    bridge.token = "someone-else"
    with pytest.raises(launcher.TokenMismatchError, match="different token"):
        run_ensure(config, spawn=never_spawn, probe_terminal=lambda: True)


def test_ensure_autostart_off(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="0")
    with pytest.raises(launcher.LauncherError, match="AUTOSTART"):
        run_ensure(config, spawn=never_spawn, probe_terminal=lambda: True)


def test_ensure_requires_terminal(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="1")
    with pytest.raises(launcher.TerminalNotRunningError, match="Open the MetaTrader 5 app"):
        run_ensure(config, spawn=never_spawn, probe_terminal=lambda: False)


def test_ensure_spawns_and_waits_for_ping(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="1")
    spawned: list[LaunchSpec] = []
    holder: list[FakeBridge] = []

    def spawn(spec: LaunchSpec, log: Path) -> FakeProcess:
        spawned.append(spec)
        fake = FakeBridge(port=free_port)  # the "bridge" listens only once launched
        fake.on("ping", {"bridge_version": "x"})
        fake.start()
        holder.append(fake)
        return FakeProcess()

    run_ensure(config, spawn=spawn, probe_terminal=lambda: True)
    holder[0].stop()
    assert len(spawned) == 1 and spawned[0].env["MT5_BRIDGE_TOKEN"] == TOKEN


def test_ensure_reports_early_exit_and_timeout(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="1")
    with pytest.raises(
        launcher.LauncherError, match=r"exited with code 3 \(MetaTrader5 package import failure\)"
    ):
        run_ensure(config, spawn=lambda s, p: FakeProcess(3), probe_terminal=lambda: True)
    with pytest.raises(launcher.LauncherError, match="did not answer ping within 90s"):
        run_ensure(config, spawn=lambda s, p: FakeProcess(None), probe_terminal=lambda: True)


def test_timeout_terminates_the_spawned_process(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="1")
    stopped: list[FakeProcess] = []
    process = FakeProcess(None)
    state = launcher.LaunchState()
    with pytest.raises(launcher.LauncherError, match="terminated"):
        run_ensure(
            config,
            spawn=lambda s, p: process,
            probe_terminal=lambda: True,
            state=state,
            terminate=lambda p: stopped.append(p),
        )
    assert stopped == [process] and state.process is None


def test_slow_starting_bridge_is_not_spawned_twice(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="1")
    process = FakeProcess(None)
    state = launcher.LaunchState(process=process)
    holder: list[FakeBridge] = []

    def becomes_ready() -> bool:
        fake = FakeBridge(port=free_port)
        fake.on("ping", {"bridge_version": "x"})
        fake.start()
        holder.append(fake)
        return True

    # A previous call spawned a process that is still alive: only wait for it.
    sleeps = {"n": 0}

    def sleep(_s: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 2:
            becomes_ready()

    clock = Clock()
    launcher.ensure_bridge(
        config,
        TOKEN,
        make_client=lambda: BridgeClient(
            config.bridge_host, config.bridge_port, TOKEN, connect_timeout=0.3
        ),
        state=state,
        spawn=never_spawn,
        probe_terminal=lambda: pytest.fail("must not probe while a process is starting"),
        base_env={},
        sleep=lambda s: (clock.sleep(s), sleep(s))[1],
        monotonic=clock.monotonic,
    )
    holder[0].stop()
    assert state.process is process


def test_exited_process_is_replaced(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port, MT5_BRIDGE_AUTOSTART="1")
    state = launcher.LaunchState(process=FakeProcess(1))
    fresh = FakeProcess(5)
    with pytest.raises(launcher.LauncherError, match="exited with code 5"):
        run_ensure(config, spawn=lambda s, p: fresh, probe_terminal=lambda: True, state=state)
    assert state.process is None


def test_pgrep_timeout_is_a_launcher_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("pgrep", 10)

    monkeypatch.setattr(launcher.subprocess, "run", hang)
    with pytest.raises(launcher.LauncherError, match="pgrep timed out"):
        launcher.terminal_running()


def test_ensure_token_is_race_free(tmp_path: Path) -> None:
    path = tmp_path / "t" / "bridge.token"
    barrier = threading.Barrier(16)
    results: list[str] = []

    def worker() -> None:
        barrier.wait()
        results.append(launcher.ensure_token(path))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 16 and len(set(results)) == 1
    assert path.read_text().strip() == results[0]
    assert sorted(p.name for p in path.parent.iterdir()) == ["bridge.token"]


def test_ensure_token_waits_for_a_file_that_is_being_filled(tmp_path: Path) -> None:
    path = tmp_path / "bridge.token"
    path.write_text("")
    path.chmod(0o600)
    calls = {"n": 0}

    def sleep(_s: float) -> None:
        calls["n"] += 1
        path.write_text("late-token\n")

    assert launcher.ensure_token(path, sleep=sleep) == "late-token"
    assert calls["n"] == 1


def test_ensure_token_uses_winner_when_creation_loses_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bridge.token"

    def lose(_tmp: object, _dest: object) -> None:
        path.write_text("winner\n")
        path.chmod(0o600)
        raise FileExistsError

    monkeypatch.setattr(launcher.os, "link", lose)
    assert launcher.ensure_token(path) == "winner"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bridge.token"]
