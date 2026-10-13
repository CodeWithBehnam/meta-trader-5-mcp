"""Pure validation of order intents against the server configuration.

Every trading tool turns its arguments into one of the intent dataclasses below,
resolving anything it must look up (position volume, tick price) beforehand, and then
asks ``check_intent`` whether the intent may proceed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from .config import Config


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class PendingType(StrEnum):
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"
    BUY_STOP_LIMIT = "BUY_STOP_LIMIT"
    SELL_STOP_LIMIT = "SELL_STOP_LIMIT"


class TimeType(StrEnum):
    GTC = "GTC"
    DAY = "DAY"
    SPECIFIED = "SPECIFIED"
    SPECIFIED_DAY = "SPECIFIED_DAY"


class Filling(StrEnum):
    FOK = "FOK"
    IOC = "IOC"
    RETURN = "RETURN"


_STOP_LIMIT_TYPES = frozenset({PendingType.BUY_STOP_LIMIT, PendingType.SELL_STOP_LIMIT})
_EXPIRY_TYPES = frozenset({TimeType.SPECIFIED, TimeType.SPECIFIED_DAY})


@dataclass(frozen=True)
class MarketOrder:
    symbol: str
    side: Side
    volume: float
    price: float
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 20
    magic: int = 0
    comment: str = ""
    filling: Filling = Filling.IOC


@dataclass(frozen=True)
class PendingOrder:
    symbol: str
    order_type: PendingType
    volume: float
    price: float
    stoplimit: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    time_type: TimeType = TimeType.GTC
    expiration: int | None = None
    magic: int = 0
    comment: str = ""
    filling: Filling = Filling.RETURN


@dataclass(frozen=True)
class ModifyPosition:
    symbol: str
    ticket: int
    sl: float
    tp: float


@dataclass(frozen=True)
class ModifyPendingOrder:
    symbol: str
    ticket: int
    price: float
    sl: float
    tp: float
    stoplimit: float = 0.0
    time_type: TimeType = TimeType.GTC
    expiration: int | None = None


@dataclass(frozen=True)
class ClosePosition:
    symbol: str
    ticket: int
    position_side: Side
    position_volume: float
    volume: float
    price: float
    deviation: int = 20
    magic: int = 0
    comment: str = ""
    filling: Filling = Filling.IOC


@dataclass(frozen=True)
class CancelOrder:
    symbol: str
    ticket: int


Intent = (
    MarketOrder | PendingOrder | ModifyPosition | ModifyPendingOrder | ClosePosition | CancelOrder
)


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class Rejected:
    reason: str


GuardResult = Allowed | Rejected


class GuardRefusal(ValueError):
    """A guard refused before an intent could be built (so there is no request to audit).

    ``request`` carries whatever identifies the attempt for the audit trail.
    """

    def __init__(self, reason: str, request: dict[str, object]) -> None:
        super().__init__(f"Refused by guard: {reason}")
        self.reason = reason
        self.request = request


def _finite(value: float) -> bool:
    return math.isfinite(value)


def check_symbol(symbol: str, config: Config) -> Rejected | None:
    if not symbol.strip():
        return Rejected("symbol is required")
    if config.allowed_symbols and symbol not in config.allowed_symbols:
        allowed = ", ".join(sorted(config.allowed_symbols))
        return Rejected(f"symbol {symbol!r} is not in MT5_ALLOWED_SYMBOLS ({allowed})")
    return None


def _check_open_volume(volume: float, config: Config) -> Rejected | None:
    if not _finite(volume) or volume <= 0:
        return Rejected(f"volume must be greater than 0, got {volume}")
    if volume > config.max_volume:
        return Rejected(f"volume {volume} exceeds MT5_MAX_VOLUME ({config.max_volume})")
    return None


def _check_deviation(deviation: int, config: Config) -> Rejected | None:
    if deviation < 0:
        return Rejected("deviation must be >= 0")
    if deviation > config.max_deviation:
        return Rejected(f"deviation {deviation} exceeds MT5_MAX_DEVIATION ({config.max_deviation})")
    return None


def _check_levels(**levels: float) -> Rejected | None:
    for name, value in levels.items():
        if not _finite(value) or value < 0:
            return Rejected(f"{name} must be a finite number >= 0 (0 means unset), got {value}")
    return None


def _check_expiry(time_type: TimeType, expiration: int | None) -> Rejected | None:
    if time_type in _EXPIRY_TYPES and (expiration is None or expiration <= 0):
        return Rejected(f"expiration is required when time type is {time_type.value}")
    return None


def _first_rejection(*results: Rejected | None) -> GuardResult:
    for result in results:
        if result is not None:
            return result
    return Allowed()


def check_intent(intent: Intent, config: Config, *, require_trading: bool = True) -> GuardResult:
    """Decide whether ``intent`` may proceed. ``require_trading=False`` is for dry runs."""
    if require_trading and not config.enable_trading:
        return Rejected("trading is disabled (set MT5_ENABLE_TRADING=1 to enable)")
    match intent:
        case MarketOrder():
            return _first_rejection(
                check_symbol(intent.symbol, config),
                _check_open_volume(intent.volume, config),
                _check_levels(price=intent.price, sl=intent.sl, tp=intent.tp),
                None if intent.price > 0 else Rejected("no valid market price available"),
                _check_deviation(intent.deviation, config),
            )
        case PendingOrder():
            stop_limit_missing = intent.order_type in _STOP_LIMIT_TYPES and not intent.stoplimit > 0
            return _first_rejection(
                check_symbol(intent.symbol, config),
                _check_open_volume(intent.volume, config),
                _check_levels(
                    price=intent.price, stoplimit=intent.stoplimit, sl=intent.sl, tp=intent.tp
                ),
                None if intent.price > 0 else Rejected("price is required for a pending order"),
                Rejected("stoplimit_price is required for STOP_LIMIT orders")
                if stop_limit_missing
                else None,
                _check_expiry(intent.time_type, intent.expiration),
            )
        case ModifyPosition():
            return _first_rejection(
                check_symbol(intent.symbol, config),
                _check_ticket(intent.ticket),
                _check_levels(sl=intent.sl, tp=intent.tp),
            )
        case ModifyPendingOrder():
            return _first_rejection(
                check_symbol(intent.symbol, config),
                _check_ticket(intent.ticket),
                _check_levels(
                    price=intent.price, stoplimit=intent.stoplimit, sl=intent.sl, tp=intent.tp
                ),
                None if intent.price > 0 else Rejected("price must be greater than 0"),
                _check_expiry(intent.time_type, intent.expiration),
            )
        case ClosePosition():
            # Closing reduces exposure, so MT5_MAX_VOLUME deliberately does not apply.
            return _first_rejection(
                check_symbol(intent.symbol, config),
                _check_ticket(intent.ticket),
                _check_close_volume(intent),
                _check_deviation(intent.deviation, config),
                None if intent.price > 0 else Rejected("no valid market price available"),
            )
        case CancelOrder():
            return _first_rejection(
                check_symbol(intent.symbol, config), _check_ticket(intent.ticket)
            )
        case _:
            assert_never(intent)


def _check_ticket(ticket: int) -> Rejected | None:
    return None if ticket > 0 else Rejected(f"ticket must be a positive integer, got {ticket}")


def _check_close_volume(intent: ClosePosition) -> Rejected | None:
    if not _finite(intent.volume) or intent.volume <= 0:
        return Rejected(f"volume must be greater than 0, got {intent.volume}")
    if intent.volume > intent.position_volume:
        return Rejected(
            f"volume {intent.volume} exceeds the position volume ({intent.position_volume})"
        )
    return None
