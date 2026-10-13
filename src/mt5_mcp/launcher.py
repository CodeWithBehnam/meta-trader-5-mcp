"""Bridge token management and auto-start (the I/O shell around pure launch specs)."""

from __future__ import annotations

import logging
import os
import secrets
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Protocol

from .bridge_client import (
    BridgeAuthError,
    BridgeClient,
    BridgeError,
    BridgeUnavailable,
)
from .config import Config

logger = logging.getLogger(__name__)


class LauncherError(RuntimeError):
    """The bridge could not be made available. The message is meant for the user."""


class TokenMismatchError(LauncherError):
    """A bridge is listening but was started with a different token."""


class TerminalNotRunningError(LauncherError):
    """The MetaTrader 5 terminal is not running."""


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    env: Mapping[str, str]


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass
class LaunchState:
    """The bridge process this instance spawned, kept so a slow start is not spawned twice.

    Mutable on purpose: it tracks a real process lifecycle across ``ensure_bridge`` calls.
    """

    process: ProcessHandle | None = None


Spawner = Callable[[LaunchSpec, Path], ProcessHandle]
TerminalProbe = Callable[[], bool]
Terminator = Callable[[ProcessHandle], None]
TERMINATE_GRACE_SECONDS = 5.0


def _read_token(path: Path, *, retries: int, delay: float, sleep: Callable[[float], None]) -> str:
    """Read the token, waiting briefly if another process has created but not filled the file."""
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)
    for attempt in range(retries + 1):
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
        if attempt < retries:
            sleep(delay)
    raise LauncherError(f"Token file {path} is empty; delete it to generate a new token")


def _create_token(path: Path) -> str:
    """Publish a new token atomically: write a private temp file, then hard-link it into place.

    ``os.link`` fails if ``path`` exists, so a concurrent creator cannot be overwritten and
    readers never observe a half-written file.
    """
    token = secrets.token_urlsafe(32)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # mode 0600
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return token


def ensure_token(
    path: Path,
    *,
    retries: int = 5,
    delay: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return the shared bridge token, creating the file (mode 0600) if missing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _read_token(path, retries=retries, delay=delay, sleep=sleep)
    except FileNotFoundError:
        pass
    try:
        return _create_token(path)
    except FileExistsError:
        # Another instance won the race; use its token.
        return _read_token(path, retries=retries, delay=delay, sleep=sleep)


def build_launch_spec(config: Config, token: str, base_env: Mapping[str, str]) -> LaunchSpec:
    """Pure: the argv and environment used to start the bridge under Wine."""
    bridge_dir = PureWindowsPath(config.bridge_dir)
    env = dict(base_env)
    env.update(
        {
            "WINEPREFIX": str(config.wineprefix),
            # winedbg.exe=d stops the Wine crash dialog from hanging a crashed bridge forever.
            "WINEDLLOVERRIDES": "mscoree=;winedbg.exe=d",
            "WINEDEBUG": "-all",
            "DYLD_FALLBACK_LIBRARY_PATH": str(config.wine_external_lib),
            "UV_PYTHON_INSTALL_DIR": str(bridge_dir / "python"),
            "UV_CACHE_DIR": str(bridge_dir / "cache"),
            "UV_PROJECT_ENVIRONMENT": str(bridge_dir / "venv"),
            "UV_PYTHON_DOWNLOADS": "never",
            "PYTHONUNBUFFERED": "1",
            "MT5_BRIDGE_PORT": str(config.bridge_port),
            "MT5_BRIDGE_TOKEN": token,
            "MT5_BRIDGE_ALLOW_TRADING": "1" if config.enable_trading else "0",
            "MT5_TERMINAL_PATH": config.terminal_path,
        }
    )
    argv = (
        str(config.wine_binary),
        config.uv_exe,
        "run",
        "--frozen",
        "--no-dev",
        "--project",
        config.bridge_project,
        "python",
        "-m",
        "mt5_bridge",
    )
    return LaunchSpec(argv=argv, env=env)


def terminal_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "terminal64.exe"], capture_output=True, check=False, timeout=10
        )
    except subprocess.TimeoutExpired as exc:
        raise LauncherError(
            "Could not check whether MetaTrader 5 is running: pgrep timed out after 10s. "
            "Retry, or start the bridge manually (scripts/run-bridge.sh)."
        ) from exc
    return result.returncode == 0


