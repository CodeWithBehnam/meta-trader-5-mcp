#!/usr/bin/env bash
# Serve mt5-mcp over Streamable HTTP for remote MCP clients (for example an agent that reaches
# your Mac through a tunnel). See docs/remote-access.md.
#
# The protections live in mt5-mcp itself (src/mt5_mcp/http.py), not in this script:
#   - it binds to a loopback address only and refuses anything else
#   - every request needs the API key (Authorization: Bearer <key> or X-API-Key: <key>), else 401
#   - it refuses to start with MT5_ENABLE_TRADING=1, so trading tools are never served
#   - only allowlisted read-only tools stay registered
# This script only creates the key file and starts the server.
#
# Usage: scripts/serve-http.sh [--new-key]
#   --new-key   replace the API key (do this for every new client or session)
#
# Environment overrides (defaults in brackets):
#   MT5_HTTP_PORT      port on 127.0.0.1 [8000]
#   MT5_HTTP_KEY_FILE  API key file, created 0600 if missing
#                      [$HOME/Library/Application Support/mt5-mcp/http.key]
#   MT5_HTTP_TOOLS     space- or comma-separated tool allowlist, or "all"
#                      [terminal_status list_symbols get_symbol_info get_tick get_rates get_ticks]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
KEY_FILE="${MT5_HTTP_KEY_FILE:-$HOME/Library/Application Support/mt5-mcp/http.key}"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

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

log "Copy the key with: pbcopy < \"$KEY_FILE\""
export MT5_HTTP_KEY_FILE="$KEY_FILE"
export MT5_ENABLE_TRADING=0
exec uv --directory "$REPO_DIR" run mt5-mcp --http
