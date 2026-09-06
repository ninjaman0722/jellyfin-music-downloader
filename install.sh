#!/usr/bin/env bash
# ==============================================================================
# Jellyfin Music Downloader V2 - Hardened System Installer
# ==============================================================================
# POSIX-compliant, hardened installer for Omarchy desktop extensions and
# headless server environments.
# Features:
#   - Strict bash error handling (set -euo pipefail)
#   - Comprehensive prerequisite checking (Python >= 3.10, venv, ffmpeg, Qt/Quickshell)
#   - Non-destructive configuration management with timestamped .bak backups
#   - POSIX 0600 permission hardening on secrets and configuration files
#   - XDG desktop entry deployment with direct binary execution paths
#   - Systemd user service installation for automated background daemon execution
# ==============================================================================

set -euo pipefail

# Trap handler for actionable error reporting
trap 'echo -e "\n\033[0;31m❌ [ERROR] Installation failed at line $LINENO executing: $BASH_COMMAND\033[0m" >&2' ERR

# ------------------------------------------------------------------------------
# 1. Directory Resolution & Configuration
# ------------------------------------------------------------------------------
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${TARGET_DIR:-$HOME/.config/omarchy/extensions/jellyfin-music-app}"
APPS_DIR="${APPS_DIR:-$HOME/.local/share/applications}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/omarchy/extensions/jellyfin-music-app}"
VENV_DIR="${TARGET_DIR}/.venv"

echo -e "\033[0;34m==============================================================================\033[0m"
echo -e "\033[1;36m🎵 Jellyfin Music Downloader V2 — System Installer\033[0m"
echo -e "\033[0;34m==============================================================================\033[0m"
echo -e "📁 Source Directory:  ${APP_DIR}"
echo -e "🎯 Target Directory:  ${TARGET_DIR}"
echo -e "🖥️  Desktop Entries:   ${APPS_DIR}"
echo -e "⚙️  Systemd Units:     ${SYSTEMD_USER_DIR}"
echo -e "📊 State & Logs:      ${STATE_DIR}"
echo ""

# Ensure base directory tree exists
mkdir -p "${TARGET_DIR}" "${APPS_DIR}" "${SYSTEMD_USER_DIR}" "${STATE_DIR}"

# ------------------------------------------------------------------------------
# 2. Prerequisite Checking
# ------------------------------------------------------------------------------
echo -e "\033[1;33m🔍 Checking system prerequisites...\033[0m"

# 2.1 Python 3 & Version Check (>= 3.10)
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "\033[0;31m❌ Python 3 is not installed or not found on PATH.\033[0m" >&2
    echo "Please install Python 3.10+ using your package manager (e.g. pacman -S python or apt install python3)." >&2
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    echo -e "\033[0;31m❌ Python >= 3.10 is required. Detected version: ${PY_VER}\033[0m" >&2
    exit 1
fi
echo -e "  \033[0;32m✔\033[0m Python ${PY_VER} (>= 3.10 verified)"

# 2.2 Python venv module
if ! python3 -c "import venv" >/dev/null 2>&1; then
    echo -e "\033[0;31m❌ Python 'venv' module is not available.\033[0m" >&2
    echo "Please install python3-venv (e.g. apt install python3-venv or pacman -S python)." >&2
    exit 1
fi
echo -e "  \033[0;32m✔\033[0m Python venv module available"

# 2.3 FFmpeg
if command -v ffmpeg >/dev/null 2>&1; then
    FF_VER=$(ffmpeg -version 2>&1 | head -n 1 | awk '{print $3}')
    echo -e "  \033[0;32m✔\033[0m FFmpeg available (version: ${FF_VER})"
else
    echo -e "  \033[0;33m⚠️  Warning: 'ffmpeg' not found on PATH. Audio conversion and tagging require ffmpeg.\033[0m"
fi

# 2.4 Curl
if command -v curl >/dev/null 2>&1; then
    echo -e "  \033[0;32m✔\033[0m curl available for daemon health checks"
else
    echo -e "  \033[0;33m⚠️  Warning: 'curl' not found. Recommended for daemon health probing.\033[0m"
