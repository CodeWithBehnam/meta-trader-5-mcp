"""Read-only MCP tools. No `from __future__ import annotations`: the SDK reads annotations."""

import logging
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import intents, planning, shaping
from .bridge_client import BridgeError
from .context import AppContext
from .errors import describe_error, guarded
from .execute import dry_run
from .launcher import LauncherError
from .service import call_for_symbol, fetch_symbol_info, fetch_tick
from .timeconv import OffsetStatus

logger = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

Symbol = Annotated[str, Field(description="Broker symbol name, e.g. EURUSD (case-sensitive)")]
Timeframe = Literal[
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
]  # fmt: skip
IsoTime = Annotated[str | None, Field(description="ISO-8601 UTC time, e.g. 2026-10-13T08:30:00Z")]
Side = Literal["BUY", "SELL"]

TERMINAL_HIGHLIGHTS = (
    "connected",
    "trade_allowed",
    "tradeapi_disabled",
    "dlls_allowed",
    "build",
    "name",
    "company",
    "path",
    "ping_last",
)


def _rows(value: Any) -> list[dict[str, Any]]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def register_read_tools(server: MCPServer, ctx: AppContext) -> None:
    def tool(**kwargs: Any) -> Any:
        return server.tool(annotations=READ_ONLY, **kwargs)

    @tool()
    @guarded
    def terminal_status() -> dict[str, Any]:
        """Health check: bridge reachability, MT5 terminal connection, Algo Trading flag and
        the estimated broker time offset. Call this first when anything fails."""
        status: dict[str, Any] = {"mcp_trading_enabled": ctx.config.enable_trading}
        try:
            status["bridge"] = ctx.bridge.call("ping")
        except (BridgeError, LauncherError) as exc:
            return {**status, "bridge_reachable": False, "error": describe_error(exc)}
        status["bridge_reachable"] = True
        try:
            status["version"] = ctx.bridge.call("version")
            info = ctx.bridge.call("terminal_info")
        except BridgeError as exc:
            return {**status, "terminal_error": describe_error(exc)}
        if not isinstance(info, dict):
            return {**status, "terminal_error": "MT5 returned no terminal information"}
        status["terminal"] = {k: info[k] for k in TERMINAL_HIGHLIGHTS if k in info}
        status.update(shaping.time_info(ctx.clock.status()))
        return status

    @tool()
    @guarded
    def get_account_info() -> dict[str, Any]:
        """Account balance, equity, margin, leverage, currency, server and trade mode
        (0 = demo, 1 = contest, 2 = real). Money values are in the account currency."""
        info = ctx.bridge.call("account_info")
        if not isinstance(info, dict):
            raise ToolError("MT5 returned no account information")
        return info

    @tool()
    @guarded
    def list_symbols(
        group: Annotated[
            str | None,
            Field(description="MT5 group filter, e.g. '*USD*' or 'Forex*,!*RUB*'"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """List symbols known to the terminal (name, description, path, currencies, digits,
        visible in Market Watch). Use group to narrow; use get_symbol_info for full detail."""
        rows = _rows(ctx.bridge.call("symbols_get", {"group": group} if group else {}))
        return {
            "total": len(rows),
            "returned": min(len(rows), limit),
            "symbols": [shaping.summarise_symbol(r) for r in rows[:limit]],
        }

    @tool()
    @guarded
    def get_symbol_info(symbol: Symbol) -> dict[str, Any]:
        """Full symbol specification: digits, point, contract size, volume min/max/step,
        stops level, trade mode, swap and filling modes (decoded in filling_modes).
        Adds the symbol to Market Watch if needed."""
        info = fetch_symbol_info(ctx, symbol)
        return shaping.shape_symbol_info(info, ctx.clock.offset(symbol))

    @tool()
    @guarded
    def get_tick(symbol: Symbol) -> dict[str, Any]:
        """Latest tick: bid, ask, last, volume. Times in raw broker epoch plus UTC and broker
        ISO strings. Adds the symbol to Market Watch if needed."""
        tick = fetch_tick(ctx, symbol)
        clock = ctx.clock.status(symbol)
        return {**shaping.shape_tick(tick, clock.offset), **shaping.time_info(clock)}

    @tool()
    @guarded
    def get_rates(
        symbol: Symbol,
        timeframe: Timeframe,
        count: Annotated[
            int | None,
            Field(description="Number of bars (default 100). Alone: the latest bars."),
        ] = None,
        start: IsoTime = None,
        end: IsoTime = None,
    ) -> dict[str, Any]:
        """OHLCV bars. Modes: count only = latest bars; end (+count) = bars up to end;
        start (+end, default now) = bars in a range. Capped at MT5_MAX_BARS (earliest kept
        for ranges; 'truncated' says so). Bar times are the bar open in broker time
        (raw 'time') plus 'time_utc'. tick_volume is tick count, real_volume often 0."""
        clock = ctx.clock.status(symbol)
        offset = clock.offset
        plan = planning.plan_rates(
            symbol,
            timeframe,
            count=count,
            start=start,
            end=end,
            max_bars=ctx.config.max_bars,
            offset=offset,
            now=ctx.now(),
        )
        rows = _rows(call_for_symbol(ctx, plan.method, plan.params, symbol))
        kept, truncated = shaping.trim_to_first(rows, plan.limit)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(kept),
            "truncated": truncated or plan.clamped,
            "max_bars": ctx.config.max_bars,
            **shaping.time_info(clock),
            "rates": shaping.shape_bulk(kept, offset),
        }

    @tool()
    @guarded
    def get_ticks(
        symbol: Symbol,
        start: Annotated[str, Field(description="ISO-8601 UTC start time (ticks run forward)")],
        end: IsoTime = None,
        count: Annotated[
            int | None, Field(description="Max ticks when end is omitted (default 1000)")
        ] = None,
        flags: Literal["ALL", "INFO", "TRADE"] = "ALL",
    ) -> dict[str, Any]:
        """Historical ticks from start, up to end or up to count ticks. Capped at MT5_MAX_BARS.
        flags: ALL, INFO (bid/ask changes) or TRADE (last/volume changes)."""
        clock = ctx.clock.status(symbol)
        offset = clock.offset
        plan = planning.plan_ticks(
            symbol,
            start=start,
            end=end,
            count=count,
            flags=flags,
            max_bars=ctx.config.max_bars,
            offset=offset,
        )
        rows = _rows(call_for_symbol(ctx, plan.method, plan.params, symbol))
        if plan.until is not None:
            rows = [r for r in rows if r.get("time", 0) <= plan.until]
        kept, truncated = shaping.trim_to_first(rows, plan.limit)
        return {
            "symbol": symbol,
            "count": len(kept),
            "truncated": truncated or plan.clamped,
            "max_ticks": ctx.config.max_bars,
            **shaping.time_info(clock),
            "ticks": shaping.shape_bulk(kept, offset, msc=True),
        }

    @tool()
    @guarded
    def get_positions(
        symbol: Annotated[str | None, Field(description="Filter by symbol")] = None,
        ticket: Annotated[int | None, Field(description="Filter by position ticket")] = None,
    ) -> dict[str, Any]:
        """Open positions with decoded type (BUY/SELL), volume in lots, open price, SL/TP,
        floating profit and times (broker + UTC)."""
        if symbol is not None and ticket is not None:
            raise ToolError("Use either symbol or ticket, not both")
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if ticket is not None:
            params["ticket"] = ticket
        clock = ctx.clock.status(symbol)
        offset = clock.offset
        rows = _rows(ctx.bridge.call("positions_get", params))
        return {
            "count": len(rows),
            **shaping.time_info(clock),
            "positions": [shaping.shape_position(r, offset) for r in rows],
        }

    @tool()
    @guarded
    def get_orders(
        symbol: Annotated[str | None, Field(description="Filter by symbol")] = None,
        ticket: Annotated[int | None, Field(description="Filter by order ticket")] = None,
    ) -> dict[str, Any]:
        """Active pending orders with decoded type, state, volume, price, SL/TP, expiry."""
        if symbol is not None and ticket is not None:
            raise ToolError("Use either symbol or ticket, not both")
        params: dict[str, Any] = {}
        if symbol is not None:
            params["symbol"] = symbol
        if ticket is not None:
            params["ticket"] = ticket
        clock = ctx.clock.status(symbol)
        offset = clock.offset
        rows = _rows(ctx.bridge.call("orders_get", params))
        return {
            "count": len(rows),
            **shaping.time_info(clock),
            "orders": [shaping.shape_order(r, offset) for r in rows],
        }

    def history_params(
        start: str | None,
        end: str | None,
        ticket: int | None,
        position: int | None,
        group: str | None,
    ) -> tuple[dict[str, Any], OffsetStatus]:
        selectors = [x for x in (ticket, position) if x is not None]
        if len(selectors) > 1:
            raise ToolError("Use either ticket or position, not both")
        if ticket is not None:
            return {"ticket": ticket}, ctx.clock.status()
        if position is not None:
            return {"position": position}, ctx.clock.status()
        clock = ctx.clock.status()
        date_from, date_to = planning.plan_history_window(
            start, end, offset=clock.offset, now=ctx.now()
        )
        params: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
        if group:
            params["group"] = group
        return params, clock

    @tool()
    @guarded
    def get_history_orders(
        start: IsoTime = None,
        end: IsoTime = None,
        ticket: Annotated[int | None, Field(description="A single order ticket")] = None,
        position: Annotated[int | None, Field(description="All orders of a position id")] = None,
        group: Annotated[str | None, Field(description="Symbol group filter, e.g. '*USD*'")] = None,
    ) -> dict[str, Any]:
        """Historical orders (default: last 7 days). Range is ISO-8601 UTC; or pass ticket or
        position id. Orders carry decoded type, state and times."""
        params, clock = history_params(start, end, ticket, position, group)
        offset = clock.offset
        rows = _rows(ctx.bridge.call("history_orders_get", params))
        return {
            "count": len(rows),
            **shaping.time_info(clock),
            "orders": [shaping.shape_order(r, offset) for r in rows],
        }

    @tool()
    @guarded
    def get_history_deals(
        start: IsoTime = None,
        end: IsoTime = None,
        ticket: Annotated[int | None, Field(description="A single deal ticket")] = None,
        position: Annotated[int | None, Field(description="All deals of a position id")] = None,
        group: Annotated[str | None, Field(description="Symbol group filter, e.g. '*USD*'")] = None,
    ) -> dict[str, Any]:
        """Historical deals (executions, balance operations; default last 7 days). Range is
        ISO-8601 UTC; or pass ticket or position id. Deals carry decoded type, entry (IN/OUT),
        profit, commission and swap in account currency."""
        params, clock = history_params(start, end, ticket, position, group)
        offset = clock.offset
        rows = _rows(ctx.bridge.call("history_deals_get", params))
        return {
            "count": len(rows),
            **shaping.time_info(clock),
            "deals": [shaping.shape_deal(r, offset) for r in rows],
        }

    @tool()
    @guarded
    def calc_margin(
        symbol: Symbol,
        side: Side,
        volume: Annotated[float, Field(gt=0, description="Lots")],
        price: Annotated[float | None, Field(description="Default: current ask/bid")] = None,
    ) -> dict[str, Any]:
        """Margin required to open a position, in account currency."""
        use_price = price if price is not None else _quote(symbol, side)
        margin = call_for_symbol(
            ctx,
            "order_calc_margin",
            {"action": side, "symbol": symbol, "volume": volume, "price": use_price},
            symbol,
        )
        return {
            "symbol": symbol,
            "side": side,
            "volume": volume,
            "price": use_price,
            "margin": margin,
        }

    @tool()
    @guarded
    def calc_profit(
        symbol: Symbol,
        side: Side,
        volume: Annotated[float, Field(gt=0, description="Lots")],
        price_open: float,
        price_close: float,
    ) -> dict[str, Any]:
        """Profit in account currency of a position opened at price_open and closed at
        price_close (before commission and swap)."""
        profit = call_for_symbol(
            ctx,
            "order_calc_profit",
            {
                "action": side,
                "symbol": symbol,
                "volume": volume,
                "price_open": price_open,
                "price_close": price_close,
            },
            symbol,
        )
        return {"symbol": symbol, "side": side, "volume": volume, "profit": profit}

    def _quote(symbol: str, side: str) -> float:
        tick = fetch_tick(ctx, symbol)
        value = tick.get("ask") if side == "BUY" else tick.get("bid")
        if not isinstance(value, int | float) or value <= 0:
            raise ToolError(f"No valid {'ask' if side == 'BUY' else 'bid'} price for {symbol}")
        return float(value)

    @tool()
    @guarded
    def check_order(
        symbol: Symbol,
        side: Side,
        volume: Annotated[float, Field(gt=0, description="Lots")],
        kind: Literal["MARKET", "LIMIT", "STOP", "STOP_LIMIT"] = "MARKET",
        price: Annotated[float | None, Field(description="Required unless MARKET")] = None,
        stoplimit_price: Annotated[float | None, Field(description="STOP_LIMIT only")] = None,
        sl: Annotated[float | None, Field(description="Stop loss price; omit for none")] = None,
        tp: Annotated[float | None, Field(description="Take profit price; omit for none")] = None,
        deviation: Annotated[
            int | None,
            Field(
                ge=0, description="Max slippage in points (default 20); capped by MT5_MAX_DEVIATION"
            ),
        ] = None,
        expiration: IsoTime = None,
        filling: Annotated[
            Literal["FOK", "IOC", "RETURN"] | None,
            Field(description="Default: chosen from the symbol's filling modes"),
        ] = None,
    ) -> dict[str, Any]:
        """Dry run: validate an order with MT5 without sending it. Works with trading
        disabled. Returns ok, retcode (0 = acceptable), margin required and free margin
        after. Same arguments as place_order. Pending kinds are LIMIT, STOP, STOP_LIMIT."""
        intent = intents.resolve_place_order(
            ctx,
            symbol=symbol,
            side=side,
            volume=volume,
            kind=kind,
            price=price,
            stoplimit_price=stoplimit_price,
            sl=sl,
            tp=tp,
            deviation=deviation,
            magic=0,
            comment="",
            expiration=expiration,
            filling=filling,
        )
        return dry_run(ctx, intent)
