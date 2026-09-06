#!/usr/bin/env bash
# ==============================================================================
# Jellyfin Music Downloader V2 - Runtime Entrypoint & Fallback Router
# ==============================================================================
# Ensures daemon health, verifies prerequisites, and launches the graphical
# interface. Features:
#   - Automated daemon health check (GET /health)
#   - Auto-start via systemd user service or background virtualenv uvicorn
#   - Automatic fallback from Quickshell QML to Universal Python Qt client
#   - POSIX hardened execution (set -euo pipefail)
#   - Zero terminal pollution with clean state logging
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${HOME}/.local/state/omarchy/extensions/jellyfin-music-app"
mkdir -p "${STATE_DIR}"
RUN_LOG="${STATE_DIR}/run.log"

DEBUG_MODE="${DEBUG:-0}"
for arg in "$@"; do
    if [ "$arg" = "--debug" ]; then
        DEBUG_MODE=1
        break
    fi
done

log_info() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] [INFO] $1"
    echo "$msg" >> "${RUN_LOG}"
    if [ "$DEBUG_MODE" = "1" ] || [ -t 1 ]; then
        echo -e "\033[0;36m$1\033[0m"
    fi
}

log_warn() {
    local msg="[$(date +'%Y-%m-%d %H:%M:%S')] [WARN] $1"
    echo "$msg" >> "${RUN_LOG}"
    if [ "$DEBUG_MODE" = "1" ] || [ -t 1 ]; then
        echo -e "\033[0;33m$1\033[0m" >&2
    fi
}

# ------------------------------------------------------------------------------
# 1. Resolve Python Runtime
# ------------------------------------------------------------------------------
PY_BIN="python3"
if [ -x "${SCRIPT_DIR}/.venv/bin/python3" ]; then
    PY_BIN="${SCRIPT_DIR}/.venv/bin/python3"
fi

# ------------------------------------------------------------------------------
# 2. Daemon Discovery & Health Check
# ------------------------------------------------------------------------------
DAEMON_URL="http://127.0.0.1:8095"
CONFIG_FILE="${SCRIPT_DIR}/config.json"

if [ -f "${CONFIG_FILE}" ]; then
    eval "$("$PY_BIN" -c "
import json
try:
    with open('${CONFIG_FILE}', 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    if cfg.get('daemonUrl'):
        print(f'export DAEMON_URL=\"{cfg[\"daemonUrl\"]}\"')
    if cfg.get('defaultUser') is not None:
        print(f'export DEFAULT_USER=\"{cfg[\"defaultUser\"]}\"')
    if cfg.get('jellyfinWebUrl'):
        print(f'export JELLYFIN_WEB_URL=\"{cfg[\"jellyfinWebUrl\"]}\"')
    if cfg.get('musicFolderUrl'):
        print(f'export MUSIC_FOLDER_URL=\"{cfg[\"musicFolderUrl\"]}\"')
    if 'autoClipboardDetect' in cfg:
        print(f'export AUTO_CLIPBOARD_DETECT=\"{str(cfg[\"autoClipboardDetect\"]).lower()}\"')
except Exception:
    pass
" 2>/dev/null || true)"
fi
export DAEMON_URL="${DAEMON_URL:-http://127.0.0.1:8095}"

HEALTH_URL="${DAEMON_URL%/}/health"

check_daemon_health() {
    if command -v curl >/dev/null 2>&1; then
        curl -sf -m 2 "${HEALTH_URL}" >/dev/null 2>&1
    else
        "$PY_BIN" -c "
import urllib.request, sys
try:
    with urllib.request.urlopen('${HEALTH_URL}', timeout=2) as res:
        sys.exit(0 if res.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null
    fi
}

# Check if daemon is responding
if check_daemon_health; then
    log_info "✔ Media daemon is active at ${DAEMON_URL}"
else
    # Daemon is not active; attempt auto-start if target is localhost
    if [[ "${DAEMON_URL}" == *"127.0.0.1"* ]] || [[ "${DAEMON_URL}" == *"localhost"* ]]; then
        log_info "🔄 Media daemon is not running. Attempting auto-start..."

        DAEMON_STARTED=0

        # Attempt 1: Systemd user service
        if command -v systemctl >/dev/null 2>&1; then
            if systemctl --user is-active --quiet jellyfin-music-daemon.service 2>/dev/null; then
                DAEMON_STARTED=1
            elif systemctl --user list-unit-files jellyfin-music-daemon.service >/dev/null 2>&1; then
                log_info "  Starting via systemd: jellyfin-music-daemon.service"
                systemctl --user start jellyfin-music-daemon.service 2>/dev/null || true
                DAEMON_STARTED=1
            fi
        fi

        # Attempt 2: Direct background uvicorn spawn via virtualenv
        if [ "$DAEMON_STARTED" -eq 0 ] || ! check_daemon_health; then
            if "$PY_BIN" -c "import uvicorn, server.app.main" >/dev/null 2>&1; then
                log_info "  Starting daemon via ${PY_BIN} in background..."
                nohup "${PY_BIN}" -m uvicorn server.app.main:app \
                    --host 127.0.0.1 --port 8095 \
                    >> "${STATE_DIR}/daemon.log" 2>&1 &
            fi
        fi

        # Wait up to 3.0 seconds for health probe
        for ((i=1; i<=15; i++)); do
            if check_daemon_health; then
                log_info "✔ Media daemon successfully started and responding at ${DAEMON_URL}"
                break
            fi
            sleep 0.2
        done

        if ! check_daemon_health; then
            log_warn "⚠️ Daemon failed to respond to health check within 3s. Client will start in offline mode."
        fi
    else
        log_info "ℹ️ Remote daemon host detected (${DAEMON_URL}); skipping local auto-start."
    fi
fi

# ------------------------------------------------------------------------------
# 3. GUI Dispatch: Quickshell QML with Fallback to Python Qt
# ------------------------------------------------------------------------------
USE_QUICKSHELL=0
if command -v quickshell >/dev/null 2>&1 && [ -n "${WAYLAND_DISPLAY:-}" ] && [ -f "${SCRIPT_DIR}/main.qml" ]; then
    USE_QUICKSHELL=1
fi

if [ "$USE_QUICKSHELL" -eq 1 ]; then
    log_info "🎨 Launching Quickshell QML client (Wayland LayerShell)..."

    # Launch launcher.sh in background and monitor early exit/crash
    "${SCRIPT_DIR}/launcher.sh" "$@" >> "${RUN_LOG}" 2>&1 &
    QS_PID=$!

    # Grace period to verify Quickshell didn't crash on layer-shell initialization
    sleep 1.2

    if kill -0 "$QS_PID" 2>/dev/null; then
        # Running healthy, wait for client termination
        wait "$QS_PID" || true
        exit 0
    fi

    # Check exit code
    set +e
    wait "$QS_PID"
    QS_CODE=$?
    set -e

    if [ "$QS_CODE" -eq 0 ]; then
        # Clean exit
        exit 0
    fi

    log_warn "⚠️ Quickshell exited prematurely (code: ${QS_CODE}). Falling back to Universal Python Qt client..."
fi

# ------------------------------------------------------------------------------
# 4. Universal Python Qt Client Fallback
# ------------------------------------------------------------------------------
APP_PY="${SCRIPT_DIR}/app.py"
if [ ! -f "${APP_PY}" ]; then
    echo "❌ Error: Neither Quickshell nor app.py could be launched." >&2
    exit 1
fi

log_info "🖥️  Launching Universal Python Qt Client (${APP_PY})..."
exec "${PY_BIN}" "${APP_PY}" "$@"