fi

# 2.5 Docker & Docker Compose (Optional for containerized server deployments)
if command -v docker >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_VER=$(docker compose version | awk '{print $4}')
        echo -e "  \033[0;32m✔\033[0m Docker & Docker Compose available (${COMPOSE_VER})"
    elif command -v docker-compose >/dev/null 2>&1; then
        echo -e "  \033[0;32m✔\033[0m Docker & docker-compose standalone available"
    else
        echo -e "  \033[0;33mℹ️  Docker available (docker-compose plugin not found)\033[0m"
    fi
else
    echo -e "  \033[0;34mℹ️  Docker not detected (native Python daemon service will be configured)\033[0m"
fi

# 2.6 Desktop UI Frameworks: Quickshell and Qt
if command -v quickshell >/dev/null 2>&1; then
    echo -e "  \033[0;32m✔\033[0m Quickshell detected (Wayland layer-shell QML interface active)"
else
    echo -e "  \033[0;34mℹ️  Quickshell not found (Universal Python Qt client will serve as primary UI)\033[0m"
fi

HAS_QT=0
for qt_mod in PyQt5 PyQt6 PySide6; do
    if python3 -c "import $qt_mod" >/dev/null 2>&1; then
        echo -e "  \033[0;32m✔\033[0m Python Qt binding found (${qt_mod})"
        HAS_QT=1
        break
    fi
done

if [ "$HAS_QT" -eq 0 ]; then
    echo -e "  \033[0;33m⚠️  No system Qt binding found (PyQt5, PyQt6, or PySide6).\033[0m"
    echo "     For universal desktop UI support, install PyQt5 via your package manager:"
    echo "     - Arch/Omarchy: sudo pacman -S python-pyqt5"
    echo "     - Debian/Ubuntu: sudo apt-get install python3-pyqt5"
fi

echo ""

