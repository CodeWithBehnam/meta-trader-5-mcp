from datetime import UTC, datetime

import pytest

from mt5_bridge.methods import METHODS
from mt5_bridge.protocol import BridgeError, ErrorType
from mt5_bridge.schemas import (
    MAX_COUNT,
    TIMEFRAMES,
    build_order_request,
    required_constants,
    validate_order_request,
    validate_params,
)


def check(method: str, raw: dict[str, object]) -> dict[str, object]:
    spec = METHODS[method]
    params = validate_params(spec.params, raw, spec.defaults)
    if spec.check:
        spec.check(params)
    return params


def assert_bad(method: str, raw: dict[str, object]) -> None:
    with pytest.raises(BridgeError) as info:
        check(method, raw)
    assert info.value.error_type is ErrorType.BAD_REQUEST


def test_times_become_utc_datetimes() -> None:
    params = check(
        "copy_rates_from", {"symbol": "EURUSD", "timeframe": "H1", "date_from": 0, "count": 5}
    )
    assert params["date_from"] == datetime(1970, 1, 1, tzinfo=UTC)


def test_defaults_applied() -> None:
    assert check("symbol_select", {"symbol": "EURUSD"})["enable"] is True
    assert check("copy_ticks_from", {"symbol": "A", "date_from": 1, "count": 1})["flags"] == "ALL"


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_all_timeframes_accepted(timeframe: str) -> None:
    check(
        "copy_rates_from_pos", {"symbol": "A", "timeframe": timeframe, "start_pos": 0, "count": 1}
    )


@pytest.mark.parametrize(
    ("method", "raw"),
    [
        ("symbol_info", {}),
        ("symbol_info", {"symbol": ""}),
        ("symbol_info", {"symbol": "A", "extra": 1}),
        ("copy_rates_from_pos", {"symbol": "A", "timeframe": "H7", "start_pos": 0, "count": 1}),
        ("copy_rates_from_pos", {"symbol": "A", "timeframe": "H1", "start_pos": -1, "count": 1}),
        ("copy_rates_from_pos", {"symbol": "A", "timeframe": "H1", "start_pos": 0, "count": 0}),
        (
            "copy_rates_from_pos",
            {"symbol": "A", "timeframe": "H1", "start_pos": 0, "count": MAX_COUNT + 1},
        ),
        ("copy_rates_from_pos", {"symbol": "A", "timeframe": "H1", "start_pos": True, "count": 1}),
        ("copy_ticks_from", {"symbol": "A", "date_from": 1, "count": 1, "flags": "NONE"}),
        ("copy_ticks_from", {"symbol": "A", "date_from": "yesterday", "count": 1}),
        ("symbol_select", {"symbol": "A", "enable": "yes"}),
        ("order_calc_margin", {"action": "HOLD", "symbol": "A", "volume": 1, "price": 1}),
        ("order_calc_margin", {"action": "BUY", "symbol": "A", "volume": "1", "price": 1}),
        ("orders_get", {"symbol": "A", "group": "*"}),
        ("history_deals_get", {}),
        ("history_deals_get", {"date_from": 1}),
        ("history_deals_get", {"ticket": 1, "position": 2}),
        ("history_deals_get", {"ticket": 1, "date_from": 1, "date_to": 2}),
    ],
)
def test_invalid_params(method: str, raw: dict[str, object]) -> None:
    assert_bad(method, raw)


@pytest.mark.parametrize(
    ("method", "raw"),
    [
        ("positions_get", {}),
        ("positions_get", {"ticket": 5}),
        ("history_deals_get", {"date_from": 1, "date_to": 2, "group": "*USD*"}),
        ("history_orders_get", {"ticket": 9}),
        ("history_deals_get", {"position": 9}),
    ],
)
def test_valid_param_forms(method: str, raw: dict[str, object]) -> None:
    check(method, raw)


