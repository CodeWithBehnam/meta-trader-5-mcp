"""BrokerClock and BridgeSession against the fake bridge."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from mt5_mcp.bridge_client import (
    BridgeAuthError,
    BridgeClient,
    BridgeConnectionLost,
    BridgeUnavailable,
)
from mt5_mcp.config import Config
from mt5_mcp.context import BridgeSession, BrokerClock
from mt5_mcp.launcher import LauncherError, TokenMismatchError

from .conftest import (
    BROKER_NOW,
    NOW,
    OFFSET,
    TOKEN,
    BridgeFailure,
    FakeBridge,
    fixed_now,
    make_config,
)


def make_clock(config: Config) -> BrokerClock:
    return BrokerClock(BridgeClient(config.bridge_host, config.bridge_port, TOKEN), fixed_now)


def test_clock_estimates_and_caches(bridge: FakeBridge, config: Config) -> None:
    clock = make_clock(config)
    assert clock.offset("EURUSD") == OFFSET
    calls = len(bridge.calls)
    assert clock.offset("EURUSD") == OFFSET
    assert len(bridge.calls) == calls


def test_clock_falls_back_to_symbol_list(bridge: FakeBridge, config: Config) -> None:
    def missing(_p: dict) -> None:
        raise BridgeFailure("mt5_error", "Terminal: Not found", -4)

    bridge.on("symbol_info_tick", missing)
    assert make_clock(config).offset("EURUSD") == OFFSET
    assert "symbols_get" in bridge.methods_called()


def test_clock_returns_none_on_stale_ticks(bridge: FakeBridge, config: Config) -> None:
    stale = BROKER_NOW - 2 * 86400
    bridge.on("symbol_info_tick", lambda _p: {"time": stale})
    bridge.on("symbols_get", [{"name": "X", "time": stale}])
    assert make_clock(config).offset() is None


def test_session_requires_matching_token(bridge: FakeBridge, config: Config) -> None:
    bridge.token = "other"
    with pytest.raises(TokenMismatchError):
        BridgeSession(config, TOKEN).call("ping")


def test_session_without_autostart_reports_missing_bridge(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port)
    with pytest.raises(LauncherError, match="AUTOSTART"):
        BridgeSession(config, TOKEN).call("ping")


def test_session_passes_calls_through(bridge: FakeBridge, config: Config) -> None:
    session = BridgeSession(config, TOKEN)
    assert session.call("ping")["initialized"] is True
    assert session.call("account_info")["currency"] == "USD"


def test_auth_error_closes_client_connection(bridge: FakeBridge, config: Config) -> None:
    bad = BridgeClient(config.bridge_host, config.bridge_port, "nope")
    with pytest.raises(BridgeAuthError):
        bad.call("ping")
    bridge.token = "nope"
    assert bad.call("ping")["bridge_version"] == "0.1.0"


class Stub:
    """A bridge client stand-in that fails a set number of times with a given error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[str] = []

    def call(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        self.calls.append(method)
        raise self.error


@pytest.mark.parametrize("error", [BridgeUnavailable("down"), BridgeConnectionLost("eof")])
def test_session_never_restarts_and_resends_order_send(
    bridge: FakeBridge, tmp_path: Path, error: Exception
) -> None:
    config = make_config(tmp_path, bridge.port, MT5_BRIDGE_AUTOSTART="1")
    session = BridgeSession(config, TOKEN)
    stub = Stub(error)
    session._client = stub  # ty: ignore[invalid-assignment]
    session._ready = True
    with pytest.raises(type(error)):
        session.call("order_send", {"request": {}})
    assert stub.calls == ["order_send"]


def test_session_still_restarts_for_reads(bridge: FakeBridge, tmp_path: Path) -> None:
    config = make_config(tmp_path, bridge.port, MT5_BRIDGE_AUTOSTART="1")
    session = BridgeSession(config, TOKEN)
    stub = Stub(BridgeUnavailable("down"))
    session._client = stub  # ty: ignore[invalid-assignment]
    session._ready = True
    with pytest.raises(BridgeUnavailable):
        session.call("account_info")
    assert stub.calls == ["account_info", "account_info"]


class MovingClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> Any:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def clock_with(
    bridge: FakeBridge, config: Config, **kwargs: Any
) -> tuple[BrokerClock, MovingClock]:
    moving = MovingClock()
    client = BridgeClient(config.bridge_host, config.bridge_port, TOKEN)
    return BrokerClock(client, moving, **kwargs), moving


def test_clock_uses_freshest_time_over_all_symbols(bridge: FakeBridge, config: Config) -> None:
    # Forex is frozen (market closed) but a 24/7 symbol keeps ticking.
    stale = BROKER_NOW - 2 * 86400
    bridge.on("symbol_info_tick", lambda _p: {"time": stale})
    bridge.on(
        "symbols_get",
        [{"name": "EURUSD", "time": stale}, {"name": "BTCUSD", "time": BROKER_NOW - 3}],
    )
    clock, _ = clock_with(bridge, config)
    status = clock.status("EURUSD")
    assert status.offset == OFFSET and status.confident


def test_clock_rejects_a_stale_best_estimate(bridge: FakeBridge, config: Config) -> None:
    old = BROKER_NOW - 400  # plausible after rounding, but 400 s old
    bridge.on("symbol_info_tick", lambda _p: {"time": old})
    bridge.on("symbols_get", [{"name": "X", "time": old}])
    clock, _ = clock_with(bridge, config)
    status = clock.status()
    assert status.offset is None and status.confident is False


def test_clock_keeps_cached_value_when_reestimate_fails(bridge: FakeBridge, config: Config) -> None:
    clock, moving = clock_with(bridge, config, ttl=100.0)
    assert clock.status("EURUSD").offset == OFFSET
    stale = BROKER_NOW - 5 * 86400
    bridge.on("symbol_info_tick", lambda _p: {"time": stale})
    bridge.on("symbols_get", [{"name": "X", "time": stale}])
    moving.advance(250)
    status = clock.status("EURUSD")
    assert status.offset == OFFSET and status.confident is False
    assert status.age_seconds == pytest.approx(250)
    assert status.source == "cached"


def test_clock_reestimates_after_expiry_and_backs_off_after_failure(
    bridge: FakeBridge, config: Config
) -> None:
    clock, moving = clock_with(bridge, config, ttl=100.0)
    clock.status("EURUSD")
    moving.advance(200)
    fresh = {"time": BROKER_NOW + 200 - 4}
    bridge.on("symbol_info_tick", lambda _p: fresh)
    bridge.on("symbols_get", [])
    refreshed = clock.status("EURUSD")
    assert refreshed.confident and refreshed.age_seconds == 0.0
    bridge.on("symbol_info_tick", lambda _p: {"time": 1})
    moving.advance(150)
    clock.status("EURUSD")  # fails and starts the back-off
    before = len(bridge.calls)
    moving.advance(5)
    clock.status("EURUSD")
    assert len(bridge.calls) == before


def test_clock_override_bypasses_estimation(bridge: FakeBridge, config: Config) -> None:
    clock, _ = clock_with(bridge, config, override=7200)
    status = clock.status("EURUSD")
    assert status.offset == 7200 and status.confident and status.source == "override"
    assert bridge.calls == []
