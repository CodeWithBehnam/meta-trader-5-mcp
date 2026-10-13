import json
import time

import pytest

from mt5_mcp import bridge_client as bc
from mt5_mcp.bridge_client import BridgeClient

from .conftest import TOKEN, BridgeFailure, FakeBridge, RawReply


def test_encode_request_is_one_ndjson_line() -> None:
    line = bc.encode_request(7, "tok", "ping", {"a": 1})
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"id": 7, "token": "tok", "method": "ping", "params": {"a": 1}}


def test_encode_request_limits() -> None:
    with pytest.raises(bc.BridgeBadRequest):
        bc.encode_request(1, "t", "x", {"blob": "a" * (bc.MAX_REQUEST_BYTES + 1)})
    with pytest.raises(ValueError):
        bc.encode_request(1, "t", "x", {"v": float("nan")})


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("auth", bc.BridgeAuthError),
        ("not_connected", bc.BridgeNotConnected),
        ("trading_disabled", bc.TradingDisabled),
        ("bad_request", bc.BridgeBadRequest),
        ("unknown_method", bc.BridgeBadRequest),
        ("mt5_error", bc.Mt5Error),
        ("internal", bc.BridgeInternalError),
        ("surprise", bc.BridgeInternalError),
    ],
)
def test_error_mapping(kind: str, expected: type[Exception]) -> None:
    line = json.dumps({"id": 1, "ok": False, "error": {"type": kind, "message": "m", "code": -2}})
    with pytest.raises(expected):
        bc.decode_response(line.encode(), 1)


def test_mt5_error_carries_code() -> None:
    line = json.dumps(
        {"id": 1, "ok": False, "error": {"type": "mt5_error", "message": "boom", "code": -2}}
    )
    with pytest.raises(bc.Mt5Error) as info:
        bc.decode_response(line.encode(), 1)
    assert info.value.code == -2 and info.value.message == "boom"


@pytest.mark.parametrize(
    "line",
    [b"not json", b"[1]", b'{"id":1}', b'{"id":2,"ok":true,"result":1}', b'{"ok":false,"error":3}'],
)
def test_decode_response_rejects_garbage(line: bytes) -> None:
    with pytest.raises(bc.BridgeError):
        bc.decode_response(line, 1)


def test_call_round_trip_reuses_connection(bridge: FakeBridge, client: BridgeClient) -> None:
    bridge.on("echo", lambda p: p)
    assert client.call("echo", {"x": 1}) == {"x": 1}
    assert client.call("echo", {"x": 2}) == {"x": 2}
    assert bridge.methods_called() == ["echo", "echo"]


def test_auth_failure(bridge: FakeBridge, config) -> None:
    bad = BridgeClient(config.bridge_host, config.bridge_port, "wrong")
    with pytest.raises(bc.BridgeAuthError):
        bad.call("ping")
    bad.close()


def test_mt5_error_and_unknown_method(bridge: FakeBridge, client: BridgeClient) -> None:
    def fail(_p: dict) -> None:
        raise BridgeFailure("mt5_error", "Terminal: Invalid params", -2)

    bridge.on("symbol_info", fail)
    with pytest.raises(bc.Mt5Error) as info:
        client.call("symbol_info", {"symbol": "X"})
    assert info.value.code == -2
    with pytest.raises(bc.BridgeBadRequest):
        client.call("does_not_exist")


def test_unavailable_when_nothing_listens(free_port: int) -> None:
    lonely = BridgeClient("127.0.0.1", free_port, TOKEN, connect_timeout=0.5)
    with pytest.raises(bc.BridgeUnavailable):
        lonely.call("ping")


def test_read_timeout_is_connection_lost(bridge: FakeBridge, client: BridgeClient) -> None:
    bridge.on("slow", lambda _p: time.sleep(0.6))
    with pytest.raises(bc.BridgeConnectionLost):
        client.call("slow", timeout=0.2)


def test_stale_connection_is_retried_for_reads(bridge: FakeBridge, client: BridgeClient) -> None:
    bridge.on("echo", lambda p: p)
    bridge.drop_after_requests = 1  # server hangs up on the second request of a connection
    assert client.call("echo", {"n": 1}) == {"n": 1}
    bridge.drop_after_requests = None
    assert client.call("echo", {"n": 2}) == {"n": 2}


def test_order_send_is_never_retried(bridge: FakeBridge, client: BridgeClient) -> None:
    bridge.on("echo", lambda p: p)
    bridge.on("order_send", {"retcode": 10009})
    client.call("echo")
    bridge.drop_after_requests = 1
    with pytest.raises(bc.BridgeConnectionLost):
        client.call("order_send", {"request": {}})
    assert "order_send" not in bridge.methods_called()


def test_order_send_reconnects_when_bridge_closed_idle_socket(
    bridge: FakeBridge, client: BridgeClient
) -> None:
    bridge.on("echo", lambda p: p)
    bridge.on("order_send", {"retcode": 10009})
    bridge.close_after_reply = True  # as the real bridge does after its idle timeout
    client.call("echo")
    time.sleep(0.05)  # let the FIN reach the client
    assert client.call("order_send", {"request": {}}) == {"retcode": 10009}
    assert bridge.methods_called().count("order_send") == 1


@pytest.mark.parametrize(
    "reply",
    [
        b"not json\n",
        b'{"ok":true,"id":999,"result":1}\n',
        b'{"id":1,"result":1}\n',
        b'{"id":1,"ok":true,"result":1}',  # no newline, then EOF
    ],
)
def test_bad_reply_after_delivery_is_connection_lost_and_drops_socket(
    bridge: FakeBridge, client: BridgeClient, reply: bytes
) -> None:
    bridge.on("order_send", RawReply(reply))
    with pytest.raises(bc.BridgeConnectionLost):
        client.call("order_send", {"request": {}}, timeout=0.3)
    assert client._sock is None
    assert bridge.methods_called().count("order_send") == 1


def test_truncated_oversized_line_cannot_desync_later_calls(
    bridge: FakeBridge, client: BridgeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bc, "MAX_RESPONSE_BYTES", 64)
    bridge.on("big", {"blob": "x" * 500})
    bridge.on("echo", lambda p: p)
    with pytest.raises(bc.BridgeConnectionLost):
        client.call("big")
    assert client.call("echo", {"n": 1}) == {"n": 1}
    assert client.call("echo", {"n": 2}) == {"n": 2}
