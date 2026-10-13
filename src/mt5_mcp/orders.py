"""Pure construction of bridge order requests from validated intents."""

from __future__ import annotations

from typing import Any, assert_never

from .enums import SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC, SYMBOL_TRADE_EXECUTION_MARKET
from .guards import (
    CancelOrder,
    ClosePosition,
    Filling,
    Intent,
    MarketOrder,
    ModifyPendingOrder,
    ModifyPosition,
    PendingOrder,
    PendingType,
    Side,
    TimeType,
)

_PENDING_BY_SIDE_AND_KIND: dict[tuple[Side, str], PendingType] = {
    (Side.BUY, "LIMIT"): PendingType.BUY_LIMIT,
    (Side.SELL, "LIMIT"): PendingType.SELL_LIMIT,
    (Side.BUY, "STOP"): PendingType.BUY_STOP,
    (Side.SELL, "STOP"): PendingType.SELL_STOP,
    (Side.BUY, "STOP_LIMIT"): PendingType.BUY_STOP_LIMIT,
    (Side.SELL, "STOP_LIMIT"): PendingType.SELL_STOP_LIMIT,
}


def pending_type_for(side: Side, kind: str) -> PendingType:
    try:
        return _PENDING_BY_SIDE_AND_KIND[(side, kind)]
    except KeyError:
        raise ValueError(f"Unknown pending order kind {kind!r}") from None


def opposite(side: Side) -> Side:
    return Side.SELL if side is Side.BUY else Side.BUY


def is_market_execution(execution_mode: object) -> bool:
    return execution_mode == SYMBOL_TRADE_EXECUTION_MARKET and not isinstance(execution_mode, bool)


def pick_filling(
    symbol_filling_flags: int, *, market: bool, execution_mode: object = None
) -> Filling:
    """Choose a filling mode from symbol_info().filling_mode flags.

    Pending orders use RETURN. Market orders prefer FOK (all or nothing), then IOC,
    and fall back to RETURN when the symbol advertises neither flag. Symbols with market
    execution (trade_exemode 2) never accept RETURN for a market order, so they fall back
    to IOC instead.
    """
    if not market:
        return Filling.RETURN
    if symbol_filling_flags & SYMBOL_FILLING_FOK:
        return Filling.FOK
    if symbol_filling_flags & SYMBOL_FILLING_IOC:
        return Filling.IOC
    return Filling.IOC if is_market_execution(execution_mode) else Filling.RETURN


def _expiry(time_type: TimeType, expiration: int | None) -> dict[str, Any]:
    out: dict[str, Any] = {"type_time": time_type.value}
    if expiration is not None:
        out["expiration"] = expiration
    return out


def build_request(intent: Intent) -> dict[str, Any]:
    """Translate an intent into the bridge's order request dict (strings for enums)."""
    match intent:
        case MarketOrder():
            return {
                "action": "DEAL",
                "symbol": intent.symbol,
                "volume": intent.volume,
                "type": intent.side.value,
                "price": intent.price,
                "sl": intent.sl,
                "tp": intent.tp,
                "deviation": intent.deviation,
                "magic": intent.magic,
                "comment": intent.comment,
                "type_filling": intent.filling.value,
                "type_time": TimeType.GTC.value,
            }
        case PendingOrder():
            return {
                "action": "PENDING",
                "symbol": intent.symbol,
                "volume": intent.volume,
                "type": intent.order_type.value,
                "price": intent.price,
                "stoplimit": intent.stoplimit,
                "sl": intent.sl,
                "tp": intent.tp,
                "magic": intent.magic,
                "comment": intent.comment,
                "type_filling": intent.filling.value,
                **_expiry(intent.time_type, intent.expiration),
            }
        case ModifyPosition():
            return {
                "action": "SLTP",
                "symbol": intent.symbol,
                "position": intent.ticket,
                "sl": intent.sl,
                "tp": intent.tp,
            }
        case ModifyPendingOrder():
            return {
                "action": "MODIFY",
                "symbol": intent.symbol,
                "order": intent.ticket,
                "price": intent.price,
                "stoplimit": intent.stoplimit,
                "sl": intent.sl,
                "tp": intent.tp,
                **_expiry(intent.time_type, intent.expiration),
            }
        case ClosePosition():
            # A DEAL in the opposite direction with `position` set closes (or reduces) it.
            return {
                "action": "DEAL",
                "symbol": intent.symbol,
                "volume": intent.volume,
                "type": opposite(intent.position_side).value,
                "position": intent.ticket,
                "price": intent.price,
                "deviation": intent.deviation,
                "magic": intent.magic,
                "comment": intent.comment,
                "type_filling": intent.filling.value,
            }
        case CancelOrder():
            return {"action": "REMOVE", "order": intent.ticket}
        case _:
            assert_never(intent)