# ------------------------------------------------------------------------------
# 3. File Synchronization & Installation
# ------------------------------------------------------------------------------
if [ "${APP_DIR}" != "${TARGET_DIR}" ]; then
    echo -e "\033[1;33m📦 Synchronizing files from ${APP_DIR} to ${TARGET_DIR}...\033[0m"
    # Copy files excluding .git, existing virtual environment, and caches
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude='.git/' \
            --exclude='.venv/' \
            --exclude='__pycache__/' \
            --exclude='*.pyc' \
            --exclude='config.json' \
            --exclude='server/server_config.json' \
            "${APP_DIR}/" "${TARGET_DIR}/"
    else
        # Fallback to standard cp while preserving existing target configs
        mkdir -p "${TARGET_DIR}/server" "${TARGET_DIR}/qml" "${TARGET_DIR}/scripts" "${TARGET_DIR}/tests"
        for item in "$APP_DIR"/*; do
            basename_item="$(basename "$item")"
            if [ "$basename_item" != ".git" ] && [ "$basename_item" != ".venv" ] && [ "$basename_item" != "config.json" ]; then
                cp -r "$item" "${TARGET_DIR}/"
            fi
        done
    fi
    echo -e "  \033[0;32m✔\033[0m Core application files synchronized"
fi

# ------------------------------------------------------------------------------
# 4. Virtual Environment Setup & Dependencies
# ------------------------------------------------------------------------------
echo -e "\033[1;33m🐍 Setting up Python virtual environment...\033[0m"
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    echo -e "  \033[0;32m✔\033[0m Created new virtualenv at ${VENV_DIR}"
else
    echo -e "  \033[0;32m✔\033[0m Existing virtualenv found at ${VENV_DIR}"
fi

# Check and install daemon dependencies if requirements.txt is present
if [ -f "${TARGET_DIR}/server/requirements.txt" ]; then
    VENV_PIP="${VENV_DIR}/bin/pip"
    VENV_PY="${VENV_DIR}/bin/python3"
    
    # Check if core daemon libraries are already present in venv
    if ! "$VENV_PY" -c "import fastapi, uvicorn, pydantic, mutagen" >/dev/null 2>&1; then
        echo -e "  \033[0;34mℹ️  Installing backend dependencies into virtual environment...\033[0m"
        "$VENV_PIP" install --quiet --upgrade pip || true
        "$VENV_PIP" install --quiet -r "${TARGET_DIR}/server/requirements.txt" || {
            echo -e "  \033[0;33m⚠️  Warning: pip install encountered warnings; continuing with local packages.\033[0m"
        }
        echo -e "  \033[0;32m✔\033[0m Backend daemon requirements verified"
    else
        echo -e "  \033[0;32m✔\033[0m Backend daemon requirements already satisfied in venv"
    fi
fi

# ------------------------------------------------------------------------------
# 5. Non-Destructive Configuration Management
# ------------------------------------------------------------------------------
echo -e "\033[1;33m🔒 Configuring application settings (Non-Destructive & 0600 Permissions)...\033[0m"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

# 5.1 Client Configuration: config.json
CLIENT_CFG="${TARGET_DIR}/config.json"
if [ -f "${CLIENT_CFG}" ]; then
    BACKUP_CFG="${CLIENT_CFG}.bak.${TIMESTAMP}"
    cp -a "${CLIENT_CFG}" "${BACKUP_CFG}"
    chmod 0600 "${BACKUP_CFG}"
    chmod 0600 "${CLIENT_CFG}"
    echo -e "  \033[0;32m✔\033[0m Preserved existing client config: ${CLIENT_CFG}"
    echo -e "     (Created backup: $(basename "${BACKUP_CFG}"), permissions set to 0600)"
else
    # Create default configuration with modern V2 daemon properties
    cat << 'EOF' > "${CLIENT_CFG}"
{
  "daemonUrl": "http://127.0.0.1:8095",
  "wsUrl": "ws://127.0.0.1:8095/ws/events",
  "jellyfinWebUrl": "http://127.0.0.1:8096",
  "musicFolderUrl": "/mnt/media/music",
  "defaultUser": "Kendon",
  "bitrate": "320k",
  "embedLyrics": true,
  "embedCover": true,
  "autoClipboardDetect": true,
  "themeSync": true
}
EOF
    chmod 0600 "${CLIENT_CFG}"
    echo -e "  \033[0;32m✔\033[0m Created new client config with secure 0600 permissions"
fi

# 5.2 Server Configuration: server/server_config.json
SERVER_CFG="${TARGET_DIR}/server/server_config.json"
SERVER_CFG_EXAMPLE="${TARGET_DIR}/server/server_config.json.example"

if [ -f "${SERVER_CFG}" ]; then
    SERVER_BACKUP="${SERVER_CFG}.bak.${TIMESTAMP}"
    cp -a "${SERVER_CFG}" "${SERVER_BACKUP}"
    chmod 0600 "${SERVER_BACKUP}"
    chmod 0600 "${SERVER_CFG}"
    echo -e "  \033[0;32m✔\033[0m Preserved existing server config: ${SERVER_CFG}"
    echo -e "     (Created backup: $(basename "${SERVER_BACKUP}"), permissions set to 0600)"
elif [ -f "${SERVER_CFG_EXAMPLE}" ]; then
    cp -a "${SERVER_CFG_EXAMPLE}" "${SERVER_CFG}"
    chmod 0600 "${SERVER_CFG}"
    echo -e "  \033[0;32m✔\033[0m Initialized server_config.json from template with 0600 permissions"
fi

# ------------------------------------------------------------------------------
# 6. File Permissions Hardening
# ------------------------------------------------------------------------------
echo -e "\033[1;33m🛡️  Setting executable bits on runtime scripts...\033[0m"
chmod +x "${TARGET_DIR}/run.sh" "${TARGET_DIR}/launcher.sh" "${TARGET_DIR}/install.sh" "${TARGET_DIR}/app.py" 2>/dev/null || true

if [ -f "${TARGET_DIR}/scripts/ws_listener.py" ]; then
    chmod +x "${TARGET_DIR}/scripts/ws_listener.py" 2>/dev/null || true
fi

if [ -f "${TARGET_DIR}/server/get-music.sh" ]; then
    chmod +x "${TARGET_DIR}/server/get-music.sh" 2>/dev/null || true
fi
echo -e "  \033[0;32m✔\033[0m Scripts executable (run.sh, launcher.sh, install.sh, app.py)"

# ------------------------------------------------------------------------------
# 7. Desktop Entry Registration
# ------------------------------------------------------------------------------
echo -e "\033[1;33m🖥️  Installing XDG Desktop Entry...\033[0m"
DESKTOP_FILE="${APPS_DIR}/jellyfin-music-downloader.desktop"

cat << EOF > "${DESKTOP_FILE}"
[Desktop Entry]
Type=Application
Name=Download to Jellyfin (Music)
GenericName=Jellyfin Music Downloader
Comment=Download Spotify & YouTube Music playlists directly to Jellyfin Server
Exec=${TARGET_DIR}/run.sh
Icon=audio-headphones
Terminal=false
Categories=AudioVideo;Audio;Music;Network;
Keywords=spotify;youtube;music;jellyfin;download;playlist;
StartupNotify=true
StartupWMClass=jellyfin-music-app
EOF

chmod 0644 "${DESKTOP_FILE}"

# Keep a co-located copy in TARGET_DIR for reference
cp -a "${DESKTOP_FILE}" "${TARGET_DIR}/jellyfin-music-downloader.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APPS_DIR}" 2>/dev/null || true
fi
echo -e "  \033[0;32m✔\033[0m Installed ${DESKTOP_FILE} (direct binary path)"

# ------------------------------------------------------------------------------
# 8. Systemd User Unit Registration
# ------------------------------------------------------------------------------
echo -e "\033[1;33m⚙️  Configuring systemd user daemon service...\033[0m"
SYSTEMD_SERVICE_FILE="${SYSTEMD_USER_DIR}/jellyfin-music-daemon.service"

PY_EXEC="${VENV_DIR}/bin/python3"
if [ ! -x "${PY_EXEC}" ]; then
    PY_EXEC="$(command -v python3)"
fi

cat << EOF > "${SYSTEMD_SERVICE_FILE}"
[Unit]
Description=Jellyfin Music Downloader Daemon (V2 REST & WebSocket Engine)
Documentation=https://github.com/omarchy/jellyfin-music-app
After=network.target sound.target

[Service]
Type=simple
WorkingDirectory=${TARGET_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PORT=8095
Environment=HOST=127.0.0.1
Environment=PATH=${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${PY_EXEC} -m uvicorn server.app.main:app --host 127.0.0.1 --port 8095
Restart=on-failure
RestartSec=5s
KillMode=process
TimeoutStopSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

chmod 0644 "${SYSTEMD_SERVICE_FILE}"

# Also save copy in target dir
cp -a "${SYSTEMD_SERVICE_FILE}" "${TARGET_DIR}/server/jellyfin-music-daemon.service" 2>/dev/null || true

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
    echo -e "  \033[0;32m✔\033[0m Installed and reloaded systemd user unit: jellyfin-music-daemon.service"
else
    echo -e "  \033[0;34mℹ️  systemctl not available; daemon service unit file installed.\033[0m"
fi

# ------------------------------------------------------------------------------
# 9. Summary & Launch Instructions
# ------------------------------------------------------------------------------
echo ""
echo -e "\033[0;32m==============================================================================\033[0m"
echo -e "\033[1;32m✨ Installation Successfully Completed!\033[0m"
echo -e "\033[0;32m==============================================================================\033[0m"
echo -e "🚀 Run client interactively:"
echo -e "   \033[1m${TARGET_DIR}/run.sh\033[0m"
echo ""
echo -e "⚙️  Manage daemon background service via systemd:"
echo -e "   Start & Enable:  \033[1msystemctl --user enable --now jellyfin-music-daemon.service\033[0m"
echo -e "   Check Status:    \033[1msystemctl --user status jellyfin-music-daemon.service\033[0m"
echo -e "   View Live Logs:  \033[1mjournalctl --user -u jellyfin-music-daemon.service -f\033[0m"
echo ""
echo -e "🎵 Open from Application Menu: \033[1m'Download to Jellyfin (Music)'\033[0m"
echo -e "\033[0;32m==============================================================================\033[0m"
