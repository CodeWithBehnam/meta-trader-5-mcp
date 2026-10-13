# mt5-mcp

An MCP server that lets AI agents (Claude Code, Claude Desktop and other MCP clients) read market
data from, and optionally trade through, MetaTrader 5 on macOS, where MT5 runs inside the official
MetaQuotes app's bundled Wine.

Project page: <https://codewithbehnam.github.io/meta-trader-5-mcp/>

> Not affiliated with or endorsed by MetaQuotes. Trading carries risk. This software comes with no
> warranty, so test on a demo account first.

## How it works

```
MCP client (Claude Code / Desktop)
   │  stdio
mt5-mcp            macOS, Python 3.13, uv           src/mt5_mcp/
   │  NDJSON over TCP 127.0.0.1:18812, shared token
mt5-bridge         inside Wine, Windows CPython 3.12  bridge/src/mt5_bridge/
   │  official MetaTrader5 package (local IPC)
terminal64.exe     the MT5 terminal you are logged into
```

The official `MetaTrader5` Python package is Windows-only and must run in the same Wine prefix as
the terminal. The bridge runs there under Windows uv. `mt5-mcp` starts the bridge on the first tool
call, so normally you never start it yourself.

## Requirements

- macOS with the official [MetaTrader 5 app](https://www.metatrader5.com/en/download) installed in
  `/Applications`, opened and logged into a broker account.
- [uv](https://docs.astral.sh/uv/) on the Mac.

## Install

```bash
git clone https://github.com/CodeWithBehnam/meta-trader-5-mcp.git
```

```bash
cd meta-trader-5-mcp
```

Install the bridge into the MT5 Wine prefix (MetaTrader 5 must be running and logged in for the
bridge to connect later, but not for the install):

```bash
scripts/install-bridge.sh
```

This puts Windows uv 0.13.0 (checksum-verified), CPython 3.12 and the bridge's environment into
`C:\mt5-bridge` inside the MT5 Wine prefix. It changes nothing else in MT5. Running it again is safe.
Rerun it after an MT5 app update if the bridge stops starting.

Install the Mac side:

```bash
uv sync
```

## Register with Claude Code

```bash
claude mcp add --transport stdio --scope user mt5 -- uv --directory /path/to/meta-trader-5-mcp run mt5-mcp
```

To enable trading (see [Trading](#trading) first), add env options before the name:

```bash
claude mcp add --transport stdio --scope user --env MT5_ENABLE_TRADING=1 --env MT5_ALLOWED_SYMBOLS=EURUSD,XAUUSD --env MT5_MAX_VOLUME=0.1 mt5 -- uv --directory /path/to/meta-trader-5-mcp run mt5-mcp
```

For Claude Desktop, add this to `~/Library/Application Support/Claude/claude_desktop_config.json`.
Desktop does not inherit your shell `PATH`, so give the full path to `uv` (`which uv`):

```json
{
  "mcpServers": {
    "mt5": {
      "command": "/path/to/uv",
      "args": ["--directory", "/path/to/meta-trader-5-mcp", "run", "mt5-mcp"],
      "env": { "MT5_ENABLE_TRADING": "0" }
    }
  }
}
```

## Remote agents

To let a cloud agent (Grok or any client with remote MCP support) use it over HTTPS, with a key,
price tools only and trading forced off, follow [docs/remote-access.md](docs/remote-access.md).

## Tools

Always available (read-only):

| Tool | Purpose |
|---|---|
| `terminal_status` | Bridge, terminal and connection state, Algo Trading flag, broker UTC offset |
| `get_account_info` | Balance, equity, margin, leverage, currency |
| `list_symbols` | Symbols matching an MT5 group pattern such as `*USD*` |
| `get_symbol_info` | Full symbol specification |
| `get_tick` | Latest bid/ask |
| `get_rates` | OHLC bars by count or ISO-8601 UTC range (capped by `MT5_MAX_BARS`) |
| `get_ticks` | Ticks from a UTC start time |
| `get_positions`, `get_orders` | Open positions and pending orders |
| `get_history_orders`, `get_history_deals` | History, default last 7 days |
| `calc_margin`, `calc_profit` | Broker-side margin and profit calculations |
| `check_order` | Dry run of an order through MT5's `order_check`. Nothing is sent. |

Only when `MT5_ENABLE_TRADING=1`: `place_order`, `modify_position_sltp`, `modify_pending_order`,
`close_position`, `cancel_order`.

### Time

MT5 reports broker server time as if it were UTC. Tools take ISO-8601 UTC inputs and return the raw
broker epoch plus `*_utc` fields (and `*_broker` on single records).

The broker offset is estimated from the freshest tick across all symbols, including 24/7 crypto. An
estimate is accepted only if that tick is within 90 s of a whole quarter-hour offset. When no
confident estimate is available, the last confident value is used and responses carry
`broker_offset_confident: false`. If there has never been one, ISO-based queries are refused. Set
`MT5_BROKER_UTC_OFFSET_HOURS` to skip estimation. Count-based queries always work.

## Trading

Trading tools are off by default and not even listed to the agent. When enabled, every order goes
through these steps:

1. The guards check the symbol against `MT5_ALLOWED_SYMBOLS` before any other lookup. They then
   check the volume against `MT5_MAX_VOLUME` and slippage against `MT5_MAX_DEVIATION`, and
   refuse a past expiration.
2. `order_check` runs. Any non-zero result refuses the order.
3. An `intent` record is written to the audit log. If that write fails, the order is not sent.
4. `order_send` runs once. Neither `mt5-mcp` nor the bridge ever retries it. If the connection
   drops mid-send, the tool reports the outcome as unknown, so check `get_positions` and
   `get_history_deals`.
5. The outcome is appended to the audit log. Guard refusals and failed checks are logged too.

The bridge also refuses `order_send` unless `mt5-mcp` launched it with trading enabled.

MT5 itself must allow it: the **Algo Trading** button in the terminal toolbar must be on, or orders
fail with retcode 10027. That button is terminal-wide, so it also enables any Expert Advisors
attached to your charts. Test on a demo account first.

The guards check what the agent asks for. Your broker account is the real limit, so size the account
accordingly.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MT5_ENABLE_TRADING` | `0` | `1` registers trading tools |
| `MT5_ALLOWED_SYMBOLS` | unset (all) | Comma-separated symbols allowed for orders. A value with no symbols in it (`" "`, `","`) is an error. |
| `MT5_MAX_VOLUME` | `1.0` | Maximum lots per order |
| `MT5_MAX_DEVIATION` | `50` | Maximum slippage in points. The default deviation is 20, capped at this value. |
| `MT5_BROKER_UTC_OFFSET_HOURS` | unset (estimated) | Fixed broker offset (multiple of 0.25) that skips estimation |
| `MT5_MAX_BARS` | `5000` | Cap for bars and ticks per call |
| `MT5_AUDIT_LOG` | `~/Library/Logs/mt5-mcp/audit.jsonl` | Trading audit log |
| `MT5_BRIDGE_LOG` | `~/Library/Logs/mt5-mcp/bridge.log` | Bridge stdout/stderr |
| `MT5_BRIDGE_AUTOSTART` | `1` | Start the bridge on demand |
| `MT5_BRIDGE_HOST`, `MT5_BRIDGE_PORT` | `127.0.0.1`, `18812` | Bridge address |
| `MT5_TOKEN_FILE` | `~/Library/Application Support/mt5-mcp/bridge.token` | Shared secret (created 0600) |
| `MT5_APP_PATH` | `/Applications/MetaTrader 5.app` | MT5 app bundle |
| `MT5_WINEPREFIX` | `~/Library/Application Support/net.metaquotes.wine.metatrader5` | MT5 Wine prefix |
| `MT5_BRIDGE_DIR` | `C:\mt5-bridge` | Bridge toolchain inside the prefix |
| `MT5_BRIDGE_PROJECT` | this repo's `bridge/` as a `Z:\` path | Bridge project location |
| `MT5_TERMINAL_PATH` | `C:\Program Files\MetaTrader 5\terminal64.exe` | Terminal executable |

## Troubleshooting

- **"open the MetaTrader 5 app"**: the terminal is not running. Open it and log in.
- **Bridge fails to start**: read `~/Library/Logs/mt5-mcp/bridge.log`. Exit code 3 means the
  `MetaTrader5` package failed to import, so rerun `scripts/install-bridge.sh`.
- **Debug the bridge by hand**: stop `mt5-mcp`, then run `scripts/run-bridge.sh` in a terminal.
- **numpy**: the bridge pins `numpy<2`. numpy 2 calls a function Wine 10 does not implement.
- **Stop a running bridge**: `pkill -f mt5_bridge`. The next tool call starts a fresh one.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check && uv run ruff format --check && uv run ty check
```

The bridge is a separate uv project, and its pure modules are tested on the Mac:

```bash
uv --directory bridge run pytest
```

## Licence

[MIT](LICENSE)
