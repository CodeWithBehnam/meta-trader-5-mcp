"""Configuration built from environment variables, validated at the boundary.

Safety-related variables (all optional):

* ``MT5_ENABLE_TRADING``: 0 (default) or 1; trading tools exist only when 1.
* ``MT5_ALLOWED_SYMBOLS``: comma-separated allowlist. Unset or empty means no restriction;
  a value with no usable names (for example " " or ",") is an error, never "allow all".
* ``MT5_MAX_VOLUME``: largest lot size per opening order (default 1.0).
* ``MT5_MAX_DEVIATION``: largest accepted slippage in points (default 50, integer >= 0).
  The default deviation of 20 points is lowered to this cap when the cap is smaller.
* ``MT5_AUDIT_LOG``: JSONL audit path; must not be empty or a directory. The directory is
  created 0700 and the file 0600.
* ``MT5_BROKER_UTC_OFFSET_HOURS``: broker server time offset from UTC in hours (a multiple
  of 0.25 within -12..14, for example 3 or 5.5). When set it bypasses the tick based
  estimate; use it when the market is closed or the estimate is not confident.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .timeconv import MAX_OFFSET_SECONDS, MIN_OFFSET_SECONDS

DEFAULT_APP_PATH = "/Applications/MetaTrader 5.app"
DEFAULT_WINEPREFIX = "~/Library/Application Support/net.metaquotes.wine.metatrader5"
DEFAULT_BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 18812
DEFAULT_BRIDGE_DIR = "C:\\mt5-bridge"
DEFAULT_TERMINAL_PATH = "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
DEFAULT_MAX_VOLUME = 1.0
DEFAULT_MAX_BARS = 5000
DEFAULT_MAX_DEVIATION = 50
DEFAULT_DEVIATION = 20
DEFAULT_AUDIT_LOG = "~/Library/Logs/mt5-mcp/audit.jsonl"
DEFAULT_BRIDGE_LOG = "~/Library/Logs/mt5-mcp/bridge.log"
DEFAULT_TOKEN_FILE = "~/Library/Application Support/mt5-mcp/bridge.token"
DEFAULT_READY_TIMEOUT = 90.0

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


class ConfigError(ValueError):
    """An environment variable holds a value that cannot be used."""


@dataclass(frozen=True)
class Config:
    app_path: Path
    wineprefix: Path
    bridge_host: str
    bridge_port: int
    bridge_autostart: bool
    bridge_dir: str
    terminal_path: str
    bridge_project: str
    enable_trading: bool
    allowed_symbols: frozenset[str]
    max_volume: float
    max_bars: int
    audit_log: Path
    bridge_log: Path
    token_file: Path
    ready_timeout: float = DEFAULT_READY_TIMEOUT
    max_deviation: int = DEFAULT_MAX_DEVIATION
    broker_utc_offset_seconds: int | None = None

    @property
    def wine_binary(self) -> Path:
        return self.app_path / "Contents" / "SharedSupport" / "wine" / "bin" / "wine64"

    @property
    def wine_external_lib(self) -> Path:
        return self.app_path / "Contents" / "SharedSupport" / "wine" / "lib" / "external"

    @property
    def default_deviation(self) -> int:
        """Slippage used when a tool call omits it: 20 points, never above the cap."""
        return min(DEFAULT_DEVIATION, self.max_deviation)

    @property
    def uv_exe(self) -> str:
        return str(PureWindowsPath(self.bridge_dir) / "bin" / "uv.exe")


def to_wine_path(path: Path) -> str:
    """Express an absolute macOS path as a Wine path (Wine maps Z: to the Unix root)."""
    if not path.is_absolute():
        raise ConfigError(f"Cannot map a relative path into Wine: {path}")
    return "Z:" + str(path).replace("/", "\\")


def default_bridge_project(package_file: Path) -> Path:
    """Locate bridge/ in the repository: src/mt5_mcp/<file> -> <repo>/bridge."""
    return package_file.resolve().parents[2] / "bridge"


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be 0 or 1, got {raw!r}")


def _parse_int(name: str, raw: str, *, minimum: int, maximum: int | None = None) -> int:
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"{name} must be {bound}, got {value}")
    return value


def _parse_positive_float(name: str, raw: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a positive number, got {raw!r}")
    return value


def parse_symbol_list(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _parse_allowed_symbols(raw: str) -> frozenset[str]:
    symbols = parse_symbol_list(raw)
    # "" means unset; anything else that yields no names is a typo that would otherwise
    # silently lift the restriction.
    if raw != "" and not symbols:
        raise ConfigError(
            f"MT5_ALLOWED_SYMBOLS is set but lists no symbols ({raw!r}); "
            "unset it to allow every symbol"
        )
    return symbols


def _parse_offset_hours(raw: str) -> int | None:
    text = raw.strip()
    if not text:
        return None
    try:
        hours = float(text)
    except ValueError as exc:
        raise ConfigError(f"MT5_BROKER_UTC_OFFSET_HOURS must be a number, got {raw!r}") from exc
    quarters = hours * 4
    if not math.isfinite(hours) or abs(quarters - round(quarters)) > 1e-9:
        raise ConfigError(
            f"MT5_BROKER_UTC_OFFSET_HOURS must be a multiple of 0.25 hours, got {raw!r}"
        )
    seconds = round(quarters) * 900
    if not MIN_OFFSET_SECONDS <= seconds <= MAX_OFFSET_SECONDS:
        raise ConfigError(f"MT5_BROKER_UTC_OFFSET_HOURS must be between -12 and 14, got {raw!r}")
    return seconds


def _parse_audit_log(raw: str, expand: Callable[[str], Path]) -> Path:
    if not raw.strip():
        raise ConfigError("MT5_AUDIT_LOG must not be empty")
    path = expand(raw.strip())
    if path.is_dir():
        raise ConfigError(f"MT5_AUDIT_LOG must be a file path, but {path} is a directory")
    return path


def load_config(
    env: Mapping[str, str],
    *,
    home: Path,
    package_file: Path,
) -> Config:
    """Build a Config from ``env``. ``home`` and ``package_file`` are passed in for testability."""

    def get(name: str, default: str) -> str:
        return env.get(name, default)

    def expand(raw: str) -> Path:
        # Expand "~" against the injected home rather than the process environment.
        if raw == "~":
            return home
        if raw.startswith("~/"):
            return home / raw[2:]
        return Path(raw)

    bridge_project_raw = env.get("MT5_BRIDGE_PROJECT", "").strip()
    bridge_project = bridge_project_raw or to_wine_path(default_bridge_project(package_file))

    host = get("MT5_BRIDGE_HOST", DEFAULT_BRIDGE_HOST).strip()
    if not host:
        raise ConfigError("MT5_BRIDGE_HOST must not be empty")

    return Config(
        app_path=expand(get("MT5_APP_PATH", DEFAULT_APP_PATH)),
        wineprefix=expand(get("MT5_WINEPREFIX", DEFAULT_WINEPREFIX)),
        bridge_host=host,
        bridge_port=_parse_int(
            "MT5_BRIDGE_PORT",
            get("MT5_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)),
            minimum=1,
            maximum=65535,
        ),
        bridge_autostart=_parse_bool("MT5_BRIDGE_AUTOSTART", get("MT5_BRIDGE_AUTOSTART", "1")),
        bridge_dir=get("MT5_BRIDGE_DIR", DEFAULT_BRIDGE_DIR),
        terminal_path=get("MT5_TERMINAL_PATH", DEFAULT_TERMINAL_PATH),
        bridge_project=bridge_project,
        enable_trading=_parse_bool("MT5_ENABLE_TRADING", get("MT5_ENABLE_TRADING", "0")),
        allowed_symbols=_parse_allowed_symbols(get("MT5_ALLOWED_SYMBOLS", "")),
        max_volume=_parse_positive_float(
            "MT5_MAX_VOLUME", get("MT5_MAX_VOLUME", str(DEFAULT_MAX_VOLUME))
        ),
        max_bars=_parse_int(
            "MT5_MAX_BARS", get("MT5_MAX_BARS", str(DEFAULT_MAX_BARS)), minimum=1, maximum=100000
        ),
        audit_log=_parse_audit_log(get("MT5_AUDIT_LOG", DEFAULT_AUDIT_LOG), expand),
        bridge_log=expand(get("MT5_BRIDGE_LOG", DEFAULT_BRIDGE_LOG)),
        token_file=expand(get("MT5_TOKEN_FILE", DEFAULT_TOKEN_FILE)),
        max_deviation=_parse_int(
            "MT5_MAX_DEVIATION",
            get("MT5_MAX_DEVIATION", str(DEFAULT_MAX_DEVIATION)),
            minimum=0,
        ),
        broker_utc_offset_seconds=_parse_offset_hours(get("MT5_BROKER_UTC_OFFSET_HOURS", "")),
    )