def test_order_request_validation_and_mapping() -> None:
    clean = validate_order_request(
        {
            "action": "DEAL",
            "type": "BUY",
            "symbol": "EURUSD",
            "volume": 1,
            "type_filling": "IOC",
            "type_time": "GTC",
            "expiration": 0,
            "magic": 7,
            "comment": "hi",
        }
    )
    assert clean["volume"] == 1.0
    mapped = build_order_request(
        clean,
        {
            "TRADE_ACTION_DEAL": 1,
            "ORDER_TYPE_BUY": 0,
            "ORDER_FILLING_IOC": 1,
            "ORDER_TIME_GTC": 0,
        }.__getitem__,
    )
    assert mapped["action"] == 1
    assert mapped["type"] == 0
    assert mapped["type_filling"] == 1
    assert mapped["symbol"] == "EURUSD"


@pytest.mark.parametrize(
    "request_",
    [
        "x",
        {},
        {"action": "DEAL", "bogus": 1},
        {"action": "BUY"},
        {"action": "DEAL", "type": "HODL"},
        {"action": "DEAL", "volume": "1"},
        {"action": "DEAL", "magic": 1.5},
        {"action": "DEAL", "comment": 3},
        {"action": "DEAL", "volume": float("nan")},
    ],
)
def test_invalid_order_requests(request_: object) -> None:
    with pytest.raises(BridgeError) as info:
        validate_order_request(request_)
    assert info.value.error_type is ErrorType.BAD_REQUEST


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("magic", 2**32),
        ("magic", -1),
        ("deviation", 2**32),
        ("order", 2**63),
        ("position", -1),
        ("position_by", 2**63),
        ("volume", -0.1),
        ("price", -1.0),
        ("sl", -1.0),
        ("tp", float("inf")),
    ],
)
def test_order_numbers_are_bounded(key: str, value: object) -> None:
    with pytest.raises(BridgeError) as info:
        validate_order_request({"action": "DEAL", key: value})
    assert info.value.error_type is ErrorType.BAD_REQUEST


def test_order_numbers_at_the_limits_are_accepted() -> None:
    request = {"action": "DEAL", "magic": 2**32 - 1, "order": 2**63 - 1, "volume": 0, "tp": 0.0}
    assert validate_order_request(request)["magic"] == 2**32 - 1


@pytest.mark.parametrize("name", ["ticket", "position"])
def test_history_tickets_are_bounded(name: str) -> None:
    with pytest.raises(BridgeError):
        validate_params(METHODS["history_deals_get"].params, {name: 2**63}, {})


def test_required_constants_cover_every_enum_name() -> None:
    names = required_constants()
    assert "TIMEFRAME_H1" in names and "ORDER_TIME_SPECIFIED" in names
    assert "COPY_TICKS_ALL" in names and "TRADE_ACTION_DEAL" in names


@pytest.mark.parametrize(
    ("method", "raw"),
    [
        ("history_deals_get", {"date_from": 100, "date_to": 50}),
        ("history_orders_get", {"date_from": 100, "date_to": 50}),
        ("history_deals_total", {"date_from": 100, "date_to": 50}),
        ("history_orders_total", {"date_from": 100, "date_to": 50}),
        ("copy_rates_range", {"symbol": "A", "timeframe": "H1", "date_from": 100, "date_to": 50}),
        ("copy_ticks_range", {"symbol": "A", "date_from": 100, "date_to": 50}),
    ],
)
def test_inverted_date_ranges_are_bad_request(method: str, raw: dict[str, object]) -> None:
    assert_bad(method, raw)


def test_equal_date_bounds_are_accepted() -> None:
    check("history_deals_get", {"date_from": 100, "date_to": 100})


def test_expiration_stays_an_integer_epoch() -> None:
    clean = validate_order_request({"action": "PENDING", "expiration": 1_800_000_000})
    assert clean["expiration"] == 1_800_000_000
    assert type(clean["expiration"]) is int
    with pytest.raises(BridgeError):
        validate_order_request({"action": "PENDING", "expiration": 1.5})
