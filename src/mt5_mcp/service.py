"""Shared bridge lookups used by the read and trading tools."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .bridge_client import Mt5Error
from .context import AppContext


def call_for_symbol(ctx: AppContext, method: str, params: Mapping[str, Any], symbol: str) -> Any:
    """Call a symbol-scoped method, adding the symbol to Market Watch if MT5 refuses it."""
    try:
        return ctx.bridge.call(method, params)
    except Mt5Error as first:
        try:
            selected = ctx.bridge.call("symbol_select", {"symbol": symbol, "enable": True})
        except Mt5Error:
            raise first from None
        if not selected:
            raise ValueError(f"Unknown symbol {symbol!r}") from first
        return ctx.bridge.call(method, params)


def fetch_tick(ctx: AppContext, symbol: str) -> dict[str, Any]:
    tick = call_for_symbol(ctx, "symbol_info_tick", {"symbol": symbol}, symbol)
    if not isinstance(tick, dict):
        raise ValueError(f"No tick available for {symbol!r}")
    return tick


def fetch_symbol_info(ctx: AppContext, symbol: str) -> dict[str, Any]:
    info = call_for_symbol(ctx, "symbol_info", {"symbol": symbol}, symbol)
    if not isinstance(info, dict):
        raise ValueError(f"No symbol info for {symbol!r}")
    return info
