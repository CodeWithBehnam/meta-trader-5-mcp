# Remote access: connect a cloud agent to mt5-mcp

`mt5-mcp` speaks MCP over stdio, so only programs on your Mac can use it directly. A cloud-hosted
agent (Grok, a hosted Claude agent, or any client that supports remote MCP servers over Streamable
HTTP) needs two extra pieces:

```
cloud agent ──HTTPS──> tunnel ──> supergateway on 127.0.0.1:8000 ──stdio──> mt5-mcp ──> bridge ──> MT5
```

- [supergateway](https://github.com/supercorp-ai/supergateway) turns the stdio server into a
  Streamable HTTP endpoint.
- A tunnel (Cloudflare Tunnel here, ngrok works too) gives that endpoint a public HTTPS address.

`scripts/serve-http.sh` starts supergateway with safe settings, so you never have to remember the
flags.

## What is exposed, and what is not

| Protection | How |
|---|---|
| No trading | The script refuses to start unless `MT5_ENABLE_TRADING` is `0`. Trading tools are then not registered, and the bridge separately refuses `order_send`. A leaked key cannot place an order. |
| Key required | Every request needs `Authorization: Bearer <key>` or `X-API-Key: <key>`. Anything else gets `401 Unauthorized`. The key lives in a 0600 file and is never on the command line. |
| Not on your network | supergateway listens on every interface by default. The script binds it to `127.0.0.1`, so the tunnel is the only way in. |
| Price data only | By default only `terminal_status`, `list_symbols`, `get_symbol_info`, `get_tick`, `get_rates` and `get_ticks` are exposed. Account details (name, account number, balance), positions and history stay private. |

What still leaves your Mac: market data, and whatever `terminal_status` reports (terminal build,
connection state, broker clock offset).

## Requirements

- Everything from the [README](../README.md) working locally: MT5 open and logged in,
  `scripts/install-bridge.sh` run, `uv sync` done.
- [bun](https://bun.sh) (for `bunx`) and [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/):

```bash
brew install cloudflared
```

## Steps

These are a sequence: each depends on the one before.

### 1. Start the gateway

From the repository folder, in its own terminal:

```bash
scripts/serve-http.sh --new-key
```

`--new-key` creates a fresh key. Leave it off to keep the existing key. The gateway serves
`http://127.0.0.1:8000/mcp` and keeps running until you press Ctrl+C.

### 2. Check it locally

The first request must be refused:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/mcp
```

Expect `401`.

### 3. Open the tunnel

In a second terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

It prints an address like `https://random-words.trycloudflare.com`. Your MCP endpoint is that
address plus `/mcp`. A quick tunnel needs no Cloudflare account, and its address changes every
time you start it.

### 4. Give the agent the endpoint and key

Copy the key to the clipboard without printing it:

```bash
pbcopy < "$HOME/Library/Application Support/mt5-mcp/http.key"
```

Configure the agent's remote MCP server with:

- URL: `https://<your-tunnel>.trycloudflare.com/mcp`
- Transport: Streamable HTTP
- Header: `Authorization: Bearer <key>`

Pass the key as a header, never in the URL, since URLs end up in logs.

### 5. Stop when you are done

Press Ctrl+C in both terminals. The tunnel address stops working at once. Start with
`--new-key` next time so an old key is useless.

## Choosing tools

Set `MT5_HTTP_TOOLS` to a space-separated list:

```bash
MT5_HTTP_TOOLS="get_tick get_rates" scripts/serve-http.sh
```

`MT5_HTTP_TOOLS=all` exposes every read-only tool, including `get_account_info`, `get_positions`
and the history tools. Only do that for an agent you would show your account statement to.
Trading tools are never available through this script.

## Other settings

| Variable | Default | Meaning |
|---|---|---|
| `MT5_HTTP_PORT` | `8000` | Local port |
| `MT5_HTTP_KEY_FILE` | `~/Library/Application Support/mt5-mcp/http.key` | API key file |
| `MT5_HTTP_TOOLS` | the six price tools | Tool allowlist, or `all` |
| `MT5_SUPERGATEWAY` | `supergateway@4.1.0` | Pinned gateway version |

## Things to know

- **The key is shared with the agent's provider.** Whatever you paste into a chat or agent
  config is stored by that service. Use a new key per session and stop the tunnel when idle.
- **Check that your agent supports it.** The client must support remote MCP over Streamable HTTP
  with a custom `Authorization` header. If it cannot send a header, do not fall back to an
  unprotected URL.
- **Your Mac must stay awake**, with MT5 open, for the endpoint to answer.
- **Use a demo account** while you try this out.

## Troubleshooting

- **`401` with the key**: check that the header is exactly `Authorization: Bearer <key>` and that
  you copied the current key (after `--new-key`, the old one stops working).
- **`502` or `1033` from the tunnel**: the gateway is not running, or not on the port the tunnel
  points to.
- **Tool calls fail but `tools/list` works**: MT5 is closed or logged out. Run `terminal_status`
  locally through Claude Code, or read `~/Library/Logs/mt5-mcp/bridge.log`.
