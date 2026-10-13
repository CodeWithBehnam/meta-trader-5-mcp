"""Shared fixtures: an in-process fake bridge speaking the NDJSON contract over TCP."""

import json
import socket
import socketserver
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mt5_mcp.bridge_client import BridgeClient
from mt5_mcp.config import Config, load_config
from mt5_mcp.server import build_server

TOKEN = "test-token"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
OFFSET = 3 * 3600
BROKER_NOW = int(NOW.timestamp()) + OFFSET


class DropConnection(Exception):
    """Raised by a canned handler to hang up without answering."""


class BridgeFailure(Exception):
    """Raised by a canned handler to produce a contract error response."""

    def __init__(self, kind: str, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.code = code


class RawReply:
    """Returned by a canned handler to send exact bytes instead of a contract response."""

    def __init__(self, data: bytes) -> None:
        self.data = data


Handler = Callable[[dict[str, Any]], Any]


class FakeBridge:
    def __init__(self, port: int = 0) -> None:
        self.handlers: dict[str, Handler] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.token = TOKEN
        self.drop_after_requests: int | None = None
        self.close_after_reply = False
        self._served = 0
        owner = self

        class RequestHandler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                for raw in self.rfile:
                    limit = owner.drop_after_requests
                    if limit is not None and owner._served >= limit:
                        return
                    owner._served += 1
                    try:
                        reply = owner.respond(raw)
                    except DropConnection:
                        return
                    self.wfile.write(reply)
                    self.wfile.flush()
                    if owner.close_after_reply:
                        # One-shot, reset here so the test cannot race the handler.
                        owner.close_after_reply = False
                        return

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", port), RequestHandler)
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def on(self, method: str, handler: Handler | Any) -> None:
        self.handlers[method] = handler if callable(handler) else (lambda _p, v=handler: v)

    def methods_called(self) -> list[str]:
        return [m for m, _ in self.calls]

    def params_of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def respond(self, raw: bytes) -> bytes:
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            return self._fail(None, "bad_request", "malformed JSON")
        request_id = request.get("id")
        if request.get("token") != self.token:
            return self._fail(request_id, "auth", "bad token")
        method = request.get("method")
        params = request.get("params", {})
        self.calls.append((method, params))
        handler = self.handlers.get(method)
        if handler is None:
            return self._fail(request_id, "unknown_method", f"unknown method {method}")
        try:
            result = handler(params)
        except BridgeFailure as failure:
            return self._fail(request_id, failure.kind, failure.message, failure.code)
        if isinstance(result, RawReply):
            return result.data
        return (json.dumps({"id": request_id, "ok": True, "result": result}) + "\n").encode()

    @staticmethod
    def _fail(request_id: Any, kind: str, message: str, code: int | None = None) -> bytes:
        body = {
            "id": request_id,
            "ok": False,
            "error": {"type": kind, "message": message, "code": code},
        }
        return (json.dumps(body) + "\n").encode()


def eurusd_info() -> dict[str, Any]:
    return {
        "name": "EURUSD",
        "description": "Euro vs US Dollar",
        "path": "Forex\\EURUSD",
        "currency_base": "EUR",
        "currency_profit": "USD",
        "digits": 5,
        "visible": True,
        "trade_mode": 4,
        "filling_mode": 3,
        "time": BROKER_NOW - 5,
        "volume_min": 0.01,
    }


def eurusd_tick() -> dict[str, Any]:
    return {
        "time": BROKER_NOW - 5,
        "bid": 1.16,
        "ask": 1.16002,
        "last": 0.0,
        "volume": 0,
        "time_msc": (BROKER_NOW - 5) * 1000,
        "flags": 6,
        "volume_real": 0.0,
    }


def install_defaults(bridge: FakeBridge) -> None:
    bridge.on(
        "ping",
        {
            "bridge_version": "0.1.0",
            "python": "3.12.15",
            "mt5_package": "5.0.6231",
            "initialized": True,
            "trading_allowed_by_bridge": True,
        },
    )
    bridge.on("version", {"build": 5833, "release": "2026-09-01", "date": "01 Sep 2026"})
    bridge.on(
        "terminal_info",
        {
            "connected": True,
            "trade_allowed": False,
            "tradeapi_disabled": False,
            "build": 5833,
            "name": "MetaTrader 5",
            "company": "Demo Broker",
            "path": "C:\\Program Files\\MetaTrader 5",
            "ping_last": 12345,
        },
    )
    bridge.on("account_info", {"login": 1, "balance": 10000.0, "currency": "USD", "trade_mode": 0})
    bridge.on("symbols_get", [eurusd_info()])
    bridge.on("symbol_info", lambda _p: eurusd_info())
    bridge.on("symbol_info_tick", lambda p: eurusd_tick())
    bridge.on("symbol_select", True)


@pytest.fixture
def bridge() -> Iterator[FakeBridge]:
    fake = FakeBridge()
    install_defaults(fake)
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def config(tmp_path: Path, bridge: FakeBridge) -> Config:
    return make_config(tmp_path, bridge.port)


def make_config(tmp_path: Path, port: int, **overrides: str) -> Config:
    env = {
        "MT5_BRIDGE_PORT": str(port),
        "MT5_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "MT5_BRIDGE_LOG": str(tmp_path / "bridge.log"),
        "MT5_TOKEN_FILE": str(tmp_path / "bridge.token"),
        "MT5_BRIDGE_AUTOSTART": "0",
        **overrides,
    }
    return load_config(env, home=tmp_path, package_file=Path("/repo/src/mt5_mcp/config.py"))


@pytest.fixture
def client(config: Config) -> Iterator[BridgeClient]:
    instance = BridgeClient(config.bridge_host, config.bridge_port, TOKEN)
    yield instance
    instance.close()


@pytest.fixture
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fixed_now() -> datetime:
    return NOW


def build_test_server(config: Config) -> Any:
    client = BridgeClient(config.bridge_host, config.bridge_port, TOKEN)
    return build_server(config, client, now=fixed_now)
