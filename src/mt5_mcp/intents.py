"""Resolve tool arguments into order intents, looking up what MT5 must supply.

These functions perform read-only bridge calls (positions, orders, ticks, symbol info)
and then delegate the decisions to pure helpers.
"""

from __future__ import annotations

from typing import Any

from . import enums
from .context import AppContext
from .guards import (
    CancelOrder,
    ClosePosition,
    Filling,
    GuardRefusal,
    MarketOrder,
    ModifyPendingOrder,
    ModifyPosition,
    PendingOrder,
    Side,
    TimeType,
    check_symbol,
)
from .orders import is_market_execution, pending_type_for, pick_filling
from .service import fetch_symbol_info, fetch_tick
from .timeconv import parse_iso_utc, utc_to_broker_epoch


def normalise_price(value: float, digits: object) -> float:
    """Round to the symbol's digits; MT5 rejects prices with extra decimals."""
    if isinstance(digits, int) and not isinstance(digits, bool) and digits >= 0:
        return round(value, digits)
    return value


def _digits(info: dict[str, Any]) -> object:
    return info.get("digits")


def _require_allowed(ctx: AppContext, symbol: str) -> None:
    """Apply the symbol allowlist before anything touches Market Watch or the bridge."""
    rejection = check_symbol(symbol, ctx.config)
    if rejection is not None:
        raise GuardRefusal(rejection.reason, {"symbol": symbol})


def _expiry_epoch(ctx: AppContext, symbol: str, expiration: str | None) -> int | None:
    if expiration is None:
        return None
    offset = ctx.clock.offset(symbol)
    if offset is None:
        raise ValueError(
            "Cannot convert expiration to broker time: the broker offset is unknown (it could "
            "not be estimated, probably because the market is closed). Set "
            "MT5_BROKER_UTC_OFFSET_HOURS (for example 3) and retry."
        )
    moment = parse_iso_utc(expiration)
    if moment <= ctx.now():
        raise GuardRefusal(
            f"expiration {expiration} is in the past",
            {"symbol": symbol, "expiration": expiration},
        )
    return utc_to_broker_epoch(moment, offset)


def resolve_place_order(
    ctx: AppContext,
    *,
    symbol: str,
    side: str,
    volume: float,
    kind: str,
    price: float | None,
    stoplimit_price: float | None,
    sl: float | None,
    tp: float | None,
    deviation: int | None,
    magic: int,
    comment: str,
    expiration: str | None,
    filling: str | None,
) -> MarketOrder | PendingOrder:
    _require_allowed(ctx, symbol)
    side_enum = Side(side)
    info = fetch_symbol_info(ctx, symbol)
    digits = _digits(info)
    flags = info.get("filling_mode", 0)
    is_market = kind == "MARKET"
    exemode = info.get("trade_exemode")
    if is_market and filling == Filling.RETURN and is_market_execution(exemode):
        raise ValueError(
            f"{symbol} uses market execution, which does not accept filling RETURN for market "
            "orders; use FOK or IOC, or omit filling to have one chosen"
        )
    chosen = (
        Filling(filling)
        if filling is not None
        else pick_filling(
            flags if isinstance(flags, int) else 0, market=is_market, execution_mode=exemode
        )
    )
    sl_value = normalise_price(sl or 0.0, digits)
    tp_value = normalise_price(tp or 0.0, digits)
    if is_market:
        tick = fetch_tick(ctx, symbol)
        quote = tick.get("ask") if side_enum is Side.BUY else tick.get("bid")
        return MarketOrder(
            symbol=symbol,
            side=side_enum,
            volume=volume,
            price=float(quote or 0.0),
            sl=sl_value,
            tp=tp_value,
            deviation=ctx.config.default_deviation if deviation is None else deviation,
            magic=magic,
            comment=comment,
            filling=chosen,
        )
    if price is None:
        raise ValueError(f"price is required for {kind} orders")
    expiry = _expiry_epoch(ctx, symbol, expiration)
    return PendingOrder(
        symbol=symbol,
        order_type=pending_type_for(side_enum, kind),
        volume=volume,
        price=normalise_price(price, digits),
        stoplimit=normalise_price(stoplimit_price or 0.0, digits),
        sl=sl_value,
        tp=tp_value,
        time_type=TimeType.SPECIFIED if expiry is not None else TimeType.GTC,
        expiration=expiry,
        magic=magic,
        comment=comment,
        filling=chosen,
    )


