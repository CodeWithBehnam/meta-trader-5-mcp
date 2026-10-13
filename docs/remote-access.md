# Remote access: connect a cloud agent to mt5-mcp

`mt5-mcp` normally speaks MCP over stdio, so only programs on your Mac can use it. A cloud-hosted
agent (Grok, a hosted Claude agent, or any client that supports remote MCP servers over Streamable
HTTP) can reach it through HTTP mode plus a tunnel:

```
cloud agent ──HTTPS──> tunnel ──> mt5-mcp --http on 127.0.0.1:8000 ──> bridge ──> MT5
```

- `mt5-mcp --http` serves MCP over Streamable HTTP, using the official MCP SDK.
- A tunnel (Cloudflare Tunnel here, ngrok works too) gives it a public HTTPS address.

`scripts/serve-http.sh` creates the API key and starts HTTP mode.

## What is exposed, and what is not

The protections are enforced by `mt5-mcp` itself (`src/mt5_mcp/http.py`), so no flag can be
forgotten or silently ignored.

| Protection | How |
|---|---|
| Key required | Every request needs `Authorization: Bearer <key>` or `X-API-Key: <key>`, checked in constant time before the MCP app sees it. Anything else gets `401`. The key lives in a 0600 file; the server refuses group- or world-readable key files and keys shorter than 32 characters. |
| No trading | HTTP mode refuses to start if `MT5_ENABLE_TRADING=1`. Trading tools are then never registered, and the bridge separately refuses `order_send`. A leaked key cannot place an order. |
| Not on your network | It binds to `127.0.0.1` and refuses any non-loopback address, so the tunnel is the only way in. |
| Price data only | Only `terminal_status`, `list_symbols`, `get_symbol_info`, `get_tick`, `get_rates` and `get_ticks` are registered. Account details (name, account number, balance), positions and history stay private. |

What still leaves your Mac: market data, and whatever `terminal_status` reports (terminal build,
connection state, broker clock offset).

## Requirements

- Everything from the [README](../README.md) working locally: MT5 open and logged in,
  `scripts/install-bridge.sh` run, `uv sync` done.
- [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/):

```bash
brew install cloudflared
```

## Steps

Each step depends on the one before.

### 1. Start HTTP mode

From the repository folder, in its own terminal:

```bash
scripts/serve-http.sh --new-key
```

`--new-key` creates a fresh key. Leave it off to keep the existing key. It prints the tools it
serves and keeps running until you press Ctrl+C.

### 2. Check it refuses requests without the key

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/mcp
```

Expect `401`. If you get anything else, stop here.

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

Set `MT5_HTTP_TOOLS` to a space- or comma-separated list:

```bash
MT5_HTTP_TOOLS="get_tick get_rates" scripts/serve-http.sh
```

`MT5_HTTP_TOOLS=all` serves every read-only tool, including `get_account_info`, `get_positions`
and the history tools. Only do that for an agent you would show your account statement to. An
unknown tool name stops the server at startup. Trading tools are never available in HTTP mode.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `MT5_HTTP_PORT` | `8000` | Local port |
| `MT5_HTTP_HOST` | `127.0.0.1` | Loopback address only (`127.0.0.1`, `::1` or `localhost`) |
| `MT5_HTTP_KEY_FILE` | `~/Library/Application Support/mt5-mcp/http.key` | API key file |
| `MT5_HTTP_TOOLS` | the six price tools | Tool allowlist, or `all` |

## Things to know

- **The key is shared with the agent's provider.** Whatever you paste into a chat or agent
  config is stored by that service. Use a new key per session and stop the tunnel when idle.
- **Check that your agent supports it.** The client must support remote MCP over Streamable HTTP
  with a custom `Authorization` header. If it cannot send a header, do not try to work around it.
- **DNS-rebinding protection is off in HTTP mode**, because it rejects the tunnel's public
  `Host` header. The API key covers the same attack: a hostile web page cannot know the key.
- **Your Mac must stay awake**, with MT5 open, for the endpoint to answer.
- **Use a demo account** while you try this out.

## Troubleshooting

- **`401` with the key**: check that the header is exactly `Authorization: Bearer <key>` and that
  you copied the current key (after `--new-key`, the old one stops working).
- **`502` or `1033` from the tunnel**: HTTP mode is not running, or not on the port the tunnel
  points to.
- **Startup error about the key file**: run `chmod 600` on it, or start with `--new-key`.
- **Tool calls fail but `tools/list` works**: MT5 is closed or logged out. Read
  `~/Library/Logs/mt5-mcp/bridge.log`.
