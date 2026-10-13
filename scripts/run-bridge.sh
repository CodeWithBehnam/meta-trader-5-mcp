#!/usr/bin/env bash
# Run the MT5 bridge in the foreground for debugging. The MCP server normally
# launches it itself; use this to watch the log or test without the server.
#
# Environment overrides (defaults in brackets):
#   MT5_APP_PATH, MT5_WINEPREFIX   as in install-bridge.sh
#   MT5_BRIDGE_TOKEN_FILE          [$HOME/Library/Application Support/mt5-mcp/bridge.token]
#   MT5_BRIDGE_PORT                [18812]
#   MT5_TERMINAL_PATH              [C:\Program Files\MetaTrader 5\terminal64.exe]
#   MT5_BRIDGE_ALLOW_TRADING       [0]; set to 1 to let order_send reach the terminal
set -euo pipefail

APP_PATH="${MT5_APP_PATH:-/Applications/MetaTrader 5.app}"
PREFIX="${MT5_WINEPREFIX:-$HOME/Library/Application Support/net.metaquotes.wine.metatrader5}"
TOKEN_FILE="${MT5_BRIDGE_TOKEN_FILE:-$HOME/Library/Application Support/mt5-mcp/bridge.token}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/../bridge" && pwd)"

WINE_BIN="$APP_PATH/Contents/SharedSupport/wine/bin/wine64"
WINE_LIB="$APP_PATH/Contents/SharedSupport/wine/lib/external"
ROOT_WIN='C:\mt5-bridge'

[ -x "$WINE_BIN" ] || { echo "error: wine64 not found at '$WINE_BIN'" >&2; exit 1; }
pgrep -f terminal64.exe >/dev/null || {
  echo "error: terminal64.exe is not running; open the MetaTrader 5 app and log in" >&2
  exit 1
}

# Create the shared token file (0600) if the MCP server has not done so yet.
if [ ! -s "$TOKEN_FILE" ]; then
  mkdir -p "$(dirname "$TOKEN_FILE")"
  (umask 077 && head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' >"$TOKEN_FILE")
  chmod 600 "$TOKEN_FILE"
  echo "created token file $TOKEN_FILE" >&2
fi
mode="$(stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null || stat -c '%a' "$TOKEN_FILE" 2>/dev/null || true)"
if [ -n "$mode" ] && [ "$mode" != "600" ]; then
  echo "warning: token file $TOKEN_FILE has mode $mode, expected 600" >&2
fi
# The token goes in the environment, not argv, where `ps` would show it to other users.
MT5_BRIDGE_TOKEN="$(cat "$TOKEN_FILE")"
export MT5_BRIDGE_TOKEN

BRIDGE_WIN="Z:${BRIDGE_DIR//\//\\}"

# exec so signals (Ctrl-C) reach Wine directly.
exec env \
  WINEPREFIX="$PREFIX" \
  WINEDLLOVERRIDES="mscoree=;winedbg.exe=d" \
  WINEDEBUG=-all \
  DYLD_FALLBACK_LIBRARY_PATH="$WINE_LIB" \
  UV_PYTHON_INSTALL_DIR="$ROOT_WIN\\python" \
  UV_CACHE_DIR="$ROOT_WIN\\cache" \
  UV_PROJECT_ENVIRONMENT="$ROOT_WIN\\venv" \
  UV_PYTHON_DOWNLOADS=never \
  PYTHONUNBUFFERED=1 \
  MT5_BRIDGE_PORT="${MT5_BRIDGE_PORT:-18812}" \
  MT5_BRIDGE_ALLOW_TRADING="${MT5_BRIDGE_ALLOW_TRADING:-0}" \
  MT5_TERMINAL_PATH="${MT5_TERMINAL_PATH:-C:\\Program Files\\MetaTrader 5\\terminal64.exe}" \
  "$WINE_BIN" "$ROOT_WIN\\bin\\uv.exe" run --frozen --no-dev --project "$BRIDGE_WIN" \
  python -m mt5_bridge
