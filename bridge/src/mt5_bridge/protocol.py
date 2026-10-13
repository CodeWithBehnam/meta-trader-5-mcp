"""Wire protocol: request parsing and response building. Pure, no MetaTrader5 import."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from enum import StrEnum

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type RequestId = int | str | None

MAX_LINE_BYTES = 1024 * 1024


class ErrorType(StrEnum):
    AUTH = "auth"
    BAD_REQUEST = "bad_request"
    UNKNOWN_METHOD = "unknown_method"
    NOT_CONNECTED = "not_connected"
    TRADING_DISABLED = "trading_disabled"
    MT5_ERROR = "mt5_error"
    INTERNAL = "internal"


class BridgeError(Exception):
    """A failure that maps directly onto a protocol error response."""

    def __init__(
        self,
        error_type: ErrorType,
        message: str,
        code: int | None = None,
        request_id: RequestId = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.code = code
        self.request_id = request_id


@dataclass(frozen=True)
class Request:
    id: RequestId
    method: str
    params: dict[str, JsonValue]


def _reject_constant(name: str) -> object:
    # json.loads accepts NaN/Infinity by default; those are not valid JSON.
    raise ValueError(f"invalid JSON constant {name}")


def tokens_match(supplied: object, expected: str) -> bool:
    """Constant-time token comparison that tolerates non-string input."""
    if not isinstance(supplied, str):
        return False
    try:
        supplied_bytes = supplied.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate from a JSON \ud800 escape cannot be a valid token.
        return False
    return hmac.compare_digest(supplied_bytes, expected.encode("utf-8"))


def decode_line(line: bytes) -> dict[str, JsonValue]:
    """Decode one request line into a JSON object. Nothing is trusted yet."""
    if len(line) > MAX_LINE_BYTES:
        raise BridgeError(ErrorType.BAD_REQUEST, f"request exceeds {MAX_LINE_BYTES} bytes")
    try:
        obj = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise BridgeError(ErrorType.BAD_REQUEST, f"malformed JSON: {exc}") from exc
    except RecursionError as exc:
        # Deeply nested arrays or objects exhaust the parser's stack.
        raise BridgeError(ErrorType.BAD_REQUEST, "malformed JSON: nesting too deep") from exc
    if not isinstance(obj, dict):
        raise BridgeError(ErrorType.BAD_REQUEST, "request must be a JSON object")
    return obj


def request_id_of(obj: dict[str, JsonValue]) -> RequestId:
    raw_id = obj.get("id")
    if isinstance(raw_id, str) or (isinstance(raw_id, int) and not isinstance(raw_id, bool)):
        return raw_id
    return None


def authenticate(obj: dict[str, JsonValue], expected_token: str) -> None:
    if not tokens_match(obj.get("token"), expected_token):
        raise BridgeError(ErrorType.AUTH, "invalid or missing token", request_id=request_id_of(obj))


def read_envelope(obj: dict[str, JsonValue]) -> Request:
    """Check method and params of an already authenticated request object."""
    request_id = request_id_of(obj)
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise BridgeError(
            ErrorType.BAD_REQUEST, "'method' must be a non-empty string", request_id=request_id
        )
    params = obj.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise BridgeError(
            ErrorType.BAD_REQUEST, "'params' must be a JSON object", request_id=request_id
        )
    return Request(id=request_id, method=method, params=params)


def parse_request(line: bytes, expected_token: str) -> Request:
    """Decode one request line, authenticate it and check its envelope.

    Authentication happens before the method and params are inspected so an
    unauthenticated peer learns nothing about the method table.
    """
    obj = decode_line(line)
    authenticate(obj, expected_token)
    return read_envelope(obj)


def success_response(request_id: RequestId, result: JsonValue) -> dict[str, JsonValue]:
    return {"id": request_id, "ok": True, "result": result}


def error_response(error: BridgeError) -> dict[str, JsonValue]:
    return {
        "id": error.request_id,
        "ok": False,
        "error": {
            "type": error.error_type.value,
            "message": error.message,
            "code": error.code,
        },
    }


def encode_response(response: dict[str, JsonValue]) -> bytes:
    """Serialise to one newline-terminated UTF-8 line. NaN is never emitted."""
    return json.dumps(response, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