def terminate_process_group(process: ProcessHandle) -> None:
    """Stop a bridge started with ``start_new_session``: SIGTERM its group, then SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            logger.warning("Bridge process group %s ignored %s", process.pid, sig.name)


def spawn_detached(spec: LaunchSpec, log_path: Path) -> ProcessHandle:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        return subprocess.Popen(
            list(spec.argv),
            env=dict(spec.env),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


BRIDGE_EXIT_HINTS: dict[int, str] = {
    2: "missing token or bad configuration",
    3: "MetaTrader5 package import failure",
}


def _exit_hint(exit_code: int) -> str:
    hint = BRIDGE_EXIT_HINTS.get(exit_code)
    return f" ({hint})" if hint else ""


def tail_log(path: Path, lines: int = 8) -> str:
    """Last lines of the bridge log, for error messages; empty if unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return f"\nLast log lines:\n{tail}" if tail else ""


def _mismatch_error(config: Config) -> TokenMismatchError:
    return TokenMismatchError(
        f"A bridge is already listening on {config.bridge_host}:{config.bridge_port} but "
        f"rejected the token in {config.token_file}. It was probably started by another "
        "instance with a different token. Stop it, or set MT5_BRIDGE_PORT to a free port."
    )


def ensure_bridge(
    config: Config,
    token: str,
    *,
    make_client: Callable[[], BridgeClient],
    state: LaunchState | None = None,
    spawn: Spawner = spawn_detached,
    probe_terminal: TerminalProbe = terminal_running,
    terminate: Terminator = terminate_process_group,
    base_env: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    poll_interval: float = 1.0,
) -> None:
    """Make sure a bridge answers ``ping``, starting one if allowed and necessary."""
    if _ping_ok(make_client, config):
        return
    if not config.bridge_autostart:
        raise LauncherError(
            f"Bridge is not running on {config.bridge_host}:{config.bridge_port} and "
            "MT5_BRIDGE_AUTOSTART=0. Start it manually (scripts/run-bridge.sh)."
        )
    launch = LaunchState() if state is None else state
    if launch.process is not None and launch.process.poll() is not None:
        launch.process = None  # it exited since we last looked
    if launch.process is None:
        if not probe_terminal():
            raise TerminalNotRunningError(
                "MetaTrader 5 is not running. Open the MetaTrader 5 app and log in, then retry."
            )
        spec = build_launch_spec(config, token, os.environ if base_env is None else base_env)
        logger.info("Starting bridge: %s", " ".join(spec.argv))
        launch.process = spawn(spec, config.bridge_log)
    else:
        logger.info("Bridge process %s is still starting; waiting for it", launch.process.pid)
    process = launch.process
    deadline = monotonic() + config.ready_timeout
    while monotonic() < deadline:
        if _ping_ok(make_client, config):
            return
        exit_code = process.poll()
        if exit_code is not None:
            launch.process = None
            raise LauncherError(
                f"Bridge process exited with code {exit_code}{_exit_hint(exit_code)} before "
                f"becoming ready. See {config.bridge_log}.{tail_log(config.bridge_log)}"
            )
        sleep(poll_interval)
    # Leaving it running would let the next call spawn a second bridge on the same port.
    terminate(process)
    launch.process = None
    raise LauncherError(
        f"Bridge did not answer ping within {config.ready_timeout:.0f}s; the process was "
        f"terminated. See {config.bridge_log}.{tail_log(config.bridge_log)}"
    )


def _ping_ok(make_client: Callable[[], BridgeClient], config: Config) -> bool:
    client = make_client()
    try:
        client.call("ping", timeout=5.0)
    except BridgeAuthError:
        raise _mismatch_error(config) from None
    except BridgeUnavailable:
        return False
    except BridgeError as exc:
        raise LauncherError(f"Bridge answered ping with an error: {exc}") from exc
    finally:
        client.close()
    return True
