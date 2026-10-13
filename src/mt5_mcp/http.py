"""Streamable HTTP mode for remote MCP clients (reached through a tunnel).

Guarantees, each enforced here rather than left to deployment:
- binds to a loopback address only, so a tunnel is the single way in
- every HTTP request must carry the API key before it reaches the MCP app
- trading tools are never served (the caller refuses to start with trading enabled)
- only allowlisted read-only tools stay registered
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import ConfigError

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
DEFAULT_HTTP_TOOLS = frozenset(
    {"terminal_status", "list_symbols", "get_symbol_info", "get_tick", "get_rates", "get_ticks"}
)
DEFAULT_KEY_FILE = "~/Library/Application Support/mt5-mcp/http.key"
MIN_KEY_LENGTH = 32


@dataclass(frozen=True)
class HttpSettings:
    host: str
    port: int
    key_file: Path
    # None means every registered tool; otherwise the exact allowlist.
    tools: frozenset[str] | None


def load_http_settings(env: Mapping[str, str], home: Path) -> HttpSettings:
    host = env.get("MT5_HTTP_HOST", "127.0.0.1").strip()
    if host not in LOOPBACK_HOSTS:
        raise ConfigError(
            f"MT5_HTTP_HOST must be a loopback address ({', '.join(sorted(LOOPBACK_HOSTS))}), "
            f"got {host!r}; expose the server through a tunnel instead"
        )
    raw_port = env.get("MT5_HTTP_PORT", "8000").strip()
    if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise ConfigError(f"MT5_HTTP_PORT must be a port number, got {raw_port!r}")
    raw_key_file = env.get("MT5_HTTP_KEY_FILE", "").strip() or DEFAULT_KEY_FILE
    key_file = Path(
        raw_key_file.replace("~", str(home), 1) if raw_key_file.startswith("~") else raw_key_file
    )
    raw_tools = env.get("MT5_HTTP_TOOLS", "").strip()
    if raw_tools == "all":
        tools: frozenset[str] | None = None
    elif raw_tools:
        tools = frozenset(raw_tools.replace(",", " ").split())
    else:
        tools = DEFAULT_HTTP_TOOLS
    return HttpSettings(host=host, port=int(raw_port), key_file=key_file, tools=tools)


def read_api_key(key_file: Path) -> str:
    """Read the key, refusing files that others could read or keys too short to resist guessing."""
    try:
        mode = stat.S_IMODE(key_file.stat().st_mode)
    except FileNotFoundError as exc:
        raise ConfigError(f"API key file {key_file} does not exist") from exc
    if mode & 0o077:
        raise ConfigError(f"API key file {key_file} has mode {mode:o}; run: chmod 600 '{key_file}'")
    key = key_file.read_text(encoding="utf-8").strip()
    if len(key) < MIN_KEY_LENGTH:
        raise ConfigError(f"API key in {key_file} must be at least {MIN_KEY_LENGTH} characters")
    return key


def presented_key(headers: Mapping[str, str]) -> str | None:
    """Extract the key from `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    auth = headers.get("authorization", "")
    scheme, _, value = auth.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    api_key = headers.get("x-api-key", "").strip()
    return api_key or None


def key_matches(presented: str | None, expected: str) -> bool:
    if presented is None:
        return False
    # compare_digest needs equal types and str inputs must be ASCII, so compare bytes;
    # surrogatepass keeps malformed input from raising instead of being rejected.
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"), expected.encode("utf-8", "surrogatepass")
    )


class ApiKeyMiddleware:
    """Rejects every HTTP request without the API key before the MCP app sees it."""

    def __init__(self, app: ASGIApp, key: str) -> None:
        self._app = app
        self._key = key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan events must pass through: they start the MCP session manager.
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if key_matches(presented_key(headers), self._key):
            await self._app(scope, receive, send)
            return
        client = scope.get("client")
        logger.warning("Rejected request without a valid API key from %s", client)
        body = json.dumps(
            {"error": "unauthorized", "message": "missing or invalid API key"}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="mt5-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def restrict_tools(server: MCPServer, allowed: frozenset[str] | None) -> list[str]:
    """Remove every tool not on the allowlist; return the names that remain."""
    registered = {tool.name for tool in asyncio.run(server.list_tools())}
    if allowed is None:
        return sorted(registered)
    unknown = allowed - registered
    if unknown:
        raise ConfigError(
            f"MT5_HTTP_TOOLS names unknown tools: {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(registered))}"
        )
    for name in registered - allowed:
        server.remove_tool(name)
    return sorted(allowed)


def build_http_app(server: MCPServer, key: str) -> ApiKeyMiddleware:
    # DNS-rebinding protection checks the Host header, which a tunnel sets to its public name.
    # The API key already defeats rebinding (a hostile page cannot know it), so it is off here.
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return ApiKeyMiddleware(app, key)


def serve_http(server: MCPServer, settings: HttpSettings, key: str) -> None:
    import uvicorn

    tools = restrict_tools(server, settings.tools)
    logger.info(
        "Serving MCP over HTTP at http://%s:%d/mcp (API key required, trading off, tools: %s)",
        settings.host,
        settings.port,
        ", ".join(tools),
    )
    app: Any = build_http_app(server, key)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
