#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="$HOME/.config/omarchy/extensions/jellyfin-music-app"
APPS_DIR="$HOME/.local/share/applications"
STATE_DIR="$HOME/.local/state/omarchy/extensions/jellyfin-music-app"

echo "🎵 Installing Jellyfin Music Downloader for Omarchy..."

mkdir -p "$TARGET_DIR" "$APPS_DIR" "$STATE_DIR"

# If installing from a separate directory, sync files
if [ "$APP_DIR" != "$TARGET_DIR" ]; then
    cp -r "$APP_DIR"/* "$TARGET_DIR/"
fi

# Ensure scripts are executable
chmod +x "$TARGET_DIR/launcher.sh" "$TARGET_DIR/run.sh" "$TARGET_DIR/install.sh" "$TARGET_DIR/app.py" 2>/dev/null || true
if [ -f "$TARGET_DIR/log-filter.py" ]; then
    chmod +x "$TARGET_DIR/log-filter.py"
    cp "$TARGET_DIR/log-filter.py" "$STATE_DIR/log-filter.py"
fi

# If on Debian/Ubuntu/Linux Mint, check Qt dependency
if command -v apt-get >/dev/null 2>&1 && ! command -v pacman >/dev/null 2>&1; then
    if ! python3 -c "import PyQt5" >/dev/null 2>&1 && ! python3 -c "import PySide6" >/dev/null 2>&1; then
        echo "📦 Installing Qt GUI dependencies for Linux Mint / Ubuntu..."
        sudo apt-get update && sudo apt-get install -y python3-pyqt5 || true
    fi
fi

# Install Desktop Entry
cp "$TARGET_DIR/jellyfin-music-downloader.desktop" "$APPS_DIR/"
update-desktop-database "$APPS_DIR" 2>/dev/null || true

# Initialize default config if not present
if [ ! -f "$TARGET_DIR/config.json" ]; then
    if [ -f "$TARGET_DIR/config.example.json" ]; then
        cp "$TARGET_DIR/config.example.json" "$TARGET_DIR/config.json"
    else
        cat << 'CFG' > "$TARGET_DIR/config.json"
{
  "serverHost": "server-ip-or-hostname",
  "jellyfinWebUrl": "http://localhost:8096",
  "musicFolderUrl": "sftp://server-ip-or-hostname/mnt/media/music",
  "remoteScriptPath": "~/spotdl/ingest.py",
  "defaultUser": ""
}
CFG
    fi
fi

echo "✔ Installation complete!"
echo "🚀 Launch 'Download to Jellyfin (Music)' from your application menu or run:"
echo "   ~/.config/omarchy/extensions/jellyfin-music-app/run.sh"
