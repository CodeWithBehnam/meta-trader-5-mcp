"""Mapping of internal failures to MCP ToolError messages."""

import functools
from collections.abc import Callable

from mcp.server.mcpserver.exceptions import ToolError

from .bridge_client import (
    BridgeAuthError,
    BridgeBadRequest,
    BridgeError,
    BridgeNotConnected,
    BridgeUnavailable,
    Mt5Error,
    TradingDisabled,
)
from .config import ConfigError
from .launcher import LauncherError


def describe_error(exc: Exception) -> str:
    """User-facing text for an anticipated failure."""
    if isinstance(exc, BridgeNotConnected):
        return f"The bridge cannot reach the MT5 terminal: {exc}. Open MetaTrader 5 and log in."
    if isinstance(exc, Mt5Error):
        return f"MT5 returned an error (code {exc.code}): {exc.message}"
    if isinstance(exc, TradingDisabled):
        return f"The bridge refuses trading: {exc}"
    if isinstance(exc, BridgeAuthError):
        return f"The bridge rejected the auth token: {exc}"
    if isinstance(exc, BridgeUnavailable):
        return f"The bridge is unavailable: {exc}"
    if isinstance(exc, BridgeBadRequest):
        return f"The bridge rejected the request: {exc}"
    return str(exc)


def guarded[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Convert expected exceptions into ToolError so the model sees a clear message."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except (BridgeError, LauncherError, ConfigError, ValueError) as exc:
            raise ToolError(describe_error(exc)) from exc

    return wrapper


def tool_error(message: str) -> ToolError:
    return ToolError(message)
