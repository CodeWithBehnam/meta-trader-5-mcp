import json
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from mt5_bridge import server as server_module
from mt5_bridge.config import ConfigError, load_config
from mt5_bridge.gateway import Gateway
from mt5_bridge.server import LineReader, _Server, process_line, reuse_address_for
from tests.fake_mt5 import AccountInfo, FakeMt5

TOKEN = "tok"


def request(method: str, token: str = TOKEN, **params: object) -> bytes:
    return (
        json.dumps({"id": 1, "token": token, "method": method, "params": params}).encode() + b"\n"
    )


@pytest.fixture
def served() -> Iterator[tuple[int, FakeMt5]]:
    mt5 = FakeMt5()
    mt5.script["account_info"] = [AccountInfo(1, 2.0, 3.0, "n")]
    config = load_config({"MT5_BRIDGE_TOKEN": TOKEN, "MT5_BRIDGE_PORT": "1"})
    config = type(config)(token=TOKEN, port=0)
    server = _Server(config, Gateway(mt5, config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], mt5
    finally:
        server.shutdown()
        server.server_close()


def converse(port: int, *lines: bytes) -> list[dict]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        reader = sock.makefile("rb")
        out = []
        for line in lines:
            sock.sendall(line)
            out.append(json.loads(reader.readline()))
        return out


def test_many_requests_on_one_connection(served: tuple[int, FakeMt5]) -> None:
    port, _ = served
    first, second, third = converse(
        port, request("ping"), request("account_info"), request("positions_total")
    )
    assert first["ok"] and first["result"]["bridge_version"]
    assert second["result"]["balance"] == 2.0
    assert third == {
        "id": 1,
        "ok": False,
        "error": {"type": "mt5_error", "message": "Success", "code": 1},
    }


def test_bad_json_keeps_connection_open(served: tuple[int, FakeMt5]) -> None:
    port, _ = served
    _, bad, good = converse(port, request("ping"), b"{nope\n", request("ping"))
    assert bad["error"]["type"] == "bad_request"
    assert bad["id"] is None
    assert good["ok"]


def test_auth_failure_closes_connection(served: tuple[int, FakeMt5]) -> None:
    port, mt5 = served
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        reader = sock.makefile("rb")
        sock.sendall(request("account_info", token="wrong"))
        assert json.loads(reader.readline())["error"]["type"] == "auth"
        assert reader.readline() == b""
    assert mt5.init_count == 0


def test_oversized_line_is_rejected_and_connection_dropped(served: tuple[int, FakeMt5]) -> None:
    port, _ = served
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        reader = sock.makefile("rb")
        sock.sendall(b"x" * (1024 * 1024 + 10))
        assert json.loads(reader.readline())["error"]["type"] == "bad_request"


def test_process_line_never_raises_on_internal_failure() -> None:
    mt5 = FakeMt5()
    mt5.script["account_info"] = [object()]
    config = load_config({"MT5_BRIDGE_TOKEN": TOKEN})
    reply = process_line(request("account_info"), config, Gateway(mt5, config))
    assert json.loads(reply.payload)["error"]["type"] == "internal"
    assert not reply.close


def test_config_requires_token_and_validates_port() -> None:
    with pytest.raises(ConfigError):
        load_config({})
    with pytest.raises(ConfigError):
        load_config({"MT5_BRIDGE_TOKEN": "x", "MT5_BRIDGE_PORT": "99999"})
    config = load_config({"MT5_BRIDGE_TOKEN": "x", "MT5_BRIDGE_ALLOW_TRADING": "1"})
    assert config.allow_trading and config.port == 18812
    assert "x'" not in repr(config) and "token" not in repr(config)


def test_bad_json_before_authentication_closes_connection(served: tuple[int, FakeMt5]) -> None:
    port, _ = served
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        reader = sock.makefile("rb")
        sock.sendall(b"{nope\n")
        assert json.loads(reader.readline())["error"]["type"] == "bad_request"
        assert reader.readline() == b""


def test_deeply_nested_json_is_bad_request_then_close(served: tuple[int, FakeMt5]) -> None:
    port, _ = served
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        reader = sock.makefile("rb")
        sock.sendall(b"[" * 300_000 + b"\n")
        assert json.loads(reader.readline())["error"]["type"] == "bad_request"
        assert reader.readline() == b""


def test_unauthenticated_junk_logs_one_warning_without_traceback(
    served: tuple[int, FakeMt5], caplog: pytest.LogCaptureFixture
) -> None:
    port, _ = served
    with caplog.at_level("WARNING"):
        converse(port, b'{"id":1,"token":"\\ud800","method":"ping"}\n')
    assert all(r.exc_info is None for r in caplog.records)
    assert sum("unauthenticated" in r.message for r in caplog.records) == 1


def test_silent_peer_is_dropped_after_auth_timeout(
    served: tuple[int, FakeMt5], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "AUTH_TIMEOUT_SECONDS", 0.3)
    port, _ = served
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        assert sock.recv(10) == b""


def test_trickled_bytes_cannot_extend_the_auth_deadline(
    served: tuple[int, FakeMt5], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "AUTH_TIMEOUT_SECONDS", 0.5)
    port, _ = served
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        closed = False
        while time.monotonic() - started < 4 and not closed:
            try:
                sock.sendall(b"x")
                time.sleep(0.1)
            except OSError:
                closed = True
        assert closed or sock.recv(10) == b""
    assert time.monotonic() - started < 3


def test_idle_authenticated_peer_survives_past_the_auth_timeout(
    served: tuple[int, FakeMt5], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server_module, "AUTH_TIMEOUT_SECONDS", 0.3)
    port, _ = served
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        reader = sock.makefile("rb")
        sock.sendall(request("ping"))
        assert json.loads(reader.readline())["ok"]
        time.sleep(0.6)
        sock.sendall(request("ping"))
        assert json.loads(reader.readline())["ok"]


def test_connections_beyond_the_cap_get_an_error_and_are_closed(
    served: tuple[int, FakeMt5],
) -> None:
    port, _ = served
    held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(16)]
    try:
        for sock in held:
            sock.sendall(request("ping"))
            assert json.loads(sock.makefile("rb").readline())["ok"]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as extra:
            reader = extra.makefile("rb")
            assert json.loads(reader.readline())["ok"] is False
            assert reader.readline() == b""
    finally:
        for sock in held:
            sock.close()
    time.sleep(0.3)
    assert converse(port, request("ping"))[0]["ok"]