def _single(ctx: AppContext, method: str, ticket: int, what: str) -> dict[str, Any]:
    rows = ctx.bridge.call(method, {"ticket": ticket})
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError(f"No {what} with ticket {ticket}")
    return rows[0]


def resolve_modify_position(
    ctx: AppContext, *, ticket: int, sl: float | None, tp: float | None
) -> ModifyPosition:
    position = _single(ctx, "positions_get", ticket, "open position")
    symbol = str(position["symbol"])
    _require_allowed(ctx, symbol)
    digits = _digits(fetch_symbol_info(ctx, symbol))
    return ModifyPosition(
        symbol=symbol,
        ticket=ticket,
        sl=normalise_price(position.get("sl", 0.0) if sl is None else sl, digits),
        tp=normalise_price(position.get("tp", 0.0) if tp is None else tp, digits),
    )


def resolve_modify_pending(
    ctx: AppContext,
    *,
    ticket: int,
    price: float | None,
    sl: float | None,
    tp: float | None,
    stoplimit_price: float | None,
    expiration: str | None,
) -> ModifyPendingOrder:
    order = _single(ctx, "orders_get", ticket, "pending order")
    symbol = str(order["symbol"])
    _require_allowed(ctx, symbol)
    digits = _digits(fetch_symbol_info(ctx, symbol))
    expiry = _expiry_epoch(ctx, symbol, expiration)
    if expiry is not None:
        time_type, expires = TimeType.SPECIFIED, expiry
    else:
        name = enums.ORDER_TIMES.get(order.get("type_time", 0), "GTC")
        time_type = TimeType(name)
        stored = order.get("time_expiration")
        expires = stored if isinstance(stored, int) and stored > 0 else None
    return ModifyPendingOrder(
        symbol=symbol,
        ticket=ticket,
        price=normalise_price(order.get("price_open", 0.0) if price is None else price, digits),
        sl=normalise_price(order.get("sl", 0.0) if sl is None else sl, digits),
        tp=normalise_price(order.get("tp", 0.0) if tp is None else tp, digits),
        stoplimit=normalise_price(
            order.get("price_stoplimit", 0.0) if stoplimit_price is None else stoplimit_price,
            digits,
        ),
        time_type=time_type,
        expiration=expires if time_type in (TimeType.SPECIFIED, TimeType.SPECIFIED_DAY) else None,
    )


def resolve_close_position(
    ctx: AppContext,
    *,
    ticket: int,
    volume: float | None,
    deviation: int | None,
    comment: str,
) -> ClosePosition:
    position = _single(ctx, "positions_get", ticket, "open position")
    symbol = str(position["symbol"])
    _require_allowed(ctx, symbol)
    side = Side(enums.POSITION_TYPES[int(position["type"])])
    held = float(position["volume"])
    info = fetch_symbol_info(ctx, symbol)
    flags = info.get("filling_mode", 0)
    tick = fetch_tick(ctx, symbol)
    # Closing a long sells at the bid; closing a short buys at the ask.
    quote = tick.get("bid") if side is Side.BUY else tick.get("ask")
    return ClosePosition(
        symbol=symbol,
        ticket=ticket,
        position_side=side,
        position_volume=held,
        volume=held if volume is None else volume,
        price=float(quote or 0.0),
        deviation=ctx.config.default_deviation if deviation is None else deviation,
        magic=int(position.get("magic", 0)),
        comment=comment,
        filling=pick_filling(
            flags if isinstance(flags, int) else 0,
            market=True,
            execution_mode=info.get("trade_exemode"),
        ),
    )


def resolve_cancel_order(ctx: AppContext, *, ticket: int) -> CancelOrder:
    order = _single(ctx, "orders_get", ticket, "pending order")
    symbol = str(order["symbol"])
    _require_allowed(ctx, symbol)
    return CancelOrder(symbol=symbol, ticket=ticket)
