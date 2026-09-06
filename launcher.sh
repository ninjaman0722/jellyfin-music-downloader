#!/usr/bin/env bash
# ==============================================================================
# Jellyfin Music Ingestion Launcher (Quickshell QML Native App)
# ==============================================================================
# Launches the Omarchy Wayland layer-shell QML interface using Quickshell.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QML_ENTRY="${SCRIPT_DIR}/main.qml"

if ! command -v quickshell >/dev/null 2>&1; then
    echo "❌ Error: 'quickshell' is not installed or not available on PATH." >&2
    echo "Quickshell is required for the native Omarchy Wayland QML UI." >&2
    exit 127
fi

if [ ! -f "${QML_ENTRY}" ]; then
    echo "❌ Error: Entrypoint QML file not found: ${QML_ENTRY}" >&2
    exit 1
fi

if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "⚠️ Warning: WAYLAND_DISPLAY is not set. Quickshell requires an active Wayland compositor." >&2
fi

# Execute Quickshell with root QML file and pass all command-line arguments
exec quickshell -p "${QML_ENTRY}" "$@"
