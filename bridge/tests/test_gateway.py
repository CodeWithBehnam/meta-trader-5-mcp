from collections import namedtuple

import numpy as np
import pytest

from mt5_bridge.config import Config
from mt5_bridge.gateway import MAX_RESULT_ROWS, Gateway
from mt5_bridge.protocol import BridgeError, ErrorType, JsonValue
from tests.fake_mt5 import RATE_DTYPE, AccountInfo, FakeMt5, OrderCheckResult, TradeRequest, as_obj


def make(allow_trading: bool = False) -> tuple[FakeMt5, Gateway]:
    mt5 = FakeMt5()
    return mt5, Gateway(
        mt5, Config(token="t", allow_trading=allow_trading, terminal_path="C:\\t.exe")
    )


class Sleeper:
    """Records requested pauses and the lock state at that moment instead of sleeping."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.gateway: Gateway | None = None
        self.lock_held_while_sleeping = False

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.gateway is not None and self.gateway._lock.locked():
            self.lock_held_while_sleeping = True


NOW = 1_800_000_000.0
RANGE_PARAMS: dict[str, JsonValue] = {
    "symbol": "EURGBP",
    "timeframe": "H1",
    "date_from": int(NOW) - 7200,
    "date_to": int(NOW) - 60,
}


def make_warm() -> tuple[FakeMt5, Gateway, Sleeper]:
    mt5 = FakeMt5()
    sleeper = Sleeper()
    gateway = Gateway(
        mt5, Config(token="t", terminal_path="C:\\t.exe"), sleep=sleeper, clock=lambda: NOW
    )
    sleeper.gateway = gateway
    return mt5, gateway, sleeper


def error_of(gateway: Gateway, method: str, params: dict) -> BridgeError:
    with pytest.raises(BridgeError) as info:
        gateway.handle(method, params)
    return info.value


def test_ping_does_not_initialise() -> None:
    mt5, gateway = make()
    result = as_obj(gateway.handle("ping", {}))
    assert result["initialized"] is False
    assert result["trading_allowed_by_bridge"] is False
    assert result["mt5_package"] == "0.0-fake"
    assert mt5.init_count == 0


def test_lazy_initialize_once_with_path_and_timeout() -> None:
    mt5, gateway = make()
    mt5.script["account_info"] = [AccountInfo(1, 2.5, float("nan"), "n")]
    assert gateway.handle("account_info", {}) == {
        "login": 1,
        "balance": 2.5,
        "equity": None,
        "name": "n",
    }
    gateway.handle("account_info", {})
    assert mt5.init_count == 1
    assert mt5.calls[0] == ("initialize", (), {"path": "C:\\t.exe", "timeout": 60000})
    assert as_obj(gateway.handle("ping", {}))["initialized"] is True


def test_initialize_failure_is_not_connected_and_retried_next_call() -> None:
    mt5, gateway = make()
    mt5.initialize_ok = False
    err = error_of(gateway, "account_info", {})
    assert err.error_type is ErrorType.NOT_CONNECTED
    assert err.code == -10003
    mt5.initialize_ok = True
    mt5.script["account_info"] = [AccountInfo(1, 1.0, 1.0, "n")]
    gateway.handle("account_info", {})
    assert mt5.init_count == 2


def test_none_result_is_mt5_error_with_last_error() -> None:
    mt5, gateway = make()
    mt5.error_after_none["symbol_info"] = (-1, "Terminal: Unknown symbol")
    err = error_of(gateway, "symbol_info", {"symbol": "NOPE"})
    assert (err.error_type, err.code, err.message) == (
        ErrorType.MT5_ERROR,
        -1,
        "Terminal: Unknown symbol",
    )


def test_none_with_success_code_is_empty_list_for_list_methods_only() -> None:
    _, gateway = make()
    assert gateway.handle("positions_get", {}) == []
    err = error_of(gateway, "account_info", {})
    assert err.error_type is ErrorType.MT5_ERROR


def test_ipc_error_reinitialises_and_retries_once() -> None:
    mt5, gateway = make()
    mt5.script["symbols_total"] = [None, 42]
    mt5.error_after_none["symbols_total"] = (-10004, "connect failed")
    assert gateway.handle("symbols_total", {}) == 42
    assert mt5.init_count == 2
    assert mt5.shutdown_count == 1


def test_ipc_error_twice_is_reported() -> None:
    mt5, gateway = make()
    mt5.error_after_none["symbols_total"] = (-10005, "timeout")
    err = error_of(gateway, "symbols_total", {})
    assert (err.error_type, err.code) == (ErrorType.MT5_ERROR, -10005)
    assert mt5.init_count == 2


def test_rates_call_arguments_and_conversion() -> None:
    mt5, gateway = make()
    mt5.script["copy_rates_from_pos"] = [np.array([(1, 1.0, 2.0, 3)], dtype=RATE_DTYPE)]
    rows = gateway.handle(
        "copy_rates_from_pos",
        {"symbol": "EURUSD", "timeframe": "H1", "start_pos": 0, "count": 1},
    )
    assert rows == [{"time": 1, "open": 1.0, "close": 2.0, "tick_volume": 3}]
    assert mt5.calls[-1] == ("copy_rates_from_pos", ("EURUSD", 16385, 0, 1), {})


def test_history_and_filter_kwargs() -> None:
    mt5, gateway = make()
    gateway.handle("positions_get", {"symbol": "EURUSD"})
    assert mt5.calls[-1] == ("positions_get", (), {"symbol": "EURUSD"})
    gateway.handle("history_deals_get", {"ticket": 5})
    assert mt5.calls[-1] == ("history_deals_get", (), {"ticket": 5})
    gateway.handle("history_deals_get", {"date_from": 0, "date_to": 60, "group": "*"})
    name, args, kwargs = mt5.calls[-1]
    assert (name, len(args), kwargs) == ("history_deals_get", 2, {"group": "*"})


def test_order_check_maps_enums_and_nests_request() -> None:
    mt5, gateway = make()
    request = TradeRequest(1, "EURUSD", 0.01)
    mt5.script["order_check"] = [OrderCheckResult(0, 12.5, request)]
    result = as_obj(
        gateway.handle(
            "order_check",
            {"request": {"action": "DEAL", "type": "BUY", "symbol": "EURUSD", "volume": 0.01}},
        )
    )
    assert result["request"] == {"action": 1, "symbol": "EURUSD", "volume": 0.01}
    assert mt5.calls[-1][1] == ({"action": 1, "type": 0, "symbol": "EURUSD", "volume": 0.01},)


def test_order_send_blocked_without_flag_and_never_reaches_mt5() -> None:
    mt5, gateway = make(allow_trading=False)
    err = error_of(gateway, "order_send", {"request": {"action": "DEAL"}})
    assert err.error_type is ErrorType.TRADING_DISABLED
    assert not any(call[0] == "order_send" for call in mt5.calls)
    assert mt5.init_count == 0


def test_order_send_allowed_with_flag() -> None:
    mt5, gateway = make(allow_trading=True)
    mt5.script["order_send"] = [OrderCheckResult(10009, 0.0, TradeRequest(1, "A", 1.0))]
    sent = as_obj(gateway.handle("order_send", {"request": {"action": "DEAL"}}))
    assert sent["retcode"] == 10009


def test_validation_precedes_trading_gate_and_unknown_method() -> None:
    _, gateway = make()
    assert error_of(gateway, "order_send", {}).error_type is ErrorType.BAD_REQUEST
    assert error_of(gateway, "nope", {}).error_type is ErrorType.UNKNOWN_METHOD


def test_shutdown_only_when_initialised() -> None:
    mt5, gateway = make()
    gateway.shutdown()
    assert mt5.shutdown_count == 0
    mt5.script["symbols_total"] = [1]
    gateway.handle("symbols_total", {})
    gateway.shutdown()
    assert mt5.shutdown_count == 1


def test_missing_constant_is_bad_request_not_internal() -> None:
    mt5 = FakeMt5()
    mt5.__dict__["TIMEFRAME_H1"] = None
    gateway = Gateway(mt5, Config(token="t"))
    err = error_of(
        gateway,
        "copy_rates_from_pos",
        {"symbol": "A", "timeframe": "H1", "start_pos": 0, "count": 1},
    )
    assert err.error_type is ErrorType.BAD_REQUEST
    assert "not supported by this MetaTrader5 build" in err.message
    assert not any(call[0] == "copy_rates_from_pos" for call in mt5.calls)


def test_missing_constants_are_logged_at_start(caplog: pytest.LogCaptureFixture) -> None:
    mt5 = FakeMt5()
    mt5.__dict__["TIMEFRAME_H1"] = None
    with caplog.at_level("WARNING"):
        Gateway(mt5, Config(token="t"))
    assert any("TIMEFRAME_H1" in r.message and r.levelname == "WARNING" for r in caplog.records)


def test_version_is_reshaped_to_a_dict() -> None:
    mt5, gateway = make()
    mt5.script["version"] = [(500, 6230, "25 Sep 2026")]
    assert gateway.handle("version", {}) == {"release": 500, "build": 6230, "date": "25 Sep 2026"}


def test_order_send_is_never_retried_after_ipc_error() -> None:
    mt5, gateway = make(allow_trading=True)
    mt5.script["order_send"] = [None, OrderCheckResult(10009, 0.0, TradeRequest(1, "A", 1.0))]
    mt5.error_after_none["order_send"] = (-10005, "timeout")
    err = error_of(gateway, "order_send", {"request": {"action": "DEAL"}})
    assert (err.error_type, err.code) == (ErrorType.MT5_ERROR, -10005)
    assert "outcome is unknown" in err.message
    assert "positions_get" in err.message and "history_deals_get" in err.message
    assert sum(call[0] == "order_send" for call in mt5.calls) == 1


def test_order_check_still_retries_after_ipc_error() -> None:
    mt5, gateway = make()
    mt5.script["order_check"] = [None, OrderCheckResult(0, 1.0, TradeRequest(1, "A", 1.0))]
    mt5.error_after_none["order_check"] = (-10005, "timeout")
    gateway.handle("order_check", {"request": {"action": "DEAL"}})
    assert sum(call[0] == "order_check" for call in mt5.calls) == 2


@pytest.mark.parametrize("exc", [OverflowError("too big"), TypeError("bad"), SystemError("x")])
@pytest.mark.parametrize("method", ["order_check", "order_send"])
def test_c_layer_errors_on_orders_are_bad_request(method: str, exc: Exception) -> None:
    mt5, gateway = make(allow_trading=True)
    mt5.raises[method] = exc
    err = error_of(gateway, method, {"request": {"action": "DEAL"}})
    assert err.error_type is ErrorType.BAD_REQUEST


def test_c_layer_errors_elsewhere_stay_internal() -> None:
    mt5, gateway = make()
    mt5.raises["symbol_info"] = TypeError("boom")
    with pytest.raises(TypeError):
        gateway.handle("symbol_info", {"symbol": "A"})


def test_empty_rates_are_retried_after_selecting_the_symbol() -> None:
    mt5, gateway, sleeper = make_warm()
    mt5.script["copy_rates_range"] = [None, np.array([(1, 1.0, 2.0, 3)], dtype=RATE_DTYPE)]
    rows = gateway.handle("copy_rates_range", RANGE_PARAMS)
    assert rows == [{"time": 1, "open": 1.0, "close": 2.0, "tick_volume": 3}]
    assert ("symbol_select", ("EURGBP", True), {}) in mt5.calls
    assert sleeper.delays == [0.5]
    assert not sleeper.lock_held_while_sleeping


def test_warmup_gives_up_with_empty_list_after_two_retries() -> None:
    mt5, gateway, sleeper = make_warm()
    params = {k: v for k, v in RANGE_PARAMS.items() if k != "timeframe"}
    assert gateway.handle("copy_ticks_range", params) == []
    assert sleeper.delays == [0.5, 1.0]
    assert sum(call[0] == "copy_ticks_range" for call in mt5.calls) == 3
    assert not sleeper.lock_held_while_sleeping


def test_warmup_skips_select_for_visible_symbol() -> None:
    mt5, gateway, _ = make_warm()
    mt5.script["symbol_info"] = [namedtuple("Info", "visible")(True)]
    gateway.handle("copy_rates_range", RANGE_PARAMS)
    assert not any(call[0] == "symbol_select" for call in mt5.calls)


@pytest.mark.parametrize(
    "overrides",
    [
        {"date_from": int(NOW) + 3600, "date_to": int(NOW) + 7200},  # wholly in the future
        {"date_to": int(NOW) - 7200},  # empty window
    ],
)
def test_no_warmup_when_no_data_is_expected(overrides: dict[str, JsonValue]) -> None:
    mt5, gateway, sleeper = make_warm()
    assert gateway.handle("copy_rates_range", RANGE_PARAMS | overrides) == []
    assert sleeper.delays == []
    assert not any(call[0] == "symbol_select" for call in mt5.calls)


def test_warmup_applies_to_from_pos_but_not_other_lists() -> None:
    _, gateway, sleeper = make_warm()
    gateway.handle(
        "copy_rates_from_pos", {"symbol": "A", "timeframe": "H1", "start_pos": 0, "count": 5}
    )
    assert sleeper.delays == [0.5, 1.0]
    sleeper.delays.clear()
    gateway.handle("positions_get", {})
    assert sleeper.delays == []


def test_oversized_copy_results_are_truncated(caplog: pytest.LogCaptureFixture) -> None:
    mt5, gateway, _ = make_warm()
    big = np.zeros(MAX_RESULT_ROWS + 5, dtype=RATE_DTYPE)
    mt5.script["copy_rates_range"] = [big]
    with caplog.at_level("WARNING"):
        rows = gateway.handle("copy_rates_range", RANGE_PARAMS)
    assert isinstance(rows, list) and len(rows) == MAX_RESULT_ROWS
    assert any("truncating" in r.message for r in caplog.records)
