"""End-to-end: MCP in-memory client -> tools -> TCP client -> fake bridge."""

import json
import stat
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import CallToolResult, ToolAnnotations

from mt5_mcp.config import Config

from .conftest import (
    BROKER_NOW,
    NOW,
    OFFSET,
    BridgeFailure,
    DropConnection,
    FakeBridge,
    build_test_server,
    eurusd_info,
    make_config,
)

pytestmark = pytest.mark.anyio

READ_TOOLS = {
    "terminal_status",
    "get_account_info",
    "list_symbols",
    "get_symbol_info",
    "get_tick",
    "get_rates",
    "get_ticks",
    "get_positions",
    "get_orders",
    "get_history_orders",
    "get_history_deals",
    "calc_margin",
    "calc_profit",
    "check_order",
}
TRADE_TOOLS = {
    "place_order",
    "modify_position_sltp",
    "modify_pending_order",
    "close_position",
    "cancel_order",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def session(config: Config) -> AsyncGenerator[Client]:
    async with Client(build_test_server(config)) as client:
        yield client


async def call(client: Client, name: str, **args: Any) -> CallToolResult:
    return await client.call_tool(name, args)


def data(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return "".join(getattr(block, "text", "") for block in result.content)


def bar(i: int) -> dict[str, Any]:
    return {
        "time": BROKER_NOW - 3600 * (3 - i),
        "open": 1.1,
        "high": 1.2,
        "low": 1.0,
        "close": 1.15,
        "tick_volume": 10,
        "spread": 2,
        "real_volume": 0,
    }


# --- tool registration --------------------------------------------------------------------


def hints(tool: Any) -> ToolAnnotations:
    assert tool.annotations is not None
    return tool.annotations


async def test_read_only_registration_and_annotations(bridge: FakeBridge, config: Config) -> None:
    async with session(config) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == READ_TOOLS
    for tool in tools:
        assert hints(tool).read_only_hint is True
        assert hints(tool).open_world_hint is False
        assert tool.output_schema is not None or tool.name


async def test_trading_tools_registered_only_when_enabled(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = make_config(tmp_path, bridge.port, MT5_ENABLE_TRADING="1")
    async with session(config) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | TRADE_TOOLS
    assert hints(tools["place_order"]).destructive_hint is True
    assert hints(tools["close_position"]).destructive_hint is True
    assert hints(tools["cancel_order"]).destructive_hint is True
    assert hints(tools["modify_position_sltp"]).idempotent_hint is True
    assert hints(tools["modify_position_sltp"]).destructive_hint is False
    assert all(hints(tools[name]).read_only_hint is False for name in TRADE_TOOLS)


# --- read tools ---------------------------------------------------------------------------


async def test_terminal_status(bridge: FakeBridge, config: Config) -> None:
    async with session(config) as client:
        out = data(await call(client, "terminal_status"))
    assert out["bridge_reachable"] is True
    assert out["terminal"]["connected"] is True and out["terminal"]["trade_allowed"] is False
    assert out["version"]["build"] == 5833
    assert out["broker_offset_hours"] == 3
    assert out["mcp_trading_enabled"] is False


async def test_terminal_status_reports_unreachable_bridge(free_port: int, tmp_path: Path) -> None:
    config = make_config(tmp_path, free_port)
    async with session(config) as client:
        out = data(await call(client, "terminal_status"))
    assert out["bridge_reachable"] is False and "unavailable" in out["error"]


async def test_terminal_status_when_terminal_not_connected(
    bridge: FakeBridge, config: Config
) -> None:
    def down(_p: dict) -> None:
        raise BridgeFailure("not_connected", "initialize failed")

    bridge.on("terminal_info", down)
    async with session(config) as client:
        out = data(await call(client, "terminal_status"))
    assert out["bridge_reachable"] is True and "Open MetaTrader 5" in out["terminal_error"]


async def test_get_tick_converts_times(bridge: FakeBridge, config: Config) -> None:
    async with session(config) as client:
        out = data(await call(client, "get_tick", symbol="EURUSD"))
    assert out["bid"] == 1.16 and out["time"] == BROKER_NOW - 5
    assert out["time_utc"] == "2026-10-13T09:59:55Z"
    assert out["time_broker"] == "2026-10-13T12:59:55"
    assert out["broker_offset_seconds"] == OFFSET


async def test_get_tick_selects_symbol_on_demand(bridge: FakeBridge, config: Config) -> None:

    def tick(_p: dict) -> dict:
        if bridge.params_of("symbol_select"):
            return {"time": BROKER_NOW, "bid": 1.0, "ask": 1.1}
        raise BridgeFailure("mt5_error", "Invalid params", -2)

    bridge.on("symbol_info_tick", tick)
    bridge.on("symbol_select", True)
    async with session(config) as client:
        out = data(await call(client, "get_tick", symbol="GBPUSD"))
    assert out["ask"] == 1.1
    assert {"symbol": "GBPUSD", "enable": True} in bridge.params_of("symbol_select")


async def test_unknown_symbol_is_tool_error(bridge: FakeBridge, config: Config) -> None:
    def fail(_p: dict) -> None:
        raise BridgeFailure("mt5_error", "Invalid params", -2)

    bridge.on(
        "symbol_info_tick", lambda p: fail(p) if p["symbol"] == "NOPE" else {"time": BROKER_NOW}
    )
    bridge.on("symbol_select", False)
    async with session(config) as client:
        text = error_text(await call(client, "get_tick", symbol="NOPE"))
    assert "NOPE" in text


async def test_get_rates_latest_and_cap(bridge: FakeBridge, tmp_path: Path) -> None:
    config = make_config(tmp_path, bridge.port, MT5_MAX_BARS="2")
    bridge.on("copy_rates_from_pos", lambda p: [bar(i) for i in range(p["count"])])
    async with session(config) as client:
        out = data(await call(client, "get_rates", symbol="EURUSD", timeframe="H1", count=50))
    params = bridge.params_of("copy_rates_from_pos")[0]
    assert params == {"symbol": "EURUSD", "timeframe": "H1", "start_pos": 0, "count": 2}
    assert out["count"] == 2 and out["truncated"] is True
    assert out["rates"][0]["time_utc"] == "2026-10-13T07:00:00Z"


async def test_get_rates_range_converts_iso_to_broker_epoch(
    bridge: FakeBridge, config: Config
) -> None:
    bridge.on("copy_rates_range", [bar(0), bar(1), bar(2)])
    async with session(config) as client:
        out = data(
            await call(
                client,
                "get_rates",
                symbol="EURUSD",
                timeframe="H1",
                start="2026-10-13T06:00:00Z",
                end="2026-10-13T09:00:00Z",
            )
        )
    params = bridge.params_of("copy_rates_range")[0]
    assert params["date_from"] == int(NOW.timestamp()) - 4 * 3600 + OFFSET
    assert params["date_to"] == int(NOW.timestamp()) - 3600 + OFFSET
    assert out["count"] == 3 and out["truncated"] is False


async def test_get_rates_validation_errors(bridge: FakeBridge, config: Config) -> None:
    async with session(config) as client:
        bad_tf = await call(client, "get_rates", symbol="EURUSD", timeframe="H7")
        both = await call(
            client, "get_rates", symbol="EURUSD", timeframe="H1", count=5, start="2026-10-01"
        )
        bad_date = await call(client, "get_rates", symbol="EURUSD", timeframe="H1", start="soon")
    assert bad_tf.is_error
    assert "start" in error_text(both)
    assert "ISO-8601" in error_text(bad_date)


async def test_get_ticks(bridge: FakeBridge, config: Config) -> None:
    ticks = [{"time": BROKER_NOW - 10, "time_msc": (BROKER_NOW - 10) * 1000 + 250, "bid": 1.1}]
    bridge.on("copy_ticks_from", ticks)
    async with session(config) as client:
        out = data(
            await call(client, "get_ticks", symbol="EURUSD", start="2026-10-13T09:00:00Z", count=5)
        )
    assert bridge.params_of("copy_ticks_from")[0]["count"] == 5
    assert out["ticks"][0]["time_utc"] == "2026-10-13T09:59:50.250000Z"


async def test_get_positions_and_orders_decoded(bridge: FakeBridge, config: Config) -> None:
    bridge.on(
        "positions_get",
        [{"ticket": 11, "symbol": "EURUSD", "type": 1, "volume": 0.2, "time": BROKER_NOW - 60}],
    )
    bridge.on(
        "orders_get",
        [{"ticket": 12, "symbol": "EURUSD", "type": 2, "state": 1, "time_setup": BROKER_NOW}],
    )
    async with session(config) as client:
        positions = data(await call(client, "get_positions", symbol="EURUSD"))
        orders = data(await call(client, "get_orders"))
        both = await call(client, "get_positions", symbol="EURUSD", ticket=1)
    assert positions["positions"][0]["type_name"] == "SELL"
    assert positions["positions"][0]["time_utc"] == "2026-10-13T09:59:00Z"
    assert orders["orders"][0]["type_name"] == "BUY_LIMIT"
    assert orders["orders"][0]["state_name"] == "PLACED"
    assert bridge.params_of("positions_get")[0] == {"symbol": "EURUSD"}
    assert both.is_error


async def test_history_deals_default_window_and_decoding(
    bridge: FakeBridge, config: Config
) -> None:
    bridge.on("history_deals_get", [{"ticket": 1, "type": 0, "entry": 1, "time": BROKER_NOW}])
    async with session(config) as client:
        out = data(await call(client, "get_history_deals"))
        by_ticket = data(await call(client, "get_history_deals", ticket=5))
        conflict = await call(client, "get_history_deals", ticket=5, position=6)
    params = bridge.params_of("history_deals_get")[0]
    assert params["date_to"] - params["date_from"] == 8 * 86400
    assert out["deals"][0]["entry_name"] == "OUT" and out["deals"][0]["type_name"] == "BUY"
    assert bridge.params_of("history_deals_get")[1] == {"ticket": 5}
    assert by_ticket["count"] == 1 and conflict.is_error


async def test_history_orders(bridge: FakeBridge, config: Config) -> None:
    bridge.on("history_orders_get", [{"ticket": 1, "type": 4, "state": 4}])
    async with session(config) as client:
        out = data(await call(client, "get_history_orders", position=9))
    assert out["orders"][0]["state_name"] == "FILLED"
    assert bridge.params_of("history_orders_get")[0] == {"position": 9}


async def test_symbols_and_info(bridge: FakeBridge, config: Config) -> None:
    async with session(config) as client:
        listing = data(await call(client, "list_symbols", group="*USD*", limit=1))
        info = data(await call(client, "get_symbol_info", symbol="EURUSD"))
    assert bridge.params_of("symbols_get")[0] == {"group": "*USD*"}
    assert listing["symbols"][0]["name"] == "EURUSD" and "filling_mode" not in listing["symbols"][0]
    assert info["filling_modes"] == ["FOK", "IOC"]


async def test_account_and_calcs(bridge: FakeBridge, config: Config) -> None:
    bridge.on("order_calc_margin", 108.5)
    bridge.on("order_calc_profit", 12.0)
    async with session(config) as client:
        account = data(await call(client, "get_account_info"))
        margin = data(await call(client, "calc_margin", symbol="EURUSD", side="BUY", volume=0.1))
        profit = data(
            await call(
                client,
                "calc_profit",
                symbol="EURUSD",
                side="SELL",
                volume=0.1,
                price_open=1.2,
                price_close=1.1,
            )
        )
    assert account["balance"] == 10000.0
    assert margin["margin"] == 108.5 and margin["price"] == 1.16002
    assert bridge.params_of("order_calc_margin")[0]["action"] == "BUY"
    assert profit["profit"] == 12.0


# --- check_order and trading --------------------------------------------------------------

OK_CHECK = {"retcode": 0, "balance": 1e4, "margin": 50.0, "margin_free": 9950.0, "comment": "Done"}


def trading_config(tmp_path: Path, bridge: FakeBridge, **env: str) -> Config:
    return make_config(tmp_path, bridge.port, MT5_ENABLE_TRADING="1", **env)


def audit_records(config: Config) -> list[dict[str, Any]]:
    if not config.audit_log.exists():
        return []
    return [json.loads(line) for line in config.audit_log.read_text().splitlines()]


async def test_check_order_works_with_trading_disabled(bridge: FakeBridge, config: Config) -> None:
    bridge.on("order_check", OK_CHECK)
    async with session(config) as client:
        out = data(await call(client, "check_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert out["ok"] is True and out["margin"] == 50.0 and out["retcode_name"] == "OK"
    request = bridge.params_of("order_check")[0]["request"]
    assert request["action"] == "DEAL" and request["type"] == "BUY"
    assert request["price"] == 1.16002 and request["type_filling"] == "FOK"
    assert "order_send" not in bridge.methods_called()


async def test_check_order_reports_broker_refusal(bridge: FakeBridge, config: Config) -> None:
    bridge.on("order_check", {"retcode": 10027, "comment": "AutoTrading disabled by client"})
    async with session(config) as client:
        out = data(await call(client, "check_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert out["ok"] is False and out["retcode_name"] == "CLIENT_DISABLES_AT"


async def test_place_market_order_success_and_audit(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", OK_CHECK)
    bridge.on(
        "order_send",
        {
            "retcode": 10009,
            "order": 100,
            "deal": 200,
            "volume": 0.1,
            "price": 1.16002,
            "comment": "",
        },
    )
    async with session(config) as client:
        out = data(
            await call(
                client, "place_order", symbol="EURUSD", side="BUY", volume=0.1, sl=1.15, tp=1.18
            )
        )
    assert out["ok"] is True and out["retcode_name"] == "DONE"
    assert out["order"] == 100 and out["deal"] == 200 and out["check"]["retcode"] == 0
    assert bridge.methods_called().index("order_check") < bridge.methods_called().index(
        "order_send"
    )
    assert bridge.params_of("order_send")[0]["request"]["sl"] == 1.15
    intent_record, record = audit_records(config)
    assert intent_record["outcome"] == "intent" and intent_record["send"] is None
    assert record["tool"] == "place_order" and record["outcome"] == "sent"
    assert record["send"]["deal"] == 200 and record["timestamp"] == "2026-10-13T10:00:00Z"


async def test_place_order_refused_when_check_fails(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", {"retcode": 10019, "comment": "No money"})
    async with session(config) as client:
        out = data(await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert out["ok"] is False and out["stage"] == "check" and out["retcode_name"] == "NO_MONEY"
    assert "order_send" not in bridge.methods_called()
    assert audit_records(config)[0]["outcome"] == "check_rejected"


async def test_place_order_broker_rejection_is_structured(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10030, "comment": "Unsupported filling mode"})
    async with session(config) as client:
        out = data(await call(client, "place_order", symbol="EURUSD", side="SELL", volume=0.1))
    assert out["ok"] is False and out["stage"] == "send" and out["retcode_name"] == "INVALID_FILL"
    assert bridge.params_of("order_send")[0]["request"]["price"] == 1.16
    assert [r["outcome"] for r in audit_records(config)] == ["intent", "send_rejected"]


async def test_guards_block_before_any_order_call(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge, MT5_ALLOWED_SYMBOLS="XAUUSD", MT5_MAX_VOLUME="0.5")
    async with session(config) as client:
        wrong_symbol = await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
        too_big = await call(client, "place_order", symbol="XAUUSD", side="BUY", volume=0.6)
    assert "MT5_ALLOWED_SYMBOLS" in error_text(wrong_symbol)
    assert "MT5_MAX_VOLUME" in error_text(too_big)
    assert not {"order_check", "order_send"} & set(bridge.methods_called())
    assert [r["outcome"].split(":")[0] for r in audit_records(config)] == ["guard_rejected"] * 2


async def test_pending_order_requires_price_and_maps_type(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009, "order": 7})
    async with session(config) as client:
        missing = await call(
            client, "place_order", symbol="EURUSD", side="BUY", volume=0.1, kind="LIMIT"
        )
        placed = data(
            await call(
                client,
                "place_order",
                symbol="EURUSD",
                side="SELL",
                volume=0.1,
                kind="STOP",
                price=1.1234567,
                expiration="2026-10-14T00:00:00Z",
            )
        )
    assert "price is required" in error_text(missing)
    request = bridge.params_of("order_send")[0]["request"]
    assert placed["ok"] is True
    assert request["action"] == "PENDING" and request["type"] == "SELL_STOP"
    assert request["price"] == 1.12346 and request["type_filling"] == "RETURN"
    assert request["type_time"] == "SPECIFIED"
    assert request["expiration"] == int(NOW.timestamp()) + 14 * 3600 + OFFSET


async def test_filling_mode_follows_symbol_flags(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("symbol_info", lambda _p: {**eurusd_info(), "filling_mode": 2})
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
        await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1, filling="RETURN")
    sent = [p["request"]["type_filling"] for p in bridge.params_of("order_send")]
    assert sent == ["IOC", "RETURN"]


async def test_connection_loss_during_send_is_reported_as_unknown(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", OK_CHECK)

    def hang_up(_p: dict) -> None:
        raise DropConnection

    bridge.on("order_send", hang_up)
    async with session(config) as client:
        text = error_text(
            await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
        )
    assert "may or may not" in text
    assert bridge.methods_called().count("order_send") == 1
    assert [r["outcome"] for r in audit_records(config)] == ["intent", "send_unknown"]


async def test_modify_position_keeps_unspecified_levels(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on(
        "positions_get",
        [{"ticket": 5, "symbol": "EURUSD", "type": 0, "volume": 1.0, "sl": 1.1, "tp": 1.3}],
    )
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        out = data(await call(client, "modify_position_sltp", ticket=5, sl=1.12))
        data(await call(client, "modify_position_sltp", ticket=5, tp=0))
        missing = await call(client, "modify_position_sltp", ticket=5, sl=-1)
    first, second = (p["request"] for p in bridge.params_of("order_send"))
    assert out["ok"] is True
    assert first == {"action": "SLTP", "symbol": "EURUSD", "position": 5, "sl": 1.12, "tp": 1.3}
    assert second["sl"] == 1.1 and second["tp"] == 0
    assert missing.is_error


async def test_unknown_ticket_is_tool_error(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("positions_get", [])
    bridge.on("orders_get", [])
    async with session(config) as client:
        position = await call(client, "close_position", ticket=99)
        order = await call(client, "cancel_order", ticket=98)
    assert "No open position with ticket 99" in error_text(position)
    assert "No pending order with ticket 98" in error_text(order)


async def test_close_position_builds_opposite_deal(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge, MT5_MAX_VOLUME="0.1")
    held = {"ticket": 5, "symbol": "EURUSD", "type": 0, "volume": 2.0, "magic": 77}
    bridge.on("positions_get", [held])
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009, "deal": 9})
    async with session(config) as client:
        full = data(await call(client, "close_position", ticket=5))
        partial = data(await call(client, "close_position", ticket=5, volume=0.5))
        over = await call(client, "close_position", ticket=5, volume=3.0)
    assert full["ok"] and partial["ok"]
    full_req, partial_req = (p["request"] for p in bridge.params_of("order_send"))
    assert full_req["type"] == "SELL" and full_req["position"] == 5 and full_req["volume"] == 2.0
    assert full_req["price"] == 1.16 and full_req["magic"] == 77
    assert partial_req["volume"] == 0.5
    assert "exceeds the position volume" in error_text(over)


async def test_close_short_buys_at_ask(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("positions_get", [{"ticket": 6, "symbol": "EURUSD", "type": 1, "volume": 1.0}])
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        data(await call(client, "close_position", ticket=6))
    request = bridge.params_of("order_send")[0]["request"]
    assert request["type"] == "BUY" and request["price"] == 1.16002


async def test_cancel_order_skips_check(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("orders_get", [{"ticket": 8, "symbol": "EURUSD", "type": 2}])
    bridge.on("order_send", {"retcode": 10009, "order": 8})
    async with session(config) as client:
        out = data(await call(client, "cancel_order", ticket=8))
    assert out["ok"] is True
    assert bridge.params_of("order_send")[0]["request"] == {"action": "REMOVE", "order": 8}
    assert "order_check" not in bridge.methods_called()


async def test_modify_pending_order_keeps_existing_values(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge)
    pending = {
        "ticket": 8, "symbol": "EURUSD", "type": 2, "price_open": 1.1, "sl": 1.0, "tp": 1.3,
        "price_stoplimit": 0.0, "type_time": 2, "time_expiration": BROKER_NOW + 3600,
    }  # fmt: skip
    bridge.on("orders_get", [pending])
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10008, "order": 8})
    async with session(config) as client:
        out = data(await call(client, "modify_pending_order", ticket=8, price=1.105))
    request = bridge.params_of("order_send")[0]["request"]
    assert out["ok"] is True and out["retcode_name"] == "PLACED"
    assert request["action"] == "MODIFY" and request["order"] == 8
    assert request["price"] == 1.105 and request["sl"] == 1.0 and request["tp"] == 1.3
    assert request["type_time"] == "SPECIFIED" and request["expiration"] == BROKER_NOW + 3600


async def test_trading_disabled_in_bridge_surfaces_as_error(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", OK_CHECK)

    def refuse(_p: dict) -> None:
        raise BridgeFailure("trading_disabled", "order_send disabled")

    bridge.on("order_send", refuse)
    async with session(config) as client:
        text = error_text(
            await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
        )
    assert "refuses trading" in text
    assert [r["outcome"] for r in audit_records(config)] == ["intent", "send_error"]


# --- review fixes --------------------------------------------------------------------------


async def test_unwritable_audit_log_blocks_the_send(bridge: FakeBridge, tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    config = trading_config(tmp_path, bridge, MT5_AUDIT_LOG=str(blocker / "audit.jsonl"))
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        text = error_text(
            await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
        )
    assert "Order not sent" in text and "MT5_AUDIT_LOG" in text
    assert "order_send" not in bridge.methods_called()


async def test_audit_file_and_dir_are_private(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge, MT5_AUDIT_LOG=str(tmp_path / "logs" / "a.jsonl"))
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        data(await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert stat.S_IMODE(config.audit_log.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.audit_log.parent.stat().st_mode) == 0o700


async def test_check_error_is_audited(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)

    def broken(_p: dict) -> None:
        raise BridgeFailure("mt5_error", "Terminal: Invalid params", -2)

    bridge.on("order_check", broken)
    async with session(config) as client:
        result = await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
    assert result.is_error
    assert "order_send" not in bridge.methods_called()
    (record,) = audit_records(config)
    assert record["outcome"] == "check_error" and "Invalid params" in record["check"]["error"]


async def test_disallowed_symbol_is_refused_before_any_bridge_lookup(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge, MT5_ALLOWED_SYMBOLS="XAUUSD")
    bridge.on("positions_get", [{"ticket": 5, "symbol": "EURUSD", "type": 0, "volume": 1.0}])
    async with session(config) as client:
        placed = await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1)
        closed = await call(client, "close_position", ticket=5)
        checked = await call(client, "check_order", symbol="EURUSD", side="BUY", volume=0.1)
    assert all("MT5_ALLOWED_SYMBOLS" in error_text(r) for r in (placed, closed, checked))
    touched = set(bridge.methods_called())
    assert not touched & {"symbol_select", "symbol_info", "symbol_info_tick", "order_check"}
    outcomes = [r["outcome"] for r in audit_records(config)]
    assert (
        outcomes == ["guard_rejected: symbol 'EURUSD' is not in MT5_ALLOWED_SYMBOLS (XAUUSD)"] * 2
    )


async def test_deviation_is_capped_and_defaults_stay_under_the_cap(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = trading_config(tmp_path, bridge, MT5_MAX_DEVIATION="10")
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        too_wide = await call(
            client, "place_order", symbol="EURUSD", side="BUY", volume=0.1, deviation=11
        )
        data(await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert "MT5_MAX_DEVIATION" in error_text(too_wide)
    assert [p["request"]["deviation"] for p in bridge.params_of("order_send")] == [10]


async def test_default_deviation_is_20(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        data(await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert bridge.params_of("order_send")[0]["request"]["deviation"] == 20


async def test_market_execution_never_uses_return(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("symbol_info", lambda _p: {**eurusd_info(), "trade_exemode": 2, "filling_mode": 0})
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        data(await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1))
        explicit = await call(
            client, "place_order", symbol="EURUSD", side="BUY", volume=0.1, filling="RETURN"
        )
        data(
            await call(
                client,
                "place_order",
                symbol="EURUSD",
                side="BUY",
                volume=0.1,
                kind="LIMIT",
                price=1.1,
            )
        )
    assert "market execution" in error_text(explicit)
    fills = [p["request"]["type_filling"] for p in bridge.params_of("order_send")]
    assert fills == ["IOC", "RETURN"]  # pending orders keep RETURN


async def test_live_eurusd_market_execution_picks_ioc(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    bridge.on("symbol_info", lambda _p: {**eurusd_info(), "trade_exemode": 2, "filling_mode": 2})
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    async with session(config) as client:
        data(await call(client, "place_order", symbol="EURUSD", side="BUY", volume=0.1))
    assert bridge.params_of("order_send")[0]["request"]["type_filling"] == "IOC"


async def test_past_expiration_is_refused_and_audited(bridge: FakeBridge, tmp_path: Path) -> None:
    config = trading_config(tmp_path, bridge)
    async with session(config) as client:
        text = error_text(
            await call(
                client,
                "place_order",
                symbol="EURUSD",
                side="BUY",
                volume=0.1,
                kind="LIMIT",
                price=1.1,
                expiration="2026-10-13T09:00:00Z",
            )
        )
    assert "in the past" in text
    assert "order_check" not in bridge.methods_called()
    assert audit_records(config)[0]["outcome"].startswith("guard_rejected")


async def test_unknown_offset_refuses_expiration_and_override_fixes_it(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    stale = BROKER_NOW - 5 * 86400
    bridge.on("symbol_info_tick", lambda _p: {**eurusd_tick_like(), "time": stale})
    bridge.on("symbols_get", [{"name": "EURUSD", "time": stale}])
    bridge.on("order_check", OK_CHECK)
    bridge.on("order_send", {"retcode": 10009})
    args: dict[str, Any] = {
        "symbol": "EURUSD",
        "side": "BUY",
        "volume": 0.1,
        "kind": "LIMIT",
        "price": 1.1,
        "expiration": "2026-10-14T00:00:00Z",
    }
    plain = trading_config(tmp_path, bridge)
    async with session(plain) as client:
        refused = error_text(await call(client, "place_order", **args))
        history = error_text(await call(client, "get_history_deals"))
    assert "MT5_BROKER_UTC_OFFSET_HOURS" in refused and "MT5_BROKER_UTC_OFFSET_HOURS" in history
    overridden = trading_config(tmp_path, bridge, MT5_BROKER_UTC_OFFSET_HOURS="3")
    async with session(overridden) as client:
        data(await call(client, "place_order", **args))
        status = data(await call(client, "terminal_status"))
    sent = bridge.params_of("order_send")[0]["request"]
    assert sent["expiration"] == int(NOW.timestamp()) + 14 * 3600 + 3 * 3600
    assert status["broker_offset_hours"] == 3 and status["broker_offset_confident"] is True


def eurusd_tick_like() -> dict[str, Any]:
    return {"bid": 1.16, "ask": 1.16002, "last": 0.0, "volume": 0}


async def test_time_info_reports_confidence(bridge: FakeBridge, config: Config) -> None:
    async with session(config) as client:
        out = data(await call(client, "get_tick", symbol="EURUSD"))
    assert out["broker_offset_confident"] is True and out["broker_offset_age_seconds"] == 0.0


async def test_terminal_status_tolerates_missing_terminal_info(
    bridge: FakeBridge, config: Config
) -> None:
    bridge.on("terminal_info", None)
    async with session(config) as client:
        out = data(await call(client, "terminal_status"))
    assert out["bridge_reachable"] is True and "terminal_error" in out


async def test_oversized_rate_range_is_clamped_before_the_bridge(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = make_config(tmp_path, bridge.port, MT5_MAX_BARS="10")
    bridge.on("copy_rates_range", lambda p: [bar(0)])
    async with session(config) as client:
        out = data(
            await call(
                client,
                "get_rates",
                symbol="EURUSD",
                timeframe="H1",
                start="2026-01-01T00:00:00Z",
                end="2026-10-01T00:00:00Z",
            )
        )
    params = bridge.params_of("copy_rates_range")[0]
    assert params["date_to"] - params["date_from"] == 15 * 3600
    assert out["truncated"] is True


async def test_tick_range_is_bounded_by_count_and_cut_at_end(
    bridge: FakeBridge, tmp_path: Path
) -> None:
    config = make_config(tmp_path, bridge.port, MT5_MAX_BARS="3")
    start = BROKER_NOW - 3600
    ticks = [{"time": start + i, "time_msc": (start + i) * 1000, "bid": 1.1} for i in range(10)]
    bridge.on("copy_ticks_from", lambda p: ticks[: p["count"]])
    async with session(config) as client:
        wide = data(
            await call(
                client,
                "get_ticks",
                symbol="EURUSD",
                start="2026-10-13T09:00:00Z",
                end="2026-10-13T09:10:00Z",
            )
        )
        narrow = data(
            await call(
                client,
                "get_ticks",
                symbol="EURUSD",
                start="2026-10-13T09:00:00Z",
                end="2026-10-13T09:00:01Z",
            )
        )
    assert bridge.params_of("copy_ticks_from")[0]["count"] == 4
    assert wide["count"] == 3 and wide["truncated"] is True
    assert narrow["count"] == 2 and narrow["truncated"] is False