@pytest.mark.parametrize(("platform", "expected"), [("win32", False), ("darwin", True)])
def test_reuse_address_is_disabled_only_on_windows(platform: str, expected: bool) -> None:
    assert reuse_address_for(platform) is expected


class ScriptedSocket:
    """Delivers queued chunks one recv at a time, advancing a fake clock per chunk."""

    def __init__(self, chunks: list[bytes], clock: list[float], step: float) -> None:
        self.chunks, self.clock, self.step = chunks, clock, step

    def settimeout(self, _value: float | None, /) -> None:
        return None

    def recv(self, _size: int, /) -> bytes:
        self.clock[0] += self.step
        return self.chunks.pop(0) if self.chunks else b""


def test_line_reader_splits_lines_and_keeps_the_remainder() -> None:
    now = [0.0]
    sock = ScriptedSocket([b"a\nb", b"c\n"], now, 0.0)
    reader = LineReader(sock, clock=lambda: now[0])
    assert reader.read_line(10, 10) == b"a\n"
    assert reader.read_line(10, 10) == b"bc\n"
    assert reader.read_line(10, 10) == b""


def test_line_reader_enforces_an_overall_line_deadline() -> None:
    now = [0.0]
    sock = ScriptedSocket([b"x"] * 100, now, 1.0)
    reader = LineReader(sock, clock=lambda: now[0])
    with pytest.raises(TimeoutError):
        reader.read_line(100, 5)
