"""Parameter schemas and validation. Pure, no MetaTrader5 import."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto

from mt5_bridge.protocol import BridgeError, ErrorType

MAX_COUNT = 100_000

TIMEFRAMES = (
    "M1", "M2", "M3", "M4", "M5", "M6", "M10", "M12", "M15", "M20", "M30",
    "H1", "H2", "H3", "H4", "H6", "H8", "H12", "D1", "W1", "MN1",
)  # fmt: skip
TICK_FLAGS = ("ALL", "INFO", "TRADE")
SIDES = ("BUY", "SELL")

# Order request enum fields: allowed names and the MT5 constant prefix.
ORDER_ENUMS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "action": ("TRADE_ACTION_", ("DEAL", "PENDING", "SLTP", "MODIFY", "REMOVE", "CLOSE_BY")),
    "type": (
        "ORDER_TYPE_",
        (
            "BUY",
            "SELL",
            "BUY_LIMIT",
            "SELL_LIMIT",
            "BUY_STOP",
            "SELL_STOP",
            "BUY_STOP_LIMIT",
            "SELL_STOP_LIMIT",
            "CLOSE_BY",
        ),
    ),
    "type_filling": ("ORDER_FILLING_", ("FOK", "IOC", "RETURN", "BOC")),
    "type_time": ("ORDER_TIME_", ("GTC", "DAY", "SPECIFIED", "SPECIFIED_DAY")),
}
U32_MAX = 2**32 - 1
I64_MAX = 2**63 - 1
# The MT5 C layer raises OverflowError beyond these, so reject them up front.
_ORDER_INT_MAX: Mapping[str, int] = {
    "magic": U32_MAX,
    "deviation": U32_MAX,
    "order": I64_MAX,
    "position": I64_MAX,
    "position_by": I64_MAX,
    # Epoch seconds as a plain int: the C layer rejects datetime objects here
    # ("Invalid expiration argument"), unlike for copy_* dates.
    "expiration": I64_MAX,
}
_ORDER_INTS = tuple(_ORDER_INT_MAX)
_ORDER_FLOATS = ("volume", "price", "stoplimit", "sl", "tp")
_ORDER_STRS = ("symbol", "comment")


class Kind(Enum):
    STR = auto()
    INT = auto()
    TICKET = auto()
    FLOAT = auto()
    BOOL = auto()
    TIME = auto()
    COUNT = auto()
    START_POS = auto()
    TIMEFRAME = auto()
    TICK_FLAGS = auto()
    SIDE = auto()
    ORDER_REQUEST = auto()


@dataclass(frozen=True)
class Param:
    name: str
    kind: Kind
    required: bool = True


type Params = dict[str, object]


def _bad(message: str) -> BridgeError:
    return BridgeError(ErrorType.BAD_REQUEST, message)


def _as_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(f"'{name}' must be an integer")
    return value


def _as_bounded_int(name: str, value: object, high: int) -> int:
    result = _as_int(name, value)
    if not 0 <= result <= high:
        raise _bad(f"'{name}' must be between 0 and {high}")
    return result


def _as_float(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _bad(f"'{name}' must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise _bad(f"'{name}' must be finite")
    return result


def _as_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(f"'{name}' must be a non-empty string")
    return value


def _as_choice(name: str, value: object, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise _bad(f"'{name}' must be one of {', '.join(choices)}")
    return value


def _as_time(name: str, value: object) -> datetime:
    seconds = _as_int(name, value)
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise _bad(f"'{name}' is not a valid epoch timestamp") from exc


def validate_order_request(raw: object) -> Params:
    """Validate an order request dict; enum fields stay as names until resolved."""
    if not isinstance(raw, dict):
        raise _bad("'request' must be an object")
    known = {*ORDER_ENUMS, *_ORDER_INTS, *_ORDER_FLOATS, *_ORDER_STRS}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise _bad(f"unknown request keys: {', '.join(unknown)}")
    if "action" not in raw:
        raise _bad("request is missing 'action'")

    clean: Params = {}
    for key, value in raw.items():
        if key in ORDER_ENUMS:
            clean[key] = _as_choice(f"request.{key}", value, ORDER_ENUMS[key][1])
        elif key in _ORDER_INTS:
            clean[key] = _as_bounded_int(f"request.{key}", value, _ORDER_INT_MAX[key])
        elif key in _ORDER_FLOATS:
            number = _as_float(f"request.{key}", value)
            if number < 0:
                raise _bad(f"'request.{key}' must be >= 0")
            clean[key] = number
        else:
            if not isinstance(value, str):
                raise _bad(f"'request.{key}' must be a string")
            clean[key] = value
    return clean


def build_order_request(clean: Params, resolve: Callable[[str], int]) -> Params:
    """Map validated enum names onto MT5 constants via ``resolve``."""
    return {
        key: resolve(ORDER_ENUMS[key][0] + str(value)) if key in ORDER_ENUMS else value
        for key, value in clean.items()
    }


def _validate_one(param: Param, value: object) -> object:
    name = param.name
    match param.kind:
        case Kind.STR:
            return _as_str(name, value)
        case Kind.INT:
            return _as_int(name, value)
        case Kind.TICKET:
            return _as_bounded_int(name, value, I64_MAX)
        case Kind.FLOAT:
            return _as_float(name, value)
        case Kind.BOOL:
            if not isinstance(value, bool):
                raise _bad(f"'{name}' must be a boolean")
            return value
        case Kind.TIME:
            return _as_time(name, value)
        case Kind.COUNT:
            count = _as_int(name, value)
            if not 1 <= count <= MAX_COUNT:
                raise _bad(f"'{name}' must be between 1 and {MAX_COUNT}")
            return count
        case Kind.START_POS:
            pos = _as_int(name, value)
            if pos < 0:
                raise _bad(f"'{name}' must be >= 0")
            return pos
        case Kind.TIMEFRAME:
            return _as_choice(name, value, TIMEFRAMES)
        case Kind.TICK_FLAGS:
            return _as_choice(name, value, TICK_FLAGS)
        case Kind.SIDE:
            return _as_choice(name, value, SIDES)
        case Kind.ORDER_REQUEST:
            return validate_order_request(value)


def validate_params(
    specs: tuple[Param, ...], raw: Mapping[str, object], defaults: Mapping[str, object]
) -> Params:
    """Return normalised params: unknown or missing keys and wrong types are bad_request."""
    allowed = {spec.name for spec in specs}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _bad(f"unknown params: {', '.join(unknown)}")
    clean: Params = {}
    for spec in specs:
        if spec.name in raw and raw[spec.name] is not None:
            clean[spec.name] = _validate_one(spec, raw[spec.name])
        elif spec.name in defaults:
            clean[spec.name] = defaults[spec.name]
        elif spec.required:
            raise _bad(f"missing required param '{spec.name}'")
    return clean


def required_constants() -> tuple[str, ...]:
    """Every MetaTrader5 constant name the schemas can ask the gateway to resolve."""
    return (
        *("TIMEFRAME_" + name for name in TIMEFRAMES),
        *("COPY_TICKS_" + name for name in TICK_FLAGS),
        *("ORDER_TYPE_" + name for name in SIDES),
        *(prefix + name for prefix, names in ORDER_ENUMS.values() for name in names),
    )
