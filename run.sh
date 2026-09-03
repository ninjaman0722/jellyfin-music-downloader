#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# If running on Wayland with Quickshell available (Omarchy), launch Quickshell GUI
if command -v quickshell >/dev/null 2>&1 && [ -n "$WAYLAND_DISPLAY" ]; then
    exec "$SCRIPT_DIR/launcher.sh"
else
    # Otherwise launch universal Qt desktop window (Linux Mint / X11 / Cinnamon / GNOME)
    exec python3 "$SCRIPT_DIR/app.py"
fi
