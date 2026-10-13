import json
import os
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mt5_mcp import http
from mt5_mcp.config import ConfigError
from mt5_mcp.server import create_server, main

from .conftest import FakeBridge, build_test_server, make_config

KEY = "k" * 40
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method: str, params: dict[str, Any] | None = None, request_id: int = 1) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )


INITIALIZE = rpc(
    "initialize",
    {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
)


# Pure helpers


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"authorization": f"Bearer {KEY}"}, KEY),
        ({"authorization": f"bearer   {KEY} "}, KEY),
        ({"x-api-key": KEY}, KEY),
        ({"authorization": f"Basic {KEY}"}, None),
        ({"authorization": "Bearer "}, None),
        ({}, None),
    ],
)
def test_presented_key(headers: dict[str, str], expected: str | None) -> None:
    assert http.presented_key(headers) == expected


def test_key_matches() -> None:
    assert http.key_matches(KEY, KEY)
    assert not http.key_matches(None, KEY)
    assert not http.key_matches(KEY[:-1], KEY)
    assert not http.key_matches("\ud800", KEY)  # lone surrogate must not raise


def test_settings_defaults(tmp_path: Path) -> None:
    settings = http.load_http_settings({}, tmp_path)
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.key_file == tmp_path / "Library/Application Support/mt5-mcp/http.key"
    assert settings.tools == http.DEFAULT_HTTP_TOOLS


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_settings_refuse_non_loopback_hosts(tmp_path: Path, host: str) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        http.load_http_settings({"MT5_HTTP_HOST": host}, tmp_path)


@pytest.mark.parametrize("port", ["0", "70000", "abc", "-1"])
def test_settings_refuse_bad_ports(tmp_path: Path, port: str) -> None:
    with pytest.raises(ConfigError, match="MT5_HTTP_PORT"):
        http.load_http_settings({"MT5_HTTP_PORT": port}, tmp_path)


def test_settings_tools(tmp_path: Path) -> None:
    assert http.load_http_settings({"MT5_HTTP_TOOLS": "all"}, tmp_path).tools is None
    assert http.load_http_settings({"MT5_HTTP_TOOLS": "get_tick, get_rates"}, tmp_path).tools == {
        "get_tick",
        "get_rates",
    }


def write_key(path: Path, key: str = KEY, mode: int = 0o600) -> Path:
    path.write_text(key + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def test_read_api_key(tmp_path: Path) -> None:
    assert http.read_api_key(write_key(tmp_path / "k")) == KEY


def test_read_api_key_refuses_unsafe_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        http.read_api_key(tmp_path / "missing")
    with pytest.raises(ConfigError, match="chmod 600"):
        http.read_api_key(write_key(tmp_path / "open", mode=0o644))
    with pytest.raises(ConfigError, match="at least"):
        http.read_api_key(write_key(tmp_path / "short", key="short"))


# Tool allowlist


def test_restrict_tools_keeps_only_allowed(tmp_path: Path, bridge: FakeBridge) -> None:
    server = build_test_server(make_config(tmp_path, bridge.port))
    assert http.restrict_tools(server, frozenset({"get_tick", "get_rates"})) == [
        "get_rates",
        "get_tick",
    ]


def test_restrict_tools_rejects_unknown_names(tmp_path: Path, bridge: FakeBridge) -> None:
    server = build_test_server(make_config(tmp_path, bridge.port))
    with pytest.raises(ConfigError, match="place_order"):
        http.restrict_tools(server, frozenset({"get_tick", "place_order"}))


# End to end through the real Starlette app


@pytest.fixture
def client(tmp_path: Path, bridge: FakeBridge) -> Any:
    server = build_test_server(make_config(tmp_path, bridge.port))
    http.restrict_tools(server, http.DEFAULT_HTTP_TOOLS)
    with TestClient(
        http.build_http_app(server, KEY), base_url="https://abc.trycloudflare.com"
    ) as tc:
        yield tc


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-key-wrong-key-wrong-key-wrong"}, {"X-API-Key": "nope"}],
)
def test_requests_without_the_key_get_401(client: Any, headers: dict[str, str]) -> None:
    for body in (INITIALIZE, "", "not json"):
        response = client.post("/mcp", content=body, headers={**MCP_HEADERS, **headers})
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
    assert client.get("/mcp", headers=headers).status_code == 401


def test_key_allows_initialize_and_lists_only_allowed_tools(client: Any) -> None:
    auth = {**MCP_HEADERS, "Authorization": f"Bearer {KEY}"}
    init = client.post("/mcp", content=INITIALIZE, headers=auth)
    assert init.status_code == 200, init.text
    listed = client.post("/mcp", content=rpc("tools/list", request_id=2), headers=auth)
    assert listed.status_code == 200, listed.text
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == set(http.DEFAULT_HTTP_TOOLS)


def test_x_api_key_header_also_works(client: Any) -> None:
    response = client.post("/mcp", content=INITIALIZE, headers={**MCP_HEADERS, "X-API-Key": KEY})
    assert response.status_code == 200, response.text


# Entry point


def test_http_mode_refuses_trading(tmp_path: Path, bridge: FakeBridge) -> None:
    env = {"MT5_ENABLE_TRADING": "1", "MT5_TOKEN_FILE": str(tmp_path / "t"), "HOME": str(tmp_path)}
    with pytest.raises(ConfigError, match="--http"):
        create_server(env, http=True)


def test_main_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--serve"])
    assert exc.value.code == 2


def test_main_http_exits_cleanly_on_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bridge: FakeBridge
) -> None:
    for name, value in {
        "MT5_HTTP_KEY_FILE": str(tmp_path / "missing.key"),
        "MT5_TOKEN_FILE": str(tmp_path / "bridge.token"),
        "MT5_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        "MT5_BRIDGE_AUTOSTART": "0",
        "MT5_ENABLE_TRADING": "0",
    }.items():
        monkeypatch.setenv(name, value)
    assert os.environ["MT5_ENABLE_TRADING"] == "0"
    with pytest.raises(SystemExit) as exc:
        main(["--http"])
    assert exc.value.code == 2
