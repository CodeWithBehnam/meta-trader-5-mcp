"""TCP shell: newline-delimited JSON over loopback, one thread per connection."""

from __future__ import annotations

import importlib
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import FrameType, ModuleType
from typing import Protocol

from mt5_bridge.config import HOST, Config, ConfigError, load_config
from mt5_bridge.gateway import Gateway
from mt5_bridge.protocol import (
    MAX_LINE_BYTES,
    BridgeError,
    ErrorType,
    authenticate,
    decode_line,
    encode_response,
    error_response,
    read_envelope,
    success_response,
)

log = logging.getLogger("mt5_bridge")

IDLE_TIMEOUT_SECONDS = 600.0
# A peer must present one valid authenticated request within this long of connecting.
AUTH_TIMEOUT_SECONDS = 10.0
# Once a line has started, it must complete within this long, however slowly bytes trickle in.
LINE_TIMEOUT_SECONDS = 30.0
SEND_TIMEOUT_SECONDS = 30.0
MAX_CONNECTIONS = 16


@dataclass(frozen=True)
class Reply:
    payload: bytes
    # Set after a failed auth so a peer cannot keep guessing on one connection.
    close: bool = False
    # True once the request carried a valid token, whatever happened to it afterwards.
    authenticated: bool = False


def process_line(line: bytes, config: Config, gateway: Gateway) -> Reply:
    """Turn one request line into one response line. Never raises."""
    started = time.monotonic()
    request_id = None
    method = "?"
    close = False
    authenticated = False
    try:
        obj = decode_line(line)
        authenticate(obj, config.token)
        authenticated = True
        request = read_envelope(obj)
        request_id, method = request.id, request.method
        result = gateway.handle(request.method, request.params)
        response = success_response(request.id, result)
    except BridgeError as exc:
        if exc.request_id is None:
            exc.request_id = request_id
        close = exc.error_type is ErrorType.AUTH
        response = error_response(exc)
    except Exception as exc:
        if authenticated:
            log.exception("unhandled error in method %s", method)
        else:
            # Junk from an unauthenticated peer must not cost a traceback per request.
            log.warning("unhandled %s before authentication", type(exc).__name__)
        response = error_response(
            BridgeError(ErrorType.INTERNAL, f"{type(exc).__name__}: {exc}", request_id=request_id)
        )
    try:
        payload = encode_response(response)
    except (TypeError, ValueError) as exc:
        log.exception("could not serialise response for %s", method)
        payload = encode_response(
            error_response(
                BridgeError(
                    ErrorType.INTERNAL, f"unserialisable result: {exc}", request_id=request_id
                )
            )
        )
    log.debug("%s -> %s in %.1f ms", method, response["ok"], (time.monotonic() - started) * 1000)
    return Reply(payload, close, authenticated)


class Receiver(Protocol):
    """The slice of a socket that LineReader needs; lets tests script the traffic."""

    def settimeout(self, value: float | None, /) -> None: ...

    def recv(self, size: int, /) -> bytes: ...


class LineReader:
    """Buffered line reader whose deadlines are absolute, so trickled bytes cannot extend them."""

    def __init__(self, sock: Receiver, clock: Callable[[], float] = time.monotonic) -> None:
        self._sock = sock
        self._clock = clock
        self._buffer = bytearray()

    def _take(self) -> bytes | None:
        """Pop one line, or the first MAX_LINE_BYTES + 1 bytes when it is over-long."""
        newline = self._buffer.find(b"\n", 0, MAX_LINE_BYTES + 1)
        size = newline + 1 if newline >= 0 else MAX_LINE_BYTES + 1
        if newline < 0 and len(self._buffer) < size:
            return None
        line = bytes(self._buffer[:size])
        del self._buffer[:size]
        return line

    def _fill(self, deadline: float) -> bool:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("deadline exceeded")
        self._sock.settimeout(remaining)
        chunk = self._sock.recv(65536)
        self._buffer += chunk
        return bool(chunk)

    def read_line(
        self, idle_deadline: float, line_timeout: float, hard_deadline: float | None = None
    ) -> bytes:
        """Return one line (b"" at EOF). Raises TimeoutError when a deadline passes.

        ``idle_deadline`` bounds the wait for the first byte; ``line_timeout`` then bounds
        the rest of the line, never beyond ``hard_deadline`` when one is given.
        """
        line = self._take()
        if line is not None:
            return line
        if not self._buffer:
            if not self._fill(idle_deadline):
                return b""
            line = self._take()
            if line is not None:
                return line
        deadline = self._clock() + line_timeout
        if hard_deadline is not None:
            deadline = min(deadline, hard_deadline)
        while True:
            if not self._fill(deadline):
                rest = bytes(self._buffer)
                self._buffer.clear()
                return rest
            line = self._take()
            if line is not None:
                return line


