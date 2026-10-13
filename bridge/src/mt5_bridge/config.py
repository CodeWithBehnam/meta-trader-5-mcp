"""Bridge configuration read from environment variables. Pure given a mapping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_PORT = 18812
DEFAULT_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
DEFAULT_INIT_TIMEOUT_MS = 60_000
# The bridge never binds anything but loopback; it is not configurable on purpose.
HOST = "127.0.0.1"


class ConfigError(ValueError):
    """Raised at start-up when the environment is unusable."""


@dataclass(frozen=True)
class Config:
    token: str
    port: int = DEFAULT_PORT
    allow_trading: bool = False
    terminal_path: str = DEFAULT_TERMINAL_PATH
    init_timeout_ms: int = DEFAULT_INIT_TIMEOUT_MS

    def __repr__(self) -> str:
        # Keep the token out of logs and tracebacks.
        return (
            f"Config(port={self.port}, allow_trading={self.allow_trading}, "
            f"terminal_path={self.terminal_path!r}, init_timeout_ms={self.init_timeout_ms})"
        )


def _int_env(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def load_config(env: Mapping[str, str]) -> Config:
    token = env.get("MT5_BRIDGE_TOKEN", "")
    if not token:
        raise ConfigError("MT5_BRIDGE_TOKEN is not set; refusing to start without auth")
    return Config(
        token=token,
        port=_int_env(env, "MT5_BRIDGE_PORT", DEFAULT_PORT, 1, 65535),
        allow_trading=env.get("MT5_BRIDGE_ALLOW_TRADING", "") == "1",
        terminal_path=env.get("MT5_TERMINAL_PATH") or DEFAULT_TERMINAL_PATH,
        init_timeout_ms=_int_env(
            env, "MT5_BRIDGE_INIT_TIMEOUT_MS", DEFAULT_INIT_TIMEOUT_MS, 1000, 600_000
        ),
    )
