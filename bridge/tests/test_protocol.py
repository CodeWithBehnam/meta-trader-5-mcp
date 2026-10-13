import json

import pytest

from mt5_bridge.protocol import (
    MAX_LINE_BYTES,
    BridgeError,
    ErrorType,
    encode_response,
    error_response,
    parse_request,
    success_response,
    tokens_match,
)

TOKEN = "s3cret"


def line(**fields: object) -> bytes:
    return json.dumps({"id": 1, "token": TOKEN, "method": "ping", "params": {}} | fields).encode()


def test_valid_request() -> None:
    request = parse_request(line(), TOKEN)
    assert (request.id, request.method, request.params) == (1, "ping", {})


def test_missing_params_defaults_to_empty() -> None:
    raw = json.dumps({"id": "a", "token": TOKEN, "method": "ping"}).encode()
    assert parse_request(raw, TOKEN).params == {}


@pytest.mark.parametrize("token", ["wrong", "", None, 5, ["s3cret"]])
def test_bad_token_is_auth_error_and_keeps_id(token: object) -> None:
    with pytest.raises(BridgeError) as info:
        parse_request(line(token=token, id=9), TOKEN)
    assert info.value.error_type is ErrorType.AUTH
    assert info.value.request_id == 9


def test_missing_token_is_auth_error() -> None:
    raw = json.dumps({"id": 1, "method": "ping"}).encode()
    with pytest.raises(BridgeError) as info:
        parse_request(raw, TOKEN)
    assert info.value.error_type is ErrorType.AUTH


def test_non_ascii_token_does_not_crash() -> None:
    with pytest.raises(BridgeError) as info:
        parse_request(line(token="caf\u00e9"), TOKEN)
    assert info.value.error_type is ErrorType.AUTH


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1,2]", b"\xff\xfe", b'{"id":1,"token":"s3cret","method":"x","params":NaN}'],
)
def test_malformed_input_is_bad_request(raw: bytes) -> None:
    with pytest.raises(BridgeError) as info:
        parse_request(raw, TOKEN)
    assert info.value.error_type is ErrorType.BAD_REQUEST


@pytest.mark.parametrize("fields", [{"method": ""}, {"method": 3}, {"params": [1]}, {"params": 1}])
def test_bad_envelope_is_bad_request(fields: dict[str, object]) -> None:
    with pytest.raises(BridgeError) as info:
        parse_request(line(**fields), TOKEN)
    assert info.value.error_type is ErrorType.BAD_REQUEST
    assert info.value.request_id == 1


def test_bool_id_is_not_echoed() -> None:
    with pytest.raises(BridgeError) as info:
        parse_request(line(id=True, token="x"), TOKEN)
    assert info.value.request_id is None


def test_oversized_line_rejected() -> None:
    with pytest.raises(BridgeError) as info:
        parse_request(b" " * (MAX_LINE_BYTES + 1), TOKEN)
    assert info.value.error_type is ErrorType.BAD_REQUEST


def test_response_shapes() -> None:
    assert json.loads(encode_response(success_response(3, {"a": 1}))) == {
        "id": 3,
        "ok": True,
        "result": {"a": 1},
    }
    failure = error_response(BridgeError(ErrorType.MT5_ERROR, "boom", -2, request_id=4))
    assert json.loads(encode_response(failure)) == {
        "id": 4,
        "ok": False,
        "error": {"type": "mt5_error", "message": "boom", "code": -2},
    }
    assert encode_response(failure).endswith(b"\n")


def test_encode_refuses_nan() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        encode_response(success_response(1, float("nan")))


def test_lone_surrogate_token_is_auth_error_not_a_crash() -> None:
    raw = b'{"id":1,"token":"\\ud800","method":"ping"}'
    with pytest.raises(BridgeError) as info:
        parse_request(raw, TOKEN)
    assert info.value.error_type is ErrorType.AUTH


def test_tokens_match_rejects_unencodable_strings() -> None:
    assert tokens_match("\ud800", TOKEN) is False
    assert tokens_match(TOKEN, TOKEN) is True


def test_deeply_nested_json_is_bad_request() -> None:
    raw = b"[" * 200_000 + b"]" * 200_000
    with pytest.raises(BridgeError) as info:
        parse_request(raw, TOKEN)
    assert info.value.error_type is ErrorType.BAD_REQUEST
