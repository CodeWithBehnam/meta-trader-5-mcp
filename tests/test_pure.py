"""Tests for the pure modules: config, timeconv, enums, guards, orders, planning, shaping."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mt5_mcp import enums, planning, shaping, timeconv
from mt5_mcp.config import ConfigError, load_config, to_wine_path
from mt5_mcp.guards import (
    Allowed,
    CancelOrder,
    ClosePosition,
    Filling,
    MarketOrder,
    ModifyPendingOrder,
    ModifyPosition,
    PendingOrder,
    PendingType,
    Rejected,
    Side,
    TimeType,
    check_intent,
)
from mt5_mcp.orders import build_request, opposite, pending_type_for, pick_filling

HOME = Path("/home/u")
PKG = Path("/repo/src/mt5_mcp/config.py")


def cfg(**env: str):
    return load_config(env, home=HOME, package_file=PKG)


# --- config -------------------------------------------------------------------------------


def test_config_defaults() -> None:
    c = cfg()
    assert c.app_path == Path("/Applications/MetaTrader 5.app")
    assert c.wineprefix == HOME / "Library/Application Support/net.metaquotes.wine.metatrader5"
    assert (c.bridge_host, c.bridge_port) == ("127.0.0.1", 18812)
    assert c.bridge_autostart is True
    assert c.enable_trading is False
    assert c.allowed_symbols == frozenset()
    assert (c.max_volume, c.max_bars) == (1.0, 5000)
    assert c.audit_log == HOME / "Library/Logs/mt5-mcp/audit.jsonl"
    assert c.bridge_project == "Z:\\repo\\bridge"
    assert c.uv_exe == "C:\\mt5-bridge\\bin\\uv.exe"
    assert c.wine_binary == Path(
        "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine64"
    )


def test_config_overrides() -> None:
    c = cfg(
        MT5_ENABLE_TRADING="1",
        MT5_ALLOWED_SYMBOLS=" EURUSD, XAUUSD ,,",
        MT5_MAX_VOLUME="0.5",
        MT5_BRIDGE_PROJECT="Z:\\custom",
        MT5_BRIDGE_AUTOSTART="false",
    )
    assert c.enable_trading is True
    assert c.allowed_symbols == {"EURUSD", "XAUUSD"}
    assert c.max_volume == 0.5
    assert c.bridge_project == "Z:\\custom"
    assert c.bridge_autostart is False


@pytest.mark.parametrize(
    "env",
    [
        {"MT5_BRIDGE_PORT": "abc"},
        {"MT5_BRIDGE_PORT": "0"},
        {"MT5_BRIDGE_PORT": "70000"},
        {"MT5_ENABLE_TRADING": "maybe"},
        {"MT5_MAX_VOLUME": "-1"},
        {"MT5_MAX_VOLUME": "nan"},
        {"MT5_MAX_BARS": "0"},
        {"MT5_BRIDGE_HOST": " "},
        {"MT5_ALLOWED_SYMBOLS": " "},
        {"MT5_ALLOWED_SYMBOLS": ","},
        {"MT5_ALLOWED_SYMBOLS": " , ,"},
        {"MT5_MAX_DEVIATION": "-1"},
        {"MT5_MAX_DEVIATION": "1.5"},
        {"MT5_BROKER_UTC_OFFSET_HOURS": "abc"},
        {"MT5_BROKER_UTC_OFFSET_HOURS": "nan"},
        {"MT5_BROKER_UTC_OFFSET_HOURS": "2.1"},
        {"MT5_BROKER_UTC_OFFSET_HOURS": "15"},
        {"MT5_BROKER_UTC_OFFSET_HOURS": "-12.5"},
        {"MT5_AUDIT_LOG": " "},
        {"MT5_AUDIT_LOG": "/tmp"},
    ],
)
def test_config_rejects_bad_values(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        cfg(**env)


def test_config_new_safety_settings() -> None:
    c = cfg()
    assert c.max_deviation == 50 and c.default_deviation == 20
    assert c.broker_utc_offset_seconds is None
    tight = cfg(MT5_MAX_DEVIATION="5")
    assert tight.default_deviation == 5
    assert cfg(MT5_MAX_DEVIATION="0").max_deviation == 0
    assert cfg(MT5_BROKER_UTC_OFFSET_HOURS="3").broker_utc_offset_seconds == 3 * 3600
    assert cfg(MT5_BROKER_UTC_OFFSET_HOURS="5.75").broker_utc_offset_seconds == 20700
    assert cfg(MT5_BROKER_UTC_OFFSET_HOURS="-3.5").broker_utc_offset_seconds == -12600
    assert cfg(MT5_BROKER_UTC_OFFSET_HOURS="").broker_utc_offset_seconds is None
    assert cfg(MT5_ALLOWED_SYMBOLS="").allowed_symbols == frozenset()


def test_to_wine_path_requires_absolute() -> None:
    assert to_wine_path(Path("/Users/a/b")) == "Z:\\Users\\a\\b"
    with pytest.raises(ConfigError):
        to_wine_path(Path("relative"))


# --- timeconv -----------------------------------------------------------------------------


def test_parse_iso_utc() -> None:
    assert timeconv.parse_iso_utc("2026-10-13T08:30:00Z") == datetime(
        2026, 10, 13, 8, 30, tzinfo=UTC
    )
    assert timeconv.parse_iso_utc("2026-10-13T10:30:00+02:00") == datetime(
        2026, 10, 13, 8, 30, tzinfo=UTC
    )
    assert timeconv.parse_iso_utc("2026-10-13").tzinfo is UTC
    for bad in ("", "  ", "yesterday"):
        with pytest.raises(ValueError):
            timeconv.parse_iso_utc(bad)


def test_offset_estimation_rounds_to_half_hour() -> None:
    now = 1_000_000_000.0
    assert timeconv.estimate_broker_offset([int(now) + 3 * 3600 - 7], now) == 3 * 3600
    assert timeconv.estimate_broker_offset([int(now) + 2 * 3600 + 15 * 60 - 20], now) == 8100
    assert timeconv.estimate_broker_offset([int(now) - 30], now) == 0


def test_offset_uses_freshest_tick() -> None:
    now = 1_000_000_000.0
    stale = int(now) + 3 * 3600 - 3600
    fresh = int(now) + 3 * 3600 - 2
    assert timeconv.estimate_broker_offset([stale, fresh], now) == 3 * 3600


def test_offset_not_confident_when_freshest_tick_is_stale() -> None:
    now = 1_000_000_000.0
    # 5 minutes old: it would round to a plausible offset, but the residual is far over 90 s.
    assert timeconv.estimate_broker_offset([int(now) + 3 * 3600 - 300], now) is None
    assert timeconv.estimate_broker_offset([int(now) + 3 * 3600 - 89], now) == 3 * 3600
    assert timeconv.estimate_broker_offset([int(now) + 3 * 3600 + 120], now) is None


def test_offset_none_when_implausible_or_empty() -> None:
    now = 1_000_000_000.0
    assert timeconv.estimate_broker_offset([], now) is None
    assert timeconv.estimate_broker_offset([0], now) is None
    assert timeconv.estimate_broker_offset([int(now) - 2 * 86400], now) is None


def test_time_round_trip_and_formats() -> None:
    moment = datetime(2026, 10, 13, 8, 30, tzinfo=UTC)
    epoch = timeconv.utc_to_broker_epoch(moment, 3 * 3600)
    assert timeconv.broker_iso(epoch) == "2026-10-13T11:30:00"
    assert timeconv.utc_iso(epoch, 3 * 3600) == "2026-10-13T08:30:00Z"
    assert timeconv.utc_iso(epoch, None) is None


def test_annotate_times_handles_msc_zero_and_missing_offset() -> None:
    rec = {"time": 1_000_000_000, "time_msc": 1_000_000_000_500, "time_done": 0, "x": 1}
    out = timeconv.annotate_times(rec, ("time", "time_msc", "time_done", "absent"), 3600)
    assert out["time_utc"] == "2001-09-09T00:46:40Z"
    assert out["time_msc_utc"] == "2001-09-09T00:46:40.500000Z".replace(".500000", ".500000")
    assert "time_done_utc" not in out
    assert out["time_broker"] == "2001-09-09T01:46:40"
    assert out["time"] == 1_000_000_000
    no_offset = timeconv.annotate_times(rec, ("time",), None)
    assert "time_utc" not in no_offset and "time_broker" in no_offset


# --- enums --------------------------------------------------------------------------------


def test_enum_decoding() -> None:
    assert enums.retcode_name(10009) == "DONE"
    assert enums.retcode_name(10008) == "PLACED"
    assert enums.retcode_name(10010) == "DONE_PARTIAL"
    assert enums.retcode_name(10027) == "CLIENT_DISABLES_AT"
    assert enums.retcode_name(10030) == "INVALID_FILL"
    assert enums.retcode_name(99999) == "UNKNOWN_99999"
    assert enums.retcode_name(None) is None
    assert enums.retcode_name(True) is None
    assert enums.is_success_retcode(10009)
    assert not enums.is_success_retcode(10027)
    assert not enums.is_success_retcode(None)
    assert set(enums.RETCODES) == set(enums.RETCODE_DESCRIPTIONS)


def test_annotate_records() -> None:
    deal = enums.annotate("deal", {"type": 1, "entry": 1, "profit": 3.0})
    assert deal["type_name"] == "SELL" and deal["entry_name"] == "OUT" and deal["type"] == 1
    order = enums.annotate("order", {"type": 3, "state": 1, "type_filling": 2, "type_time": 0})
    assert order["type_name"] == "SELL_LIMIT"
    assert order["state_name"] == "PLACED"
    assert order["type_filling_name"] == "RETURN"
    assert order["type_time_name"] == "GTC"
    assert enums.annotate("position", {"type": 0})["type_name"] == "BUY"
    assert "type_name" not in enums.annotate("position", {"volume": 1})


def test_symbol_filling_modes() -> None:
    assert enums.symbol_filling_modes(3) == ["FOK", "IOC"]
    assert enums.symbol_filling_modes(2) == ["IOC"]
    assert enums.symbol_filling_modes(0) == []
    assert enums.symbol_filling_modes(None) == []


# --- guards -------------------------------------------------------------------------------


def trading_cfg(**env: str):
    return cfg(MT5_ENABLE_TRADING="1", **env)


def market(**kw: Any) -> MarketOrder:
    base: dict[str, Any] = {"symbol": "EURUSD", "side": Side.BUY, "volume": 0.1, "price": 1.1}
    return MarketOrder(**{**base, **kw})


def pending(**kw: Any) -> PendingOrder:
    base: dict[str, Any] = {
        "symbol": "EURUSD",
        "order_type": PendingType.BUY_LIMIT,
        "volume": 0.1,
        "price": 1.05,
    }
    return PendingOrder(**{**base, **kw})


def test_guard_trading_disabled_unless_dry_run() -> None:
    off = cfg()
    assert isinstance(check_intent(market(), off), Rejected)
    assert isinstance(check_intent(market(), off, require_trading=False), Allowed)


def test_guard_deviation_cap() -> None:
    c = trading_cfg(MT5_MAX_DEVIATION="30")
    assert isinstance(check_intent(market(deviation=30), c), Allowed)
    result = check_intent(market(deviation=31), c)
    assert isinstance(result, Rejected) and "MT5_MAX_DEVIATION" in result.reason
    close = ClosePosition("EURUSD", 1, Side.BUY, 1, 1, 1.0, deviation=500)
    assert isinstance(check_intent(close, c), Rejected)
    assert isinstance(check_intent(market(deviation=-1), c), Rejected)


def test_guard_symbol_allowlist() -> None:
    c = trading_cfg(MT5_ALLOWED_SYMBOLS="EURUSD")
    assert isinstance(check_intent(market(), c), Allowed)
    result = check_intent(market(symbol="XAUUSD"), c)
    assert isinstance(result, Rejected) and "MT5_ALLOWED_SYMBOLS" in result.reason
    assert isinstance(check_intent(market(symbol=" "), trading_cfg()), Rejected)
    for intent in (
        ModifyPosition("XAUUSD", 1, 0, 0),
        CancelOrder("XAUUSD", 1),
        ClosePosition("XAUUSD", 1, Side.BUY, 1, 1, 1.0),
        pending(symbol="XAUUSD"),
    ):
        assert isinstance(check_intent(intent, c), Rejected)


@pytest.mark.parametrize("volume", [0, -1, float("nan"), float("inf"), 1.01])
def test_guard_open_volume_bounds(volume: float) -> None:
    c = trading_cfg()
    assert isinstance(check_intent(market(volume=volume), c), Rejected)
    assert isinstance(check_intent(pending(volume=volume), c), Rejected)


def test_guard_volume_at_max_is_allowed() -> None:
    assert isinstance(check_intent(market(volume=1.0), trading_cfg()), Allowed)
    assert isinstance(check_intent(market(volume=0.5), trading_cfg(MT5_MAX_VOLUME="0.5")), Allowed)


def test_guard_market_fields() -> None:
    c = trading_cfg()
    assert isinstance(check_intent(market(price=0.0), c), Rejected)
    assert isinstance(check_intent(market(sl=-1.0), c), Rejected)
    assert isinstance(check_intent(market(tp=float("nan")), c), Rejected)
    assert isinstance(check_intent(market(deviation=-1), c), Rejected)


def test_guard_pending_fields() -> None:
    c = trading_cfg()
    assert isinstance(check_intent(pending(), c), Allowed)
    assert isinstance(check_intent(pending(price=0.0), c), Rejected)
    stop_limit = pending(order_type=PendingType.SELL_STOP_LIMIT)
    assert isinstance(check_intent(stop_limit, c), Rejected)
    assert isinstance(
        check_intent(pending(order_type=PendingType.BUY_STOP_LIMIT, stoplimit=1.0), c), Allowed
    )
    assert isinstance(check_intent(pending(time_type=TimeType.SPECIFIED), c), Rejected)
    assert isinstance(
        check_intent(pending(time_type=TimeType.SPECIFIED, expiration=1_000), c), Allowed
    )


def test_guard_modify_cancel_close() -> None:
    c = trading_cfg()
    assert isinstance(check_intent(ModifyPosition("EURUSD", 5, 1.0, 0.0), c), Allowed)
    assert isinstance(check_intent(ModifyPosition("EURUSD", 0, 1.0, 0.0), c), Rejected)
    assert isinstance(check_intent(ModifyPosition("EURUSD", 5, -1.0, 0.0), c), Rejected)
    assert isinstance(check_intent(ModifyPendingOrder("EURUSD", 5, 1.0, 0, 0), c), Allowed)
    assert isinstance(check_intent(ModifyPendingOrder("EURUSD", 5, 0.0, 0, 0), c), Rejected)
    assert isinstance(check_intent(CancelOrder("EURUSD", 9), c), Allowed)
    assert isinstance(check_intent(CancelOrder("EURUSD", -9), c), Rejected)


def test_guard_close_ignores_max_volume_but_not_position_volume() -> None:
    c = trading_cfg(MT5_MAX_VOLUME="0.1")
    big = ClosePosition("EURUSD", 1, Side.BUY, position_volume=5.0, volume=5.0, price=1.1)
    assert isinstance(check_intent(big, c), Allowed)
    over = ClosePosition("EURUSD", 1, Side.BUY, position_volume=1.0, volume=1.5, price=1.1)
    assert isinstance(check_intent(over, c), Rejected)
    zero = ClosePosition("EURUSD", 1, Side.BUY, position_volume=1.0, volume=0.0, price=1.1)
    assert isinstance(check_intent(zero, c), Rejected)
    noprice = ClosePosition("EURUSD", 1, Side.BUY, position_volume=1.0, volume=1.0, price=0.0)
    assert isinstance(check_intent(noprice, c), Rejected)


# --- orders -------------------------------------------------------------------------------


def test_pick_filling_market_execution_never_returns() -> None:
    assert pick_filling(0, market=True, execution_mode=2) is Filling.IOC
    assert pick_filling(2, market=True, execution_mode=2) is Filling.IOC
    assert pick_filling(0, market=True, execution_mode=1) is Filling.RETURN
    assert pick_filling(0, market=False, execution_mode=2) is Filling.RETURN


def test_pick_filling() -> None:
    assert pick_filling(3, market=True) is Filling.FOK
    assert pick_filling(2, market=True) is Filling.IOC
    assert pick_filling(0, market=True) is Filling.RETURN
    assert pick_filling(3, market=False) is Filling.RETURN


def test_pending_type_mapping() -> None:
    assert pending_type_for(Side.BUY, "LIMIT") is PendingType.BUY_LIMIT
    assert pending_type_for(Side.SELL, "STOP_LIMIT") is PendingType.SELL_STOP_LIMIT
    with pytest.raises(ValueError):
        pending_type_for(Side.BUY, "MARKET")
    assert opposite(Side.BUY) is Side.SELL


def test_build_requests() -> None:
    m = build_request(market(sl=1.0, tp=1.2, filling=Filling.FOK))
    assert m["action"] == "DEAL" and m["type"] == "BUY" and m["type_filling"] == "FOK"
    p = build_request(pending(time_type=TimeType.SPECIFIED, expiration=5))
    assert p["action"] == "PENDING" and p["type"] == "BUY_LIMIT"
    assert p["type_time"] == "SPECIFIED" and p["expiration"] == 5
    assert "expiration" not in build_request(pending())
    assert build_request(ModifyPosition("EURUSD", 7, 1.0, 2.0)) == {
        "action": "SLTP",
        "symbol": "EURUSD",
        "position": 7,
        "sl": 1.0,
        "tp": 2.0,
    }
    mod = build_request(ModifyPendingOrder("EURUSD", 8, 1.1, 1.0, 1.2))
    assert mod["action"] == "MODIFY" and mod["order"] == 8
    close = build_request(ClosePosition("EURUSD", 7, Side.BUY, 1.0, 0.5, 1.15))
    assert close["action"] == "DEAL" and close["type"] == "SELL"
    assert close["position"] == 7 and close["volume"] == 0.5
    assert build_request(CancelOrder("EURUSD", 3)) == {"action": "REMOVE", "order": 3}


# --- planning -----------------------------------------------------------------------------

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
OFF = 10800


def rates(**kw: Any):
    base: dict[str, Any] = {
        "count": None,
        "start": None,
        "end": None,
        "max_bars": 500,
        "offset": OFF,
        "now": NOW,
    }
    return planning.plan_rates("EURUSD", "H1", **{**base, **kw})


def test_plan_rates_modes() -> None:
    latest = rates()
    assert latest.method == "copy_rates_from_pos"
    assert latest.params["count"] == 100 and latest.params["start_pos"] == 0
    clamped = rates(count=9999)
    assert clamped.params["count"] == 500 and clamped.clamped
    upto = rates(count=10, end="2026-10-13T08:00:00Z")
    assert upto.method == "copy_rates_from"
    assert upto.params["date_from"] == int(datetime(2026, 10, 13, 11, tzinfo=UTC).timestamp())
    span = rates(start="2026-10-12T00:00:00Z", end="2026-10-13T00:00:00Z")
    assert span.method == "copy_rates_range" and span.limit == 500
    assert span.params["date_to"] - span.params["date_from"] == 86400
    open_ended = rates(start="2026-10-12T00:00:00Z")
    assert open_ended.params["date_to"] == int(NOW.timestamp()) + OFF


def test_plan_rates_clamps_obviously_oversized_ranges() -> None:
    # 500 H1 bars * 1.5 = 750 hours of window; a 100 day request is far beyond that.
    wide = rates(start="2026-06-01T00:00:00Z", end="2026-09-10T00:00:00Z")
    assert wide.clamped and wide.params["date_to"] - wide.params["date_from"] == 750 * 3600
    # Within the window (weekend gaps keep real counts lower) nothing is clamped.
    fits = rates(start="2026-10-01T00:00:00Z", end="2026-10-13T00:00:00Z")
    assert not fits.clamped and fits.params["date_to"] - fits.params["date_from"] == 12 * 86400


def test_plan_rates_validation() -> None:
    with pytest.raises(ValueError):
        rates(count=0)
    with pytest.raises(ValueError):
        rates(start="2026-10-12T00:00:00Z", count=5)
    with pytest.raises(ValueError):
        rates(start="2026-10-13T00:00:00Z", end="2026-10-12T00:00:00Z")
    with pytest.raises(ValueError):
        rates(start="2026-10-12T00:00:00Z", offset=None)
    with pytest.raises(ValueError):
        planning.plan_rates(
            "EURUSD", "H7", count=1, start=None, end=None, max_bars=5, offset=OFF, now=NOW
        )
    assert rates(count=5, offset=None).method == "copy_rates_from_pos"


def test_plan_ticks() -> None:
    base: dict[str, Any] = {"flags": "ALL", "max_bars": 50, "offset": OFF}
    forward = planning.plan_ticks(
        "EURUSD", start="2026-10-13T09:00:00Z", end=None, count=None, **base
    )
    assert forward.method == "copy_ticks_from" and forward.params["count"] == 50 and forward.clamped
    span = planning.plan_ticks(
        "EURUSD", start="2026-10-13T09:00:00Z", end="2026-10-13T09:05:00Z", count=None, **base
    )
    # Range queries are bounded by count (one extra tick) and cut at ``end`` afterwards.
    assert span.method == "copy_ticks_from" and span.params["count"] == 51
    assert span.limit == 50 and span.until == span.params["date_from"] + 300
    with pytest.raises(ValueError):
        planning.plan_ticks(
            "EURUSD", start="2026-10-13T09:00:00Z", end="2026-10-13T09:05:00Z", count=3, **base
        )
    with pytest.raises(ValueError):
        planning.plan_ticks(
            "EURUSD",
            start="2026-10-13T09:00:00Z",
            end=None,
            count=None,
            flags="X",
            max_bars=50,
            offset=OFF,
        )


def test_plan_history_window() -> None:
    start, end = planning.plan_history_window(None, None, offset=OFF, now=NOW)
    assert end - start == 8 * 86400
    assert end == int(NOW.timestamp()) + OFF + 86400
    s2, e2 = planning.plan_history_window("2026-10-01", "2026-10-02", offset=OFF, now=NOW)
    assert e2 - s2 == 86400
    with pytest.raises(ValueError):
        planning.plan_history_window("2026-10-02", "2026-10-01", offset=OFF, now=NOW)
    with pytest.raises(ValueError):
        planning.plan_history_window(None, None, offset=None, now=NOW)


# --- shaping ------------------------------------------------------------------------------


def test_shape_bulk_and_trim() -> None:
    rows = [{"time": 1_000_000_000, "open": 1.0}, {"time": 0}]
    shaped = shaping.shape_bulk(rows, 0)
    assert shaped[0]["time_utc"] == "2001-09-09T01:46:40Z" and "time_utc" not in shaped[1]
    assert "time_utc" not in shaping.shape_bulk(rows, None)[0]
    kept, cut = shaping.trim_to_first([{"a": i} for i in range(5)], 3)
    assert len(kept) == 3 and cut and not shaping.trim_to_first([], 3)[1]


def test_summaries() -> None:
    check = shaping.summarise_check({"retcode": 0, "margin": 5.0, "request": {"big": 1}})
    assert check["retcode_name"] == "OK" and check["margin"] == 5.0 and "request" not in check
    send = shaping.summarise_send({"retcode": 10009, "order": 1, "deal": 2, "price": 1.1})
    assert send["retcode_name"] == "DONE" and send["deal"] == 2
    unknown = shaping.time_info(timeconv.OffsetStatus(None, False))
    assert unknown["broker_offset_hours"] is None and unknown["broker_offset_confident"] is False
    assert "MT5_BROKER_UTC_OFFSET_HOURS" in unknown["time_note"]
    known = shaping.time_info(timeconv.OffsetStatus(7200, True, 3.0, "estimated"))
    assert known["broker_offset_hours"] == 2 and known["broker_offset_age_seconds"] == 3.0
    stale = shaping.time_info(timeconv.OffsetStatus(7200, False, 700.0, "cached"))
    assert stale["broker_offset_confident"] is False and "remembered" in stale["time_note"]
