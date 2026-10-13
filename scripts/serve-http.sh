#!/usr/bin/env bash
# Serve mt5-mcp over Streamable HTTP for remote MCP clients (for example an agent that reaches
# your Mac through a tunnel). See docs/remote-access.md.
#
# Safety defaults, all deliberate:
#   - listens on 127.0.0.1 only, so the tunnel is the single way in
#   - every request must carry the API key (Authorization: Bearer <key> or X-API-Key: <key>)
#   - trading is forced off; the script refuses to start if MT5_ENABLE_TRADING is set to anything else
#   - only price tools are exposed unless MT5_HTTP_TOOLS says otherwise
#
# Usage: scripts/serve-http.sh [--new-key]
#   --new-key   replace the API key (do this for every new client or session)
#
# Environment overrides (defaults in brackets):
#   MT5_HTTP_PORT      port on 127.0.0.1 [8000]
#   MT5_HTTP_KEY_FILE  API key file, created 0600 if missing
#                      [$HOME/Library/Application Support/mt5-mcp/http.key]
#   MT5_HTTP_TOOLS     space-separated tool allowlist, or "all"
#                      [terminal_status list_symbols get_symbol_info get_tick get_rates get_ticks]
#   MT5_SUPERGATEWAY   supergateway package spec [supergateway@4.1.0]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${MT5_HTTP_PORT:-8000}"
KEY_FILE="${MT5_HTTP_KEY_FILE:-$HOME/Library/Application Support/mt5-mcp/http.key}"
TOOLS="${MT5_HTTP_TOOLS:-terminal_status list_symbols get_symbol_info get_tick get_rates get_ticks}"
SUPERGATEWAY="${MT5_SUPERGATEWAY:-supergateway@4.1.0}"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

# The published supergateway 4.1.0 silently ignores --host, --apiKeyFile and --tools, so this
# script would serve an unauthenticated endpoint on every interface. Disabled until replaced.
die "disabled: supergateway 4.1.0 ignores the security flags this script relies on (see docs/remote-access.md)"

if [ "${MT5_ENABLE_TRADING:-0}" != "0" ]; then
  die "MT5_ENABLE_TRADING must be 0 when serving over HTTP; trading tools are never exposed remotely"
fi
command -v bunx >/dev/null 2>&1 || die "bunx not found; install bun from https://bun.sh"
command -v uv >/dev/null 2>&1 || die "uv not found; install it from https://docs.astral.sh/uv/"
command -v openssl >/dev/null 2>&1 || die "openssl not found"

if [ "${1:-}" = "--new-key" ]; then
  rm -f "$KEY_FILE"
elif [ -n "${1:-}" ]; then
  die "unknown argument: $1"
fi

if [ ! -s "$KEY_FILE" ]; then
  mkdir -p "$(dirname "$KEY_FILE")"
  chmod 700 "$(dirname "$KEY_FILE")"
  # umask in a subshell so the key file is never readable by others, even briefly.
  (umask 077 && openssl rand -hex 32 > "$KEY_FILE")
  log "Created a new API key in $KEY_FILE"
fi
KEY_MODE="$(stat -f '%Lp' "$KEY_FILE")"
[ "$KEY_MODE" = "600" ] || die "$KEY_FILE has mode $KEY_MODE; run: chmod 600 \"$KEY_FILE\""

set -- --stdio "uv --directory \"$REPO_DIR\" run mt5-mcp" \
  --outputTransport streamableHttp \
  --host 127.0.0.1 \
  --port "$PORT" \
  --apiKeyFile "$KEY_FILE"

if [ "$TOOLS" = "all" ]; then
  log "warning: exposing every read-only tool, including account details, positions and history"
else
  for tool in $TOOLS; do
    set -- "$@" --tools "$tool"
  done
fi

log "Serving mt5-mcp on http://127.0.0.1:$PORT/mcp (key required, trading off)"
log "Copy the key with: pbcopy < \"$KEY_FILE\""
MT5_ENABLE_TRADING=0 exec bunx "$SUPERGATEWAY" "$@"
