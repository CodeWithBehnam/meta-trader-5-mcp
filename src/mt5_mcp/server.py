"""MCP server entry point: wires configuration, bridge session and tools."""

import logging
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from mcp.server import MCPServer

from .config import Config, ConfigError, load_config
from .context import AppContext, BridgeCaller, BridgeSession, BrokerClock
from .launcher import ensure_token
from .tools_read import register_read_tools
from .tools_trade import register_trading_tools

logger = logging.getLogger("mt5_mcp")

INSTRUCTIONS = (
    "MetaTrader 5 access through a local bridge. Times: inputs are ISO-8601 UTC; outputs carry "
    "raw broker-time epochs plus *_utc and *_broker strings. Volumes are lots. "
    "Call terminal_status first if anything fails."
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def build_server(
    config: Config,
    bridge: BridgeCaller,
    *,
    now: Callable[[], datetime] = utc_now,
) -> MCPServer:
    """Assemble the server. Trading tools exist only when trading is enabled."""
    ctx = AppContext(
        config=config,
        bridge=bridge,
        clock=BrokerClock(bridge, now, override=config.broker_utc_offset_seconds),
        now=now,
    )
    server = MCPServer("mt5", instructions=INSTRUCTIONS)
    register_read_tools(server, ctx)
    if config.enable_trading:
        register_trading_tools(server, ctx)
    return server


def configure_logging() -> None:
    # stdout carries the MCP protocol, so every log line goes to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("MT5_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_server(env: Mapping[str, str]) -> MCPServer:
    config = load_config(env, home=Path.home(), package_file=Path(__file__))
    token = ensure_token(config.token_file)
    return build_server(config, BridgeSession(config, token))


def main() -> None:
    configure_logging()
    try:
        server = create_server(os.environ)
    except ConfigError as exc:
        logger.error("Invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    server.run()


if __name__ == "__main__":
    main()
