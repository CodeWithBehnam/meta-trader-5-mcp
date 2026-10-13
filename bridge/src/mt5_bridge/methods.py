"""Method table: per-method param schema and how params map onto a MetaTrader5 call. Pure."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import cast

from mt5_bridge.protocol import BridgeError, ErrorType
from mt5_bridge.schemas import Kind, Param, Params, build_order_request

type Resolver = Callable[[str], int]


class ResultKind(Enum):
    VALUE = auto()
    # A list result: MT5 returns None with last_error code 1 when it is empty.
    LIST = auto()


@dataclass(frozen=True)
class Call:
    args: tuple[object, ...] = ()
    kwargs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MethodSpec:
    name: str
    params: tuple[Param, ...]
    build: Callable[[Params, Resolver], Call]
    # None marks a bridge-local method (ping) that never touches MT5.
    mt5_name: str | None = None
    result: ResultKind = ResultKind.VALUE
    defaults: Mapping[str, object] = field(default_factory=dict)
    check: Callable[[Params], None] | None = None
    requires_trading: bool = False
    # Reshapes a raw MT5 result (e.g. a bare tuple) before JSON conversion.
    reshape: Callable[[object], object] | None = None
    # Set for read-only copy_* calls whose first answer can be empty while the terminal is
    # still loading history. The predicate says whether the request should contain data.
    warmup: Callable[[Params, datetime], bool] | None = None


def _bad(message: str) -> BridgeError:
    return BridgeError(ErrorType.BAD_REQUEST, message)


def _picked(params: Params, names: tuple[str, ...]) -> dict[str, object]:
    return {name: params[name] for name in names if name in params}


def _at_most_one(*names: str) -> Callable[[Params], None]:
    def check(params: Params) -> None:
        given = [name for name in names if name in params]
        if len(given) > 1:
            raise _bad(f"provide at most one of: {', '.join(names)}")

    return check


def _check_date_order(params: Params) -> None:
    # MT5 answers an inverted range with an opaque error or empty data.
    start, end = params.get("date_from"), params.get("date_to")
    if isinstance(start, datetime) and isinstance(end, datetime) and end < start:
        raise _bad("'date_to' must not be before 'date_from'")


def _expects_rows(_params: Params, _now: datetime) -> bool:
    # from/from_pos requests always ask for at least one row.
    return True


def _range_expects_rows(params: Params, now: datetime) -> bool:
    start, end = params["date_from"], params["date_to"]
    return isinstance(start, datetime) and isinstance(end, datetime) and start < end and start < now


def _check_history(params: Params) -> None:
    _check_date_order(params)
    has_range = "date_from" in params or "date_to" in params
    singles = [name for name in ("ticket", "position") if name in params]
    if len(singles) > 1 or (singles and (has_range or "group" in params)):
        raise _bad("use either {date_from, date_to, group?}, {ticket} or {position}")
    if not singles and not ("date_from" in params and "date_to" in params):
        raise _bad("provide {date_from, date_to}, {ticket} or {position}")


def _version_dict(raw: object) -> object:
    """mt5.version() returns (terminal version, build, release date) as a bare tuple."""
    if not (isinstance(raw, tuple) and len(raw) == 3):
        raise BridgeError(ErrorType.INTERNAL, f"unexpected version() result: {raw!r}")
    return {"release": raw[0], "build": raw[1], "date": raw[2]}


def _no_call(_params: Params, _resolve: Resolver) -> Call:
    return Call()


def _symbol_only(params: Params, _resolve: Resolver) -> Call:
    return Call(args=(params["symbol"],))


def _kwargs_of(*names: str) -> Callable[[Params, Resolver], Call]:
    def build(params: Params, _resolve: Resolver) -> Call:
        return Call(kwargs=_picked(params, names))

    return build


def _timeframe(params: Params, resolve: Resolver) -> int:
    return resolve("TIMEFRAME_" + str(params["timeframe"]))


def _tick_flags(params: Params, resolve: Resolver) -> int:
    return resolve("COPY_TICKS_" + str(params["flags"]))


def _side(params: Params, resolve: Resolver) -> int:
    return resolve("ORDER_TYPE_" + str(params["action"]))


def _build_history(params: Params, _resolve: Resolver) -> Call:
    if "ticket" in params or "position" in params:
        return Call(kwargs=_picked(params, ("ticket", "position")))
    return Call(args=(params["date_from"], params["date_to"]), kwargs=_picked(params, ("group",)))


def _build_order(params: Params, resolve: Resolver) -> Call:
    request = cast(Params, params["request"])
    return Call(args=(build_order_request(request, resolve),))


SYMBOL = Param("symbol", Kind.STR)
TIMEFRAME = Param("timeframe", Kind.TIMEFRAME)
COUNT = Param("count", Kind.COUNT)
DATE_FROM = Param("date_from", Kind.TIME)
DATE_TO = Param("date_to", Kind.TIME)
REQUEST = Param("request", Kind.ORDER_REQUEST)
_HISTORY_PARAMS = (
    Param("date_from", Kind.TIME, required=False),
    Param("date_to", Kind.TIME, required=False),
    Param("group", Kind.STR, required=False),
    Param("ticket", Kind.TICKET, required=False),
    Param("position", Kind.TICKET, required=False),
)
_FILTER_PARAMS = (
    Param("symbol", Kind.STR, required=False),
    Param("group", Kind.STR, required=False),
    Param("ticket", Kind.TICKET, required=False),
)


def _simple(name: str, result: ResultKind = ResultKind.VALUE) -> MethodSpec:
    return MethodSpec(name, (), _no_call, mt5_name=name, result=result)


def _specs() -> tuple[MethodSpec, ...]:
    return (
        MethodSpec("ping", (), _no_call),
        MethodSpec("version", (), _no_call, mt5_name="version", reshape=_version_dict),
        _simple("terminal_info"),
        _simple("account_info"),
        _simple("symbols_total"),
        MethodSpec(
            "symbols_get",
            (Param("group", Kind.STR, required=False),),
            _kwargs_of("group"),
            mt5_name="symbols_get",
            result=ResultKind.LIST,
        ),
        MethodSpec("symbol_info", (SYMBOL,), _symbol_only, mt5_name="symbol_info"),
        MethodSpec("symbol_info_tick", (SYMBOL,), _symbol_only, mt5_name="symbol_info_tick"),
        MethodSpec(
            "symbol_select",
            (SYMBOL, Param("enable", Kind.BOOL, required=False)),
            lambda p, _r: Call(args=(p["symbol"], p["enable"])),
            mt5_name="symbol_select",
            defaults={"enable": True},
        ),
        MethodSpec(
            "copy_rates_from",
            (SYMBOL, TIMEFRAME, DATE_FROM, COUNT),
            lambda p, r: Call(args=(p["symbol"], _timeframe(p, r), p["date_from"], p["count"])),
            mt5_name="copy_rates_from",
            result=ResultKind.LIST,
            warmup=_expects_rows,
        ),
        MethodSpec(
            "copy_rates_from_pos",
            (SYMBOL, TIMEFRAME, Param("start_pos", Kind.START_POS), COUNT),
            lambda p, r: Call(args=(p["symbol"], _timeframe(p, r), p["start_pos"], p["count"])),
            mt5_name="copy_rates_from_pos",
            result=ResultKind.LIST,
            warmup=_expects_rows,
        ),
        MethodSpec(
            "copy_rates_range",
            (SYMBOL, TIMEFRAME, DATE_FROM, DATE_TO),
            lambda p, r: Call(args=(p["symbol"], _timeframe(p, r), p["date_from"], p["date_to"])),
            mt5_name="copy_rates_range",
            result=ResultKind.LIST,
            warmup=_range_expects_rows,
            check=_check_date_order,
        ),
        MethodSpec(
            "copy_ticks_from",
            (SYMBOL, DATE_FROM, COUNT, Param("flags", Kind.TICK_FLAGS, required=False)),
            lambda p, r: Call(args=(p["symbol"], p["date_from"], p["count"], _tick_flags(p, r))),
            mt5_name="copy_ticks_from",
            result=ResultKind.LIST,
            warmup=_expects_rows,
            defaults={"flags": "ALL"},
        ),
        MethodSpec(
            "copy_ticks_range",
            (SYMBOL, DATE_FROM, DATE_TO, Param("flags", Kind.TICK_FLAGS, required=False)),
            lambda p, r: Call(args=(p["symbol"], p["date_from"], p["date_to"], _tick_flags(p, r))),
            mt5_name="copy_ticks_range",
            result=ResultKind.LIST,
            warmup=_range_expects_rows,
            check=_check_date_order,
            defaults={"flags": "ALL"},
        ),
        _simple("orders_total"),
        _simple("positions_total"),
        MethodSpec(
            "orders_get",
            _FILTER_PARAMS,
            _kwargs_of("symbol", "group", "ticket"),
            mt5_name="orders_get",
            result=ResultKind.LIST,
            check=_at_most_one("symbol", "group", "ticket"),
        ),
        MethodSpec(
            "positions_get",
            _FILTER_PARAMS,
            _kwargs_of("symbol", "group", "ticket"),
            mt5_name="positions_get",
            result=ResultKind.LIST,
            check=_at_most_one("symbol", "group", "ticket"),
        ),
        MethodSpec(
            "history_orders_get",
            _HISTORY_PARAMS,
            _build_history,
            mt5_name="history_orders_get",
            result=ResultKind.LIST,
            check=_check_history,
        ),
        MethodSpec(
            "history_deals_get",
            _HISTORY_PARAMS,
            _build_history,
            mt5_name="history_deals_get",
            result=ResultKind.LIST,
            check=_check_history,
        ),
        MethodSpec(
            "history_orders_total",
            (DATE_FROM, DATE_TO),
            lambda p, _r: Call(args=(p["date_from"], p["date_to"])),
            mt5_name="history_orders_total",
            check=_check_date_order,
        ),
        MethodSpec(
            "history_deals_total",
            (DATE_FROM, DATE_TO),
            lambda p, _r: Call(args=(p["date_from"], p["date_to"])),
            mt5_name="history_deals_total",
            check=_check_date_order,
        ),
        MethodSpec(
            "order_calc_margin",
            (
                Param("action", Kind.SIDE),
                SYMBOL,
                Param("volume", Kind.FLOAT),
                Param("price", Kind.FLOAT),
            ),
            lambda p, r: Call(args=(_side(p, r), p["symbol"], p["volume"], p["price"])),
            mt5_name="order_calc_margin",
        ),
        MethodSpec(
            "order_calc_profit",
            (
                Param("action", Kind.SIDE),
                SYMBOL,
                Param("volume", Kind.FLOAT),
                Param("price_open", Kind.FLOAT),
                Param("price_close", Kind.FLOAT),
            ),
            lambda p, r: Call(
                args=(_side(p, r), p["symbol"], p["volume"], p["price_open"], p["price_close"])
            ),
            mt5_name="order_calc_profit",
        ),
        MethodSpec("order_check", (REQUEST,), _build_order, mt5_name="order_check"),
        MethodSpec(
            "order_send", (REQUEST,), _build_order, mt5_name="order_send", requires_trading=True
        ),
    )


METHODS: Mapping[str, MethodSpec] = {spec.name: spec for spec in _specs()}
