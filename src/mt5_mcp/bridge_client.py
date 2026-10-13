"""TCP newline-delimited JSON client for the Wine-hosted MT5 bridge.

Encoding and decoding are pure functions; ``BridgeClient`` is the thin socket shell.
"""

from __future__ import annotations

import json
import logging
import select
import socket
import threading
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 30.0
# A resent order_send could trade twice, so it is never retried automatically.
NON_RETRYABLE_METHODS = frozenset({"order_send"})


class BridgeError(Exception):
    """Base class for every bridge failure."""


class BridgeUnavailable(BridgeError):
    """The bridge cannot be reached or spoke an invalid protocol."""


class BridgeConnectionLost(BridgeUnavailable):
    """The connection broke after the request may have been delivered."""


class BridgeAuthError(BridgeError):
    """The bridge rejected the token."""


class BridgeNotConnected(BridgeError):
    """The bridge is up but cannot reach the MT5 terminal."""


class TradingDisabled(BridgeError):
    """The bridge refuses trading requests (MT5_BRIDGE_ALLOW_TRADING is not set)."""


class BridgeBadRequest(BridgeError):
    """The bridge rejected the request as malformed or unknown."""


class BridgeInternalError(BridgeError):
    """The bridge failed unexpectedly."""


class Mt5Error(BridgeError):
    """An MT5 call returned None; carries the terminal's last_error()."""

    def __init__(self, code: int | None, message: str) -> None:
        super().__init__(f"MT5 error {code}: {message}")
        self.code = code
        self.message = message


def encode_request(
    request_id: int | str, token: str, method: str, params: Mapping[str, Any]
) -> bytes:
    line = (
        json.dumps(
            {"id": request_id, "token": token, "method": method, "params": dict(params)},
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(line) > MAX_REQUEST_BYTES:
        raise BridgeBadRequest(f"Request of {len(line)} bytes exceeds the 1 MiB bridge limit")
    return line


def error_from_payload(error: Mapping[str, Any]) -> BridgeError:
    kind = error.get("type")
    message = str(error.get("message") or "")
    code = error.get("code")
    match kind:
        case "auth":
            return BridgeAuthError(message or "Bridge rejected the token")
        case "not_connected":
            return BridgeNotConnected(message or "Bridge cannot reach the MT5 terminal")
        case "trading_disabled":
            return TradingDisabled(message or "Trading is disabled in the bridge")
        case "bad_request" | "unknown_method":
            return BridgeBadRequest(message or str(kind))
        case "mt5_error":
            return Mt5Error(code if isinstance(code, int) else None, message)
        case _:
            return BridgeInternalError(f"{kind}: {message}")


def decode_response(line: bytes, expected_id: int | str) -> Any:
    """Return the ``result`` of a success line or raise the mapped exception."""
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BridgeUnavailable(f"Bridge sent invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BridgeUnavailable("Bridge response is not a JSON object")
    ok = payload.get("ok")
    if ok is True:
        if payload.get("id") != expected_id:
            raise BridgeUnavailable(
                f"Response id {payload.get('id')!r} does not match request id {expected_id!r}"
            )
        return payload.get("result")
    if ok is False:
        error = payload.get("error")
        if not isinstance(error, dict):
            raise BridgeInternalError("Bridge failure without an error object")
        raise error_from_payload(error)
    raise BridgeUnavailable("Bridge response has no boolean 'ok' field")


class BridgeClient:
    """Synchronous, thread-safe client; one request in flight at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._reader: Any = None
        self._next_id = 0

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._reader is not None:
            self._reader.close()
        if self._sock is not None:
            self._sock.close()
        self._reader = None
        self._sock = None

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self._host, self._port), timeout=self._connect_timeout)
        except OSError as exc:
            raise BridgeUnavailable(
                f"Cannot connect to bridge at {self._host}:{self._port}: {exc}"
            ) from exc
        self._sock = sock
        self._reader = sock.makefile("rb")

    def _peer_closed(self) -> bool:
        """True if the idle socket is readable, which means EOF or stray bytes.

        The bridge closes idle connections. Detecting that before sending matters for
        order_send: a request written to a dead socket would be reported as "outcome
        unknown" although it never reached the bridge.
        """
        assert self._sock is not None
        readable, _, _ = select.select([self._sock], [], [], 0)
        return bool(readable)

    def _exchange(self, data: bytes, timeout: float) -> bytes:
        """Send one line and read one line. Raises BridgeUnavailable subclasses."""
        if self._sock is not None and self._peer_closed():
            logger.info("Bridge closed the idle connection; reconnecting")
            self._drop()
        if self._sock is None:
            self._connect()
        assert self._sock is not None and self._reader is not None  # set by _connect
        self._sock.settimeout(timeout)
        try:
            self._sock.sendall(data)
        except OSError as exc:
            # A partial line is never executed (no newline), so nothing was delivered.
            raise BridgeUnavailable(f"Send to bridge failed: {exc}") from exc
        try:
            line = self._reader.readline(MAX_RESPONSE_BYTES)
        except TimeoutError as exc:
            raise BridgeConnectionLost(f"Bridge did not answer within {timeout:.0f}s") from exc
        except OSError as exc:
            raise BridgeConnectionLost(f"Read from bridge failed: {exc}") from exc
        if not line:
            raise BridgeConnectionLost("Bridge closed the connection")
        if not line.endswith(b"\n"):
            # readline stopped at the size cap; the rest of the line is still in the socket.
            raise BridgeConnectionLost("Bridge response exceeded the size limit or was cut short")
        return line

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        read_timeout = self._read_timeout if timeout is None else timeout
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            data = encode_request(request_id, self._token, method, params or {})
            attempts = 1 if method in NON_RETRYABLE_METHODS else 2
            for attempt in range(attempts):
                was_reused = self._sock is not None
                try:
                    line = self._exchange(data, read_timeout)
                except BridgeUnavailable as exc:
                    self._drop()
                    # A reused socket may simply have gone stale; reconnect once.
                    if was_reused and attempt + 1 < attempts:
                        logger.info("Bridge connection was stale (%s); reconnecting", exc)
                        continue
                    raise
                try:
                    return decode_response(line, request_id)
                except BridgeAuthError:
                    # The bridge closes the connection after a bad token.
                    self._drop()
                    raise
                except BridgeUnavailable as exc:
                    # The request was delivered but the answer is unusable, so the outcome is
                    # unknown. Dropping the socket also stops leftover bytes from desyncing
                    # the next call.
                    self._drop()
                    raise BridgeConnectionLost(str(exc)) from exc
        raise BridgeUnavailable("unreachable")  # pragma: no cover