class _Handler(socketserver.BaseRequestHandler):
    server: _Server

    def handle(self) -> None:
        peer = self.client_address[0]
        log.info("connection from %s", peer)
        connected = time.monotonic()
        auth_deadline = connected + AUTH_TIMEOUT_SECONDS
        reader = LineReader(self.request)
        authenticated = False
        while True:
            try:
                if authenticated:
                    line = reader.read_line(
                        time.monotonic() + IDLE_TIMEOUT_SECONDS, LINE_TIMEOUT_SECONDS
                    )
                else:
                    line = reader.read_line(auth_deadline, LINE_TIMEOUT_SECONDS, auth_deadline)
            except (TimeoutError, ConnectionError):
                if not authenticated:
                    log.warning("connection from %s timed out before authenticating", peer)
                break
            if not line:
                break
            if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                # Cannot resynchronise mid-line, so answer and drop the connection.
                self._send(
                    encode_response(
                        error_response(
                            BridgeError(
                                ErrorType.BAD_REQUEST, f"request exceeds {MAX_LINE_BYTES} bytes"
                            )
                        )
                    )
                )
                break
            if not line.strip():
                continue
            reply = process_line(line, self.server.config, self.server.gateway)
            authenticated = authenticated or reply.authenticated
            sent = self._send(reply.payload)
            if not authenticated:
                log.warning("unauthenticated request from %s rejected; closing", peer)
                break
            if not sent or reply.close:
                break
        log.info("connection from %s closed", peer)

    def _send(self, payload: bytes) -> bool:
        try:
            self.request.settimeout(SEND_TIMEOUT_SECONDS)
            self.request.sendall(payload)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            return False
        return True


def reuse_address_for(platform: str) -> bool:
    """SO_REUSEADDR on Windows lets a second socket bind a port that is in use."""
    return platform != "win32"


type _Request = socket.socket | tuple[bytes, socket.socket]


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = reuse_address_for(sys.platform)
    daemon_threads = True

    def __init__(self, config: Config, gateway: Gateway) -> None:
        self.config = config
        self.gateway = gateway
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__((HOST, config.port), _Handler)

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()

    def process_request(self, request: _Request, client_address: tuple[str, int]) -> None:
        if not self._slots.acquire(blocking=False):
            log.warning(
                "refusing connection from %s: %d handlers busy", client_address[0], MAX_CONNECTIONS
            )
            self._refuse(request if isinstance(request, socket.socket) else request[1])
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: _Request, client_address: tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _refuse(self, request: socket.socket) -> None:
        busy = BridgeError(ErrorType.INTERNAL, "bridge is busy: too many connections")
        try:
            request.settimeout(1.0)
            request.sendall(encode_response(error_response(busy)))
        except OSError as exc:
            log.debug("could not send busy reply: %s", exc)
        finally:
            self.shutdown_request(request)


def load_mt5() -> ModuleType:
    """Import MetaTrader5 lazily so the pure modules stay importable on the Mac."""
    return importlib.import_module("MetaTrader5")


def serve(config: Config, mt5: ModuleType) -> None:
    gateway = Gateway(mt5, config)
    with _Server(config, gateway) as server:

        def stop(_signum: int, _frame: FrameType | None) -> None:
            log.info("signal received, shutting down")
            # shutdown() blocks until serve_forever exits, so it cannot run on this thread.
            threading.Thread(target=server.shutdown, daemon=True).start()

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop)
        log.info(
            "listening on %s:%d (trading %s)",
            HOST,
            config.port,
            "ALLOWED" if config.allow_trading else "disabled",
        )
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            gateway.shutdown()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("MT5_BRIDGE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(os.environ)
        mt5 = load_mt5()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except ImportError as exc:
        log.error("cannot import MetaTrader5 (is this running under Windows/Wine?): %s", exc)
        return 3
    serve(config, mt5)
    return 0
