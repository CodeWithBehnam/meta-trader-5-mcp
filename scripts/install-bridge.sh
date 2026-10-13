#!/usr/bin/env bash
# Install the Windows-side toolchain for the MT5 bridge inside the MetaTrader 5
# app's Wine prefix: uv.exe, CPython 3.12 and the bridge virtualenv.
#
# Idempotent: every step is skipped or becomes a fast no-op when already done.
#
# Environment overrides (defaults in brackets):
#   MT5_APP_PATH     MetaTrader 5 application bundle [/Applications/MetaTrader 5.app]
#   MT5_WINEPREFIX   Wine prefix used by that app
#                    [$HOME/Library/Application Support/net.metaquotes.wine.metatrader5]
#   MT5_UV_VERSION   Windows uv release to install [0.13.0]
#   MT5_WINE_TIMEOUT Seconds allowed for each Wine command [900]
set -euo pipefail

DEFAULT_UV_VERSION="0.13.0"
# Pinned so a tampered release asset and a tampered .sha256 file cannot agree.
DEFAULT_UV_SHA256="088962f9e7b7bd9ea740c04c650b2a21c8928c345bd99ac24350dc924dba656c"
UV_VERSION="${MT5_UV_VERSION:-$DEFAULT_UV_VERSION}"
APP_PATH="${MT5_APP_PATH:-/Applications/MetaTrader 5.app}"
PREFIX="${MT5_WINEPREFIX:-$HOME/Library/Application Support/net.metaquotes.wine.metatrader5}"
WINE_TIMEOUT="${MT5_WINE_TIMEOUT:-900}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/../bridge" && pwd)"

WINE_BIN="$APP_PATH/Contents/SharedSupport/wine/bin/wine64"
WINE_LIB="$APP_PATH/Contents/SharedSupport/wine/lib/external"
ROOT_UNIX="$PREFIX/drive_c/mt5-bridge"
ROOT_WIN='C:\mt5-bridge'
UV_ZIP_NAME="uv-x86_64-pc-windows-msvc.zip"
UV_BASE_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

[ -x "$WINE_BIN" ] || die "wine64 not found at '$WINE_BIN' (set MT5_APP_PATH)"
[ -d "$PREFIX/drive_c" ] || die "Wine prefix not found at '$PREFIX' (set MT5_WINEPREFIX)"

# Wine itself can hang forever on a crash dialog, so every call gets a timeout.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD=(timeout "$WINE_TIMEOUT")
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD=(gtimeout "$WINE_TIMEOUT")
else
  TIMEOUT_CMD=()
  log "warning: no 'timeout' command found; Wine calls are not time-limited"
fi

# Run a Windows program under Wine with the environment the bridge also uses.
# `env` keeps these assignments local to the child process.
wine_run() {
  env \
    WINEPREFIX="$PREFIX" \
    WINEDLLOVERRIDES="mscoree=;winedbg.exe=d" \
    WINEDEBUG=-all \
    DYLD_FALLBACK_LIBRARY_PATH="$WINE_LIB" \
    UV_PYTHON_INSTALL_DIR="$ROOT_WIN\\python" \
    UV_CACHE_DIR="$ROOT_WIN\\cache" \
    UV_PROJECT_ENVIRONMENT="$ROOT_WIN\\venv" \
    UV_PYTHON_DOWNLOADS=manual \
    ${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} \
    "$WINE_BIN" "$@"
}

UV_EXE_UNIX="$ROOT_UNIX/bin/uv.exe"
UV_EXE_WIN="$ROOT_WIN\\bin\\uv.exe"

installed_uv_version() {
  [ -f "$UV_EXE_UNIX" ] || return 0
  wine_run "$UV_EXE_WIN" --version 2>/dev/null | awk '{print $2}' | tr -d '\r' || true
}

install_uv() {
  command -v curl >/dev/null 2>&1 || die "curl is required"
  command -v unzip >/dev/null 2>&1 || die "unzip is required"
  command -v shasum >/dev/null 2>&1 || die "shasum is required"

  local work
  work="$(mktemp -d "${TMPDIR:-/tmp}/mt5-bridge-install.XXXXXX")"
  trap 'rm -rf "$work"' EXIT

  log "downloading uv $UV_VERSION for Windows"
  # HTTPS only, including across redirects, and no TLS older than 1.2.
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --output "$work/$UV_ZIP_NAME" "$UV_BASE_URL/$UV_ZIP_NAME"
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
    --output "$work/$UV_ZIP_NAME.sha256" "$UV_BASE_URL/$UV_ZIP_NAME.sha256"

  local expected actual
  expected="$(awk '{print $1}' "$work/$UV_ZIP_NAME.sha256")"
  actual="$(shasum -a 256 "$work/$UV_ZIP_NAME" | awk '{print $1}')"
  [ -n "$expected" ] || die "empty checksum file for $UV_ZIP_NAME"
  [ "$expected" = "$actual" ] || die "checksum mismatch for $UV_ZIP_NAME (expected $expected, got $actual)"
  if [ "$UV_VERSION" = "$DEFAULT_UV_VERSION" ]; then
    [ "$actual" = "$DEFAULT_UV_SHA256" ] \
      || die "$UV_ZIP_NAME does not match the pinned sha256 for uv $UV_VERSION (got $actual)"
  else
    log "warning: uv $UV_VERSION has no pinned sha256; trusting the downloaded .sha256 only"
  fi

  mkdir -p "$ROOT_UNIX/bin"
  unzip -o -q "$work/$UV_ZIP_NAME" -d "$ROOT_UNIX/bin"
  log "installed uv $UV_VERSION into $ROOT_UNIX/bin"
  rm -rf "$work"
  trap - EXIT
}

mkdir -p "$ROOT_UNIX"

current="$(installed_uv_version)"
if [ "$current" = "$UV_VERSION" ]; then
  log "uv $UV_VERSION already installed"
else
  install_uv
fi

log "ensuring CPython 3.12 is installed"
wine_run "$UV_EXE_WIN" python install --no-bin 3.12

# Z: maps to / inside Wine, so a POSIX path becomes Z:\path\with\backslashes.
BRIDGE_WIN="Z:${BRIDGE_DIR//\//\\}"
log "syncing the bridge environment from $BRIDGE_WIN"
wine_run "$UV_EXE_WIN" sync --frozen --no-dev --project "$BRIDGE_WIN"

log "done. Start it from the MCP server, or manually with scripts/run-bridge.sh"
