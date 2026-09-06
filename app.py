#!/usr/bin/env python3
"""
Jellyfin Music Downloader - Universal Desktop Client (V2 Rewrite)
Compatible with Omarchy, Arch, Ubuntu, Debian, Fedora, Linux Mint.
Supports PySide6, PyQt6, and PyQt5 seamlessly.

Features:
- REST API integration: /health, /api/config, /api/users, /api/resolve, /api/ingest, /api/cancel
- WebSocket live event stream: ws://host:port/ws/events?client_id=<uuid>
- Non-blocking QThread architecture with thread-safe Qt Signals
- Drag-and-drop streaming link ingestion (Spotify, YouTube Music)
- Clipboard auto-detection (QClipboard.dataChanged) with deduplication
- Compound progress parser supporting PROGRESS: 50|5|10 and JSON frames
- Dynamic Omarchy colors.toml theming with live QFileSystemWatcher reload
- Pre-flight diff analysis card displaying total, library, and new counts
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# ==============================================================================
# 1. Universal Qt Compatibility Layer
# ==============================================================================

QT_BINDING = None

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal as Signal, pyqtSlot as Slot
        QT_BINDING = "PyQt6"
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
            from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal as Signal, pyqtSlot as Slot
            QT_BINDING = "PyQt5"
        except ImportError:
            print("ERROR: No supported Qt binding found.")
            print("Please install PyQt5, PyQt6, or PySide6:")
            print("  Arch / Omarchy:   sudo pacman -S python-pyqt5 (or python-pyqt6)")
            print("  Ubuntu / Debian:  sudo apt install python3-pyqt5 (or python3-pyqt6)")
            print("  Generic / Pip:    pip install PySide6")
            sys.exit(1)

# Optional QtWebSockets import
HAS_QT_WEBSOCKETS = False
try:
    if QT_BINDING == "PySide6":
        from PySide6.QtWebSockets import QWebSocket
    elif QT_BINDING == "PyQt6":
        from PyQt6.QtWebSockets import QWebSocket
    elif QT_BINDING == "PyQt5":
        from PyQt5.QtWebSockets import QWebSocket
    HAS_QT_WEBSOCKETS = True
except ImportError:
    QWebSocket = None
    HAS_QT_WEBSOCKETS = False

# Optional websockets package import
HAS_WEBSOCKETS_PKG = False
try:
    from websockets.sync.client import connect as ws_sync_connect
    HAS_WEBSOCKETS_PKG = True
except ImportError:
    ws_sync_connect = None
    HAS_WEBSOCKETS_PKG = False

# Optional httpx package import
HAS_HTTPX = False
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("qt_client")


# ==============================================================================
# 2. Configuration & Paths
# ==============================================================================

CONFIG_DIR = os.path.expanduser("~/.config/omarchy/extensions/jellyfin-music-app")
STATE_DIR = os.path.expanduser("~/.local/state/omarchy/extensions/jellyfin-music-app")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
THEME_FILE = os.path.expanduser("~/.local/state/omarchy/current/theme/colors.toml")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

DEFAULT_CONFIG: Dict[str, Any] = {
    "daemonUrl": "http://127.0.0.1:8095",
    "wsUrl": "ws://127.0.0.1:8095/ws/events",
    "jellyfinWebUrl": "http://127.0.0.1:8096",
    "musicFolderUrl": "/mnt/media/music",
    "defaultUser": "",
    "bitrate": "320k",
    "embedLyrics": True,
    "embedCover": True,
    "autoClipboardDetect": True,
    "themeSync": True,
}


# ==============================================================================
# 3. Dynamic Omarchy Theming & QSS Generation
# ==============================================================================

DEFAULT_DARK_THEME: Dict[str, str] = {
    "mode": "dark",
    "accent": "#88c0d0",
    "selection": "#434c5e",
    "muted": "#4c566a",
    "background": "#242933",
    "dark_background": "#1e222a",
    "darker_background": "#191c23",
    "lighter_background": "#2e3440",
    "surface": "#2e3440",
    "border": "#3b4252",
    "foreground": "#eceff4",
    "dark_foreground": "#667080",
    "light_foreground": "#d8dee9",
    "red": "#bf616a",
    "green": "#a3be8c",
    "yellow": "#ebcb8b",
    "blue": "#81a1c1",
    "cyan": "#88c0d0",
    "magenta": "#b48ead",
}

def load_omarchy_colors(path: Optional[str] = None) -> Dict[str, str]:
    """Loads Omarchy color variables from colors.toml with robust fallback."""
    colors = dict(DEFAULT_DARK_THEME)
    file_path = path or THEME_FILE

    if not os.path.isfile(file_path):
        return colors

    try:
        # Python 3.11+ tomllib
        try:
            import tomllib
            with open(file_path, "rb") as f:
                parsed = tomllib.load(f)
                for k, v in parsed.items():
                    if isinstance(v, str):
                        colors[k] = v
                return colors
        except ImportError:
            pass

        # Robust regex / key-value fallback parser
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    colors[key] = val
    except Exception as e:
        logger.warning("Failed to parse theme file %s: %s", file_path, e)

    return colors


def generate_qss(colors: Dict[str, str]) -> str:
    """Generates a complete Qt Style Sheet bound to Omarchy theme colors."""
    bg = colors.get("background", "#242933")
    dark_bg = colors.get("dark_background", "#1e222a")
    darker_bg = colors.get("darker_background", "#191c23")
    light_bg = colors.get("lighter_background", "#2e3440")
    surface = colors.get("surface", light_bg)
    fg = colors.get("foreground", "#eceff4")
    light_fg = colors.get("light_foreground", "#d8dee9")
    muted = colors.get("muted", "#4c566a")
    border = colors.get("border", colors.get("selection", "#3b4252"))
    accent = colors.get("accent", "#88c0d0")
    red = colors.get("red", "#bf616a")
    green = colors.get("green", "#a3be8c")

    return f"""
QMainWindow, QWidget {{
    background-color: {bg};
    color: {fg};
    font-family: 'Inter', 'Ubuntu', 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {border};
    background: {bg};
    border-radius: 8px;
    margin-top: -1px;
}}
QTabBar::tab {{
    background: {dark_bg};
    color: {light_fg};
    padding: 9px 20px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: bold;
    border: 1px solid {border};
}}
QTabBar::tab:selected {{
    background: {light_bg};
    color: {fg};
    border-bottom: 2px solid {accent};
}}
QTabBar::tab:hover:!selected {{
    background: {surface};
    color: {fg};
}}
QGroupBox {{
    border: 1px solid {border};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 14px;
    font-weight: bold;
    color: {light_fg};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 12px;
}}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
    background-color: {dark_bg};
    color: {fg};
    border: 1px solid {border};
    border-radius: 6px;
    padding: 8px;
    selection-background-color: {accent};
    selection-color: {darker_bg};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QPushButton {{
    background-color: {light_bg};
    color: {fg};
    border: 1px solid {muted};
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: {border};
    border-color: {accent};
}}
QPushButton:pressed {{
    background-color: {muted};
}}
QPushButton#primaryBtn {{
    background-color: {accent};
    color: {darker_bg};
    border: none;
    font-size: 14px;
    padding: 10px 20px;
}}
QPushButton#primaryBtn:hover {{
    background-color: {colors.get('blue', '#81a1c1')};
}}
QPushButton#dangerBtn {{
    background-color: {red};
    color: {fg};
    border: none;
}}
QPushButton#dangerBtn:hover {{
    background-color: {colors.get('orange', '#d08770')};
}}
QPushButton#secondaryBtn {{
    background-color: {dark_bg};
    color: {light_fg};
    border: 1px solid {border};
}}
QPushButton#secondaryBtn:hover {{
    border-color: {accent};
    color: {fg};
}}
QProgressBar {{
    background-color: {dark_bg};
    border: 1px solid {border};
    border-radius: 6px;
    text-align: center;
    color: {fg};
    font-weight: bold;
    height: 22px;
}}
QProgressBar::chunk {{
    background-color: {accent};
    border-radius: 5px;
}}
QScrollBar:vertical {{
    border: none;
    background: {bg};
    width: 8px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {muted};
    min-height: 20px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: {accent};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QTableWidget, QTreeWidget {{
    background-color: {dark_bg};
    alternate-background-color: {bg};
    color: {fg};
    border: 1px solid {border};
    border-radius: 6px;
    gridline-color: {border};
}}
QHeaderView::section {{
    background-color: {light_bg};
    color: {light_fg};
    padding: 5px;
    border: 1px solid {border};
    font-weight: bold;
}}
QFrame#analysisCard {{
    background-color: {surface};
    border: 1px solid {border};
    border-radius: 10px;
    padding: 12px;
}}
QFrame#toastBanner {{
    background-color: {surface};
    border: 1px solid {accent};
    border-radius: 14px;
}}
"""


# ==============================================================================
# 4. Compound Progress Parser & Conversion Helpers
# ==============================================================================

def safe_float(val: Any, default: float = 0.0) -> float:
    """Safely converts any value to float, returning default on None, invalid strings, NaN, or Inf."""
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """Safely converts any value to int, handling float strings, returning default on error."""
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return int(f)
    except (ValueError, TypeError, OverflowError):
        return default


@dataclass
class ParsedProgress:
    percentage: float = 0.0
    current_track: int = 0
    total_tracks: int = 0
    current_title: str = ""
    speed: str = ""
    eta_seconds: int = 0
    status: str = "Downloading"


def parse_progress_payload(payload: Union[str, Dict[str, Any], None]) -> ParsedProgress:
    """Safely parses both structured JSON WebSocket frames and legacy PROGRESS: strings.

    Prevents ValueError and TypeError across all boundary inputs, unwraps nested event
    data envelopes, preserves pipe characters in titles, and enforces bounds clamping.
    """
    if not payload or not isinstance(payload, (dict, str)):
        return ParsedProgress()

    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload

        def _extract(key1: str, key2: Optional[str] = None) -> Any:
            for k in (key1, key2) if key2 else (key1,):
                if k in data and data[k] is not None:
                    return data[k]
                if k in payload and payload[k] is not None:
                    return payload[k]
            return None

        raw_pct = _extract("percentage", "pct")
        pct = safe_float(raw_pct, 0.0)
        current = safe_int(_extract("current_track", "current"), 0)
        total = safe_int(_extract("total_tracks", "total"), 0)
        title = str(_extract("current_title", "track") or "")
        speed = str(_extract("speed") or "")
        eta = safe_int(_extract("eta_seconds", "eta"), 0)
        status = str(_extract("status") or "Downloading")

        pct_clamped = max(0.0, min(100.0, pct))
        total_clamped = max(0, total)
        current_clamped = max(0, min(current, total_clamped)) if total_clamped > 0 else max(0, current)

        return ParsedProgress(
            percentage=pct_clamped,
            current_track=current_clamped,
            total_tracks=total_clamped,
            current_title=title,
            speed=speed,
            eta_seconds=max(0, eta),
            status=status,
        )

    if isinstance(payload, str):
        raw = payload.strip()
        if raw.startswith("PROGRESS:"):
            raw = raw[9:].strip()

        parts = raw.split("|", 3)
        pct = safe_float(parts[0].strip()) if len(parts) > 0 else 0.0
        current = safe_int(parts[1].strip()) if len(parts) > 1 else 0
        total = safe_int(parts[2].strip()) if len(parts) > 2 else 0
        title = parts[3].strip() if len(parts) > 3 else ""

        pct_clamped = max(0.0, min(100.0, pct))
        total_clamped = max(0, total)
        current_clamped = max(0, min(current, total_clamped)) if total_clamped > 0 else max(0, current)

        return ParsedProgress(
            percentage=pct_clamped,
            current_track=current_clamped,
            total_tracks=total_clamped,
            current_title=title,
        )

    return ParsedProgress()


# ==============================================================================
# 5. REST API Client & Thread-Safe Background Workers
# ==============================================================================

class DaemonApiClient:
    """Synchronous HTTP client helper for FastAPI daemon endpoints."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        if HAS_HTTPX:
            with httpx.Client(timeout=timeout) as client:
                if method.upper() == "GET":
                    resp = client.get(url)
                elif method.upper() == "POST":
                    resp = client.post(url, json=data)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                resp.raise_for_status()
                return resp.json()
        else:
            # Python standard library fallback
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            payload = json.dumps(data).encode("utf-8") if data is not None else None
            req = urllib.request.Request(url, data=payload, headers=headers, method=method.upper())
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}

    def get_health(self) -> Dict[str, Any]:
        return self._request("GET", "/health", timeout=4.0)

    def get_config(self) -> Dict[str, Any]:
        return self._request("GET", "/api/config", timeout=4.0)

    def get_users(self) -> List[Dict[str, Any]]:
        res = self._request("GET", "/api/users", timeout=8.0)
        return res.get("users", [])

    def resolve(self, urls: List[str], target_user_id: Optional[str] = None, artist_mode: str = "discography") -> Dict[str, Any]:
        payload = {"urls": urls, "target_user_id": target_user_id, "artist_mode": artist_mode}
        return self._request("POST", "/api/resolve", data=payload, timeout=20.0)

    def ingest(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/api/ingest", data=payload, timeout=10.0)

    def cancel(self, job_id: str) -> Dict[str, Any]:
        return self._request("POST", "/api/cancel", data={"job_id": job_id}, timeout=6.0)


class HealthWorker(QThread):
    health_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, api: DaemonApiClient):
        super().__init__()
        self.api = api

    def run(self):
        try:
            data = self.api.get_health()
            self.health_signal.emit(data)
        except Exception as e:
            self.error_signal.emit(str(e))


class UsersWorker(QThread):
    users_signal = Signal(list)
    error_signal = Signal(str)

    def __init__(self, api: DaemonApiClient):
        super().__init__()
        self.api = api

    def run(self):
        try:
            users = self.api.get_users()
            self.users_signal.emit(users)
        except Exception as e:
            self.error_signal.emit(str(e))


class ResolveWorker(QThread):
    resolved_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, api: DaemonApiClient, urls: List[str], user_id: Optional[str], artist_mode: str = "discography"):
        super().__init__()
        self.api = api
        self.urls = urls
        self.user_id = user_id
        self.artist_mode = artist_mode

    def run(self):
        try:
            res = self.api.resolve(self.urls, self.user_id, self.artist_mode)
            self.resolved_signal.emit(res)
        except Exception as e:
            self.error_signal.emit(str(e))


class IngestWorker(QThread):
    started_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, api: DaemonApiClient, payload: Dict[str, Any]):
        super().__init__()
        self.api = api
        self.payload = payload

    def run(self):
        try:
            res = self.api.ingest(self.payload)
            self.started_signal.emit(res)
        except Exception as e:
            self.error_signal.emit(str(e))


class CancelWorker(QThread):
    cancelled_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, api: DaemonApiClient, job_id: str):
        super().__init__()
        self.api = api
        self.job_id = job_id

    def run(self):
        try:
            res = self.api.cancel(self.job_id)
            self.cancelled_signal.emit(res)
        except Exception as e:
            self.error_signal.emit(str(e))


# ==============================================================================
# 6. WebSocket Streaming Worker (QThread)
# ==============================================================================

class WebSocketClientWorker(QThread):
    connected_signal = Signal()
    disconnected_signal = Signal()
    event_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, ws_url: str, client_id: Optional[str] = None):
        super().__init__()
        self.ws_url = ws_url
        self.client_id = client_id or f"qt-client-{uuid.uuid4().hex[:8]}"
        self._running = True
        self._ws = None

    def run(self):
        if not HAS_WEBSOCKETS_PKG:
            self.error_signal.emit("Python 'websockets' library is required for live event streaming. Run: pip install websockets")
            return

        target_url = f"{self.ws_url}?client_id={self.client_id}"
        logger.info("Connecting WebSocket: %s", target_url)

        backoff = 1.0
        max_backoff = 30.0

        while self._running:
            try:
                with ws_sync_connect(target_url, close_timeout=3) as ws:
                    self._ws = ws
                    backoff = 1.0  # Reset upon successful connection
                    self.connected_signal.emit()
                    logger.info("WebSocket connected successfully")

                    for message in ws:
                        if not self._running:
                            break
                        try:
                            frame = json.loads(message)
                            self.event_signal.emit(frame)
                        except json.JSONDecodeError:
                            pass

                # Normal/clean connection termination by server (code 1000/1001)
                if self._running:
                    self.disconnected_signal.emit()

            except Exception as e:
                if not self._running:
                    break
                logger.debug("WebSocket connection error: %s", e)
                self.error_signal.emit(str(e))
                self.disconnected_signal.emit()

            finally:
                self._ws = None

            # Exponential backoff retry for both clean closes and exceptions
            if self._running:
                sleep_steps = int(backoff * 10)
                for _ in range(sleep_steps):
                    if not self._running:
                        break
                    time.sleep(0.1)
                backoff = min(max_backoff, backoff * 1.5)

        self.disconnected_signal.emit()

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass


# ==============================================================================
# 7. UI Widgets & Components
# ==============================================================================

class DropZoneTextEdit(QtWidgets.QPlainTextEdit):
    """QPlainTextEdit with native Drag & Drop support and URL filtering."""
    urlsDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent):
        urls: List[str] = []
        if event.mimeData().hasUrls():
            for u in event.mimeData().urls():
                s = u.toString()
                if not s.startswith("file://"):
                    urls.append(s)
        if not urls and event.mimeData().hasText():
            text = event.mimeData().text()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("http://") or line.startswith("https://"):
                    urls.append(line)

        if urls:
            event.acceptProposedAction()
            self.urlsDropped.emit(urls)
        else:
            super().dropEvent(event)


class AnalysisCard(QtWidgets.QFrame):
    """Pre-flight diff analysis card displaying total, library, and new counts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("analysisCard")
        self.setVisible(False)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header_layout = QtWidgets.QHBoxLayout()
        self.title_lbl = QtWidgets.QLabel("🔍 Pre-Flight Diff Summary")
        self.title_lbl.setStyleSheet("font-weight: bold; font-size: 13px; color: #88c0d0;")
        self.time_lbl = QtWidgets.QLabel("")
        self.time_lbl.setStyleSheet("color: #8fbcbb; font-size: 11px;")
        header_layout.addWidget(self.title_lbl)
        header_layout.addStretch()
        header_layout.addWidget(self.time_lbl)
        layout.addLayout(header_layout)

        # 3 Counter Badges
        stats_layout = QtWidgets.QHBoxLayout()
        self.total_box = self._create_stat_box("Total Tracks", "0", "#eceff4")
        self.library_box = self._create_stat_box("In Library (Skip)", "0", "#a3be8c")
        self.missing_box = self._create_stat_box("To Download (New)", "0", "#88c0d0")
        stats_layout.addWidget(self.total_box)
        stats_layout.addWidget(self.library_box)
        stats_layout.addWidget(self.missing_box)
        layout.addLayout(stats_layout)

        # Toggleable track table
        self.toggle_btn = QtWidgets.QPushButton("Show Track Breakdown ▼")
        self.toggle_btn.setObjectName("secondaryBtn")
        self.toggle_btn.setFixedHeight(24)
        self.toggle_btn.clicked.connect(self._toggle_breakdown)
        layout.addWidget(self.toggle_btn)

        self.track_table = QtWidgets.QTableWidget()
        self.track_table.setColumnCount(4)
        self.track_table.setHorizontalHeaderLabels(["Status", "Title", "Artist", "Album"])
        self.track_table.horizontalHeader().setStretchLastSection(True)
        self.track_table.setFixedHeight(140)
        self.track_table.setVisible(False)
        layout.addWidget(self.track_table)

    def _create_stat_box(self, label: str, value: str, color_hex: str) -> QtWidgets.QWidget:
        box = QtWidgets.QFrame()
        box.setStyleSheet("background-color: #1e222a; border-radius: 6px; padding: 6px;")
        vbox = QtWidgets.QVBoxLayout(box)
        vbox.setContentsMargins(4, 4, 4, 4)
        vbox.setSpacing(2)

        lbl = QtWidgets.QLabel(label)
        lbl.setStyleSheet("font-size: 11px; color: #adb5c4;")
        lbl.setAlignment(Qt.AlignCenter)

        val = QtWidgets.QLabel(value)
        val.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {color_hex};")
        val.setAlignment(Qt.AlignCenter)

        vbox.addWidget(lbl)
        vbox.addWidget(val)
        box.val_lbl = val  # type: ignore
        return box

    def _toggle_breakdown(self):
        show = not self.track_table.isVisible()
        self.track_table.setVisible(show)
        self.toggle_btn.setText("Hide Track Breakdown ▲" if show else "Show Track Breakdown ▼")

    def update_diff(self, diff_data: Dict[str, Any]):
        total = diff_data.get("total_tracks", 0)
        existing = diff_data.get("existing_tracks", 0)
        missing = diff_data.get("missing_tracks", 0)
        time_ms = diff_data.get("resolve_time_ms", 0.0)

        self.total_box.val_lbl.setText(str(total))  # type: ignore
        self.library_box.val_lbl.setText(str(existing))  # type: ignore
        self.missing_box.val_lbl.setText(str(missing))  # type: ignore
        self.time_lbl.setText(f"Diff resolved in {time_ms:.1f} ms")

        tracks = diff_data.get("tracks", [])
        self.track_table.setRowCount(len(tracks))
        for row, t in enumerate(tracks):
            exists = t.get("exists_locally", False)
            status_item = QtWidgets.QTableWidgetItem("✓ Library" if exists else "⬇ New")
            status_item.setForeground(QtGui.QBrush(QtGui.QColor("#a3be8c" if exists else "#88c0d0")))
            title_item = QtWidgets.QTableWidgetItem(t.get("title", ""))
            artist_item = QtWidgets.QTableWidgetItem(t.get("artist", ""))
            album_item = QtWidgets.QTableWidgetItem(t.get("album", ""))

            self.track_table.setItem(row, 0, status_item)
            self.track_table.setItem(row, 1, title_item)
            self.track_table.setItem(row, 2, artist_item)
            self.track_table.setItem(row, 3, album_item)

        self.setVisible(True)


class ToastBanner(QtWidgets.QFrame):
    """Non-blocking temporary notification banner."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toastBanner")
        self.setFixedHeight(34)
        self.setVisible(False)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        self.lbl = QtWidgets.QLabel("")
        self.lbl.setStyleSheet("font-weight: bold; font-size: 11px; color: #eceff4;")
        layout.addWidget(self.lbl)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)

    def show_message(self, msg: str, duration_ms: int = 3500):
        self.lbl.setText(msg)
        self.setVisible(True)
        self.timer.start(duration_ms)


# ==============================================================================
# 8. Main Window Application Controller
# ==============================================================================

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jellyfin Music Downloader (V2)")
        self.resize(760, 680)
        self.setMinimumSize(700, 600)

        self.config = self.load_config()
        self.api = DaemonApiClient(self.config.get("daemonUrl", "http://127.0.0.1:8095"))
        self.colors = load_omarchy_colors()
        self.setStyleSheet(generate_qss(self.colors))

        self.users: List[Dict[str, Any]] = []
        self.selected_user: Optional[Dict[str, Any]] = None
        self.active_job_id: Optional[str] = None
        self.all_log_lines: List[Tuple[str, bool]] = []
        self._last_clipboard_text = ""

        self.ws_worker: Optional[WebSocketClientWorker] = None
        self.active_worker: Optional[QThread] = None

        self.init_ui()
        self.init_file_watcher()
        self.init_clipboard_listener()
        self.start_websocket()
        self.sync_daemon_state()

    def load_config(self) -> Dict[str, Any]:
        cfg = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception as e:
                logger.warning("Error loading config: %s", e)
        return cfg

    def save_config(self):
        self.config["daemonUrl"] = self.daemon_url_input.text().strip()
        self.config["wsUrl"] = self.ws_url_input.text().strip()
        self.config["jellyfinWebUrl"] = self.jellyfin_url_input.text().strip()
        self.config["musicFolderUrl"] = self.folder_url_input.text().strip()
        self.config["defaultUser"] = self.default_user_input.text().strip()
        self.config["autoClipboardDetect"] = self.clipboard_chk.isChecked()
        self.config["themeSync"] = self.theme_sync_chk.isChecked()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
            self.api = DaemonApiClient(self.config["daemonUrl"])
            self.show_toast("⚙️ Settings saved successfully!")
            self.sync_daemon_state()
            self.restart_websocket()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Save Error", str(e))

    def init_file_watcher(self):
        """Monitors Omarchy colors.toml for live theme hot-reloading."""
        if not self.config.get("themeSync", True):
            return
        self.watcher = QtCore.QFileSystemWatcher(self)
        theme_dir = os.path.dirname(THEME_FILE)
        if os.path.exists(THEME_FILE):
            self.watcher.addPath(THEME_FILE)
        elif os.path.exists(theme_dir):
            self.watcher.addPath(theme_dir)

        self.watcher.fileChanged.connect(self.reload_theme)

    def reload_theme(self):
        logger.info("Omarchy theme change detected; hot-reloading QSS")
        if hasattr(self, "watcher") and os.path.exists(THEME_FILE) and THEME_FILE not in self.watcher.files():
            self.watcher.addPath(THEME_FILE)
        self.colors = load_omarchy_colors()
        self.setStyleSheet(generate_qss(self.colors))
        self.show_toast("🎨 Omarchy theme reloaded dynamically")

    def init_clipboard_listener(self):
        """Monitors clipboard for Spotify/YouTube music links."""
        cb = QtWidgets.QApplication.clipboard()
        cb.dataChanged.connect(self.on_clipboard_changed)

    def on_clipboard_changed(self):
        if not self.config.get("autoClipboardDetect", True):
            return
        cb = QtWidgets.QApplication.clipboard()
        text = cb.text().strip()
        if not text or text == self._last_clipboard_text:
            return
        self._last_clipboard_text = text

        music_domains = ["spotify.com", "music.youtube.com", "youtube.com/watch", "youtube.com/playlist", "youtu.be/", "soundcloud.com", "bandcamp.com"]
        if any(d in text for d in music_domains):
            existing = self.url_edit.toPlainText().strip()
            if text not in existing:
                new_val = (existing + "\n" + text).strip() if existing else text
                self.url_edit.setPlainText(new_val)
                self.update_link_count()
                self.show_toast(f"📋 Auto-loaded streaming link: {text[:45]}...")

    def init_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(16, 14, 16, 14)
        main_layout.setSpacing(10)

        # Header Bar
        header = QtWidgets.QHBoxLayout()
        title_icon = QtWidgets.QLabel("🎵")
        title_icon.setStyleSheet("font-size: 22px;")
        title_text = QtWidgets.QLabel("Jellyfin Music Downloader")
        title_text.setStyleSheet("font-size: 17px; font-weight: bold; color: #88c0d0;")

        self.daemon_status_badge = QtWidgets.QLabel("● Connecting...")
        self.daemon_status_badge.setStyleSheet("color: #ebcb8b; font-size: 11px; font-weight: bold;")

        header.addWidget(title_icon)
        header.addWidget(title_text)
        header.addStretch()
        header.addWidget(self.daemon_status_badge)
        main_layout.addLayout(header)

        # Toast Banner
        self.toast = ToastBanner(self)
        main_layout.addWidget(self.toast)

        # Tabs
        self.tabs = QtWidgets.QTabWidget()
        main_layout.addWidget(self.tabs)

        self.tab_ingest = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_ingest, "📥 Add Music")
        self.setup_ingest_tab()

        self.tab_progress = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_progress, "📊 Task Progress")
        self.setup_progress_tab()

        self.tab_settings = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_settings, "⚙️ Settings")
        self.setup_settings_tab()

    def setup_ingest_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_ingest)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Input Header
        hdr = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("Spotify or YouTube Music URLs (Drag & Drop or Paste):")
        lbl.setStyleSheet("font-weight: bold; color: #d8dee9;")
        self.link_badge = QtWidgets.QLabel("0 links queued")
        self.link_badge.setStyleSheet("color: #88c0d0; font-size: 11px;")
        clear_btn = QtWidgets.QPushButton("✕ Clear")
        clear_btn.setStyleSheet("padding: 3px 8px; font-size: 11px;")
        clear_btn.clicked.connect(self.clear_input)

        hdr.addWidget(lbl)
        hdr.addStretch()
        hdr.addWidget(self.link_badge)
        hdr.addWidget(clear_btn)
        layout.addLayout(hdr)

        # DropZone Text Area
        self.url_edit = DropZoneTextEdit()
        self.url_edit.setPlaceholderText(
            "Paste Spotify playlist, album, artist, or track links here...\n"
            "https://open.spotify.com/playlist/...\n"
            "https://music.youtube.com/watch?v=...\n"
            "(Tip: You can drag and drop web links directly into this box)"
        )
        self.url_edit.textChanged.connect(self.update_link_count)
        self.url_edit.urlsDropped.connect(self.on_urls_dropped)
        self.url_edit.setFixedHeight(105)
        layout.addWidget(self.url_edit)

        # Pre-Flight Diff Analysis Card
        self.analysis_card = AnalysisCard()
        layout.addWidget(self.analysis_card)

        # User Selector Group
        user_group = QtWidgets.QGroupBox("Target Jellyfin Account")
        user_layout = QtWidgets.QVBoxLayout(user_group)
        self.user_btn_layout = QtWidgets.QHBoxLayout()
        self.user_status_label = QtWidgets.QLabel("Fetching accounts from daemon...")
        self.user_status_label.setStyleSheet("color: #8fbcbb;")
        self.user_btn_layout.addWidget(self.user_status_label)
        user_layout.addLayout(self.user_btn_layout)
        layout.addWidget(user_group)

        # Playlist Routing & Bitrate Group
        opts_group = QtWidgets.QGroupBox("Routing & Quality Options")
        opts_layout = QtWidgets.QVBoxLayout(opts_group)

        routing_layout = QtWidgets.QHBoxLayout()
        self.radio_lib_only = QtWidgets.QRadioButton("Library Only")
        self.radio_lib_only.setChecked(True)
        self.radio_existing_pl = QtWidgets.QRadioButton("Add to Existing Playlist")
        self.radio_new_pl = QtWidgets.QRadioButton("Create New Playlist")
        routing_layout.addWidget(self.radio_lib_only)
        routing_layout.addWidget(self.radio_existing_pl)
        routing_layout.addWidget(self.radio_new_pl)
        routing_layout.addStretch()
        opts_layout.addLayout(routing_layout)

        self.pl_controls_layout = QtWidgets.QHBoxLayout()
        self.pl_combo = QtWidgets.QComboBox()
        self.pl_combo.setVisible(False)
        self.pl_new_input = QtWidgets.QLineEdit()
        self.pl_new_input.setPlaceholderText("Enter new playlist name...")
        self.pl_new_input.setVisible(False)
        self.pl_controls_layout.addWidget(self.pl_combo)
        self.pl_controls_layout.addWidget(self.pl_new_input)
        opts_layout.addLayout(self.pl_controls_layout)

        self.radio_lib_only.toggled.connect(self.update_routing_visibility)
        self.radio_existing_pl.toggled.connect(self.update_routing_visibility)
        self.radio_new_pl.toggled.connect(self.update_routing_visibility)

        # Bitrate & Tagging checkboxes
        settings_row = QtWidgets.QHBoxLayout()
        bitrate_lbl = QtWidgets.QLabel("Audio Quality:")
        bitrate_lbl.setStyleSheet("font-weight: bold; color: #d8dee9;")
        self.bitrate_combo = QtWidgets.QComboBox()
        self.bitrate_combo.addItem("Auto (YouTube Music 256kbps Opus / High)", "auto")
        self.bitrate_combo.addItem("320 kbps MP3 (Constant Bitrate)", "320k")
        self.bitrate_combo.addItem("Lossless / FLAC (Studio Quality)", "flac")

        self.chk_lyrics = QtWidgets.QCheckBox("Embed Synced Lyrics (.lrc)")
        self.chk_lyrics.setChecked(self.config.get("embedLyrics", True))
        self.chk_cover = QtWidgets.QCheckBox("Embed Cover Art")
        self.chk_cover.setChecked(self.config.get("embedCover", True))

        settings_row.addWidget(bitrate_lbl)
        settings_row.addWidget(self.bitrate_combo)
        settings_row.addSpacing(16)
        settings_row.addWidget(self.chk_lyrics)
        settings_row.addWidget(self.chk_cover)
        settings_row.addStretch()
        opts_layout.addLayout(settings_row)
        layout.addWidget(opts_group)

        layout.addStretch()

        # Action Buttons
        btn_layout = QtWidgets.QHBoxLayout()
        self.diff_btn = QtWidgets.QPushButton("🔍 Pre-Flight Diff")
        self.diff_btn.setObjectName("secondaryBtn")
        self.diff_btn.clicked.connect(self.run_preflight_diff)

        self.start_btn = QtWidgets.QPushButton("⬇ Start Music Ingestion")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.clicked.connect(self.start_ingestion)

        btn_layout.addWidget(self.diff_btn)
        btn_layout.addWidget(self.start_btn)
        layout.addLayout(btn_layout)

    def setup_progress_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_progress)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Status & Track details
        status_row = QtWidgets.QHBoxLayout()
        self.status_lbl = QtWidgets.QLabel("Status: Idle")
        self.status_lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #88c0d0;")
        self.stage_badge = QtWidgets.QLabel("Stage: Ready")
        self.stage_badge.setStyleSheet("color: #8fbcbb; font-size: 11px;")
        status_row.addWidget(self.status_lbl)
        status_row.addStretch()
        status_row.addWidget(self.stage_badge)
        layout.addLayout(status_row)

        self.track_lbl = QtWidgets.QLabel("No active track")
        self.track_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #eceff4;")
        layout.addWidget(self.track_lbl)

        # Compound Progress Bar & Stats
        self.prog_bar = QtWidgets.QProgressBar()
        self.prog_bar.setValue(0)
        layout.addWidget(self.prog_bar)

        self.stats_lbl = QtWidgets.QLabel("Downloaded: 0 | Skipped: 0 | Failed: 0 | Speed: 0.0 MB/s | ETA: 0s")
        self.stats_lbl.setStyleSheet("color: #d8dee9; font-size: 12px;")
        layout.addWidget(self.stats_lbl)

        # Console Header
        console_hdr = QtWidgets.QHBoxLayout()
        con_lbl = QtWidgets.QLabel("Live Daemon Event & Log Console:")
        con_lbl.setStyleSheet("font-weight: bold; color: #d8dee9;")
        self.filter_chk = QtWidgets.QCheckBox("Errors & Milestones Only")
        self.filter_chk.toggled.connect(self.refilter_logs)
        copy_btn = QtWidgets.QPushButton("📋 Copy Log")
        copy_btn.clicked.connect(self.copy_log)

        self.cancel_btn = QtWidgets.QPushButton("🛑 Cancel Download")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.clicked.connect(self.cancel_ingestion)
        self.cancel_btn.setEnabled(False)

        console_hdr.addWidget(con_lbl)
        console_hdr.addStretch()
        console_hdr.addWidget(self.filter_chk)
        console_hdr.addWidget(copy_btn)
        console_hdr.addWidget(self.cancel_btn)
        layout.addLayout(console_hdr)

        # Console Output
        self.console_edit = QtWidgets.QPlainTextEdit()
        self.console_edit.setReadOnly(True)
        self.console_edit.setStyleSheet(
            f"background-color: {self.colors.get('darker_background', '#191c23')}; "
            f"color: {self.colors.get('green', '#a3be8c')}; "
            f"font-family: monospace; font-size: 11px;"
        )
        layout.addWidget(self.console_edit)

        # Bottom Quick Launch Buttons
        bottom_row = QtWidgets.QHBoxLayout()
        open_jf_btn = QtWidgets.QPushButton("🌐 Open Jellyfin Web")
        open_jf_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(self.config.get("jellyfinWebUrl", "http://127.0.0.1:8096"))))
        open_dir_btn = QtWidgets.QPushButton("📂 Open Music Folder")
        open_dir_btn.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.config.get("musicFolderUrl", "/mnt/media/music"))))
        bottom_row.addWidget(open_jf_btn)
        bottom_row.addWidget(open_dir_btn)
        layout.addLayout(bottom_row)

    def setup_settings_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_settings)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        desc = QtWidgets.QLabel("Configure Daemon REST/WebSocket endpoints and Jellyfin parameters.")
        desc.setStyleSheet("color: #8fbcbb;")
        layout.addWidget(desc)

        form = QtWidgets.QFormLayout()
        form.setSpacing(10)

        self.daemon_url_input = QtWidgets.QLineEdit(self.config.get("daemonUrl", "http://127.0.0.1:8095"))
        self.ws_url_input = QtWidgets.QLineEdit(self.config.get("wsUrl", "ws://127.0.0.1:8095/ws/events"))
        self.jellyfin_url_input = QtWidgets.QLineEdit(self.config.get("jellyfinWebUrl", "http://127.0.0.1:8096"))
        self.folder_url_input = QtWidgets.QLineEdit(self.config.get("musicFolderUrl", "/mnt/media/music"))
        self.default_user_input = QtWidgets.QLineEdit(self.config.get("defaultUser", ""))

        form.addRow("Daemon REST URL:", self.daemon_url_input)
        form.addRow("Daemon WebSocket URL:", self.ws_url_input)
        form.addRow("Jellyfin Web URL:", self.jellyfin_url_input)
        form.addRow("Local Music Folder:", self.folder_url_input)
        form.addRow("Default Jellyfin Account:", self.default_user_input)
        layout.addLayout(form)

        # Options
        self.clipboard_chk = QtWidgets.QCheckBox("Auto-detect music URLs from clipboard (QClipboard.dataChanged)")
        self.clipboard_chk.setChecked(self.config.get("autoClipboardDetect", True))
        layout.addWidget(self.clipboard_chk)

        self.theme_sync_chk = QtWidgets.QCheckBox("Live Omarchy colors.toml sync (auto-reload on theme change)")
        self.theme_sync_chk.setChecked(self.config.get("themeSync", True))
        layout.addWidget(self.theme_sync_chk)

        layout.addStretch()

        btns = QtWidgets.QHBoxLayout()
        test_btn = QtWidgets.QPushButton("🔄 Test & Sync Daemon")
        test_btn.clicked.connect(self.sync_daemon_state)
        save_btn = QtWidgets.QPushButton("💾 Save Settings")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self.save_config)
        btns.addWidget(test_btn)
        btns.addWidget(save_btn)
        layout.addLayout(btns)

    def show_toast(self, msg: str):
        self.toast.show_message(msg)

    def update_link_count(self):
        lines = [l.strip() for l in self.url_edit.toPlainText().splitlines() if l.strip()]
        self.link_badge.setText(f"{len(lines)} links queued")

    def clear_input(self):
        self.url_edit.clear()
        self.update_link_count()
        self.analysis_card.setVisible(False)

    def on_urls_dropped(self, urls: List[str]):
        existing = self.url_edit.toPlainText().strip()
        new_text = "\n".join(urls)
        combined = (existing + "\n" + new_text).strip() if existing else new_text
        self.url_edit.setPlainText(combined)
        self.update_link_count()
        self.show_toast(f"📥 Added {len(urls)} link(s) via Drag & Drop!")

    def update_routing_visibility(self):
        self.pl_combo.setVisible(self.radio_existing_pl.isChecked())
        self.pl_new_input.setVisible(self.radio_new_pl.isChecked())

    # --------------------------------------------------------------------------
    # Daemon Sync & User Management
    # --------------------------------------------------------------------------

    def sync_daemon_state(self):
        self.daemon_status_badge.setText("● Checking health...")
        self.daemon_status_badge.setStyleSheet("color: #ebcb8b;")

        self.health_worker = HealthWorker(self.api)
        self.health_worker.health_signal.connect(self.on_health_ok)
        self.health_worker.error_signal.connect(self.on_health_err)
        self.health_worker.start()

        self.users_worker = UsersWorker(self.api)
        self.users_worker.users_signal.connect(self.on_users_loaded)
        self.users_worker.error_signal.connect(self.on_users_err)
        self.users_worker.start()

    def on_health_ok(self, data: Dict[str, Any]):
        ver = data.get("version", "2.0.0")
        jobs = data.get("active_jobs", 0)
        self.daemon_status_badge.setText(f"● Daemon Online v{ver} ({jobs} active)")
        self.daemon_status_badge.setStyleSheet("color: #a3be8c; font-weight: bold;")

    def on_health_err(self, err: str):
        self.daemon_status_badge.setText("● Daemon Offline")
        self.daemon_status_badge.setStyleSheet("color: #bf616a; font-weight: bold;")
        logger.warning("Daemon health check failed: %s", err)

    def on_users_loaded(self, users: List[Dict[str, Any]]):
        self.users = users
        self.render_user_buttons()

    def on_users_err(self, err: str):
        self.user_status_label.setText("⚠️ Failed to load users from daemon")

    def render_user_buttons(self):
        while self.user_btn_layout.count():
            item = self.user_btn_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if not self.users:
            lbl = QtWidgets.QLabel("No accounts found.")
            self.user_btn_layout.addWidget(lbl)
            return

        def_name = (self.config.get("defaultUser") or "").strip().lower()
        selected_idx = 0
        for i, u in enumerate(self.users):
            if def_name and u.get("name", "").lower() == def_name:
                selected_idx = i
                break

        self.user_buttons = []
        for i, user in enumerate(self.users):
            is_shared = user.get("id") == "00000000000000000000000000000000"
            icon = "👥" if is_shared else "👤"
            name = user.get("name", "User")
            is_def = bool(def_name and name.lower() == def_name)
            btn = QtWidgets.QPushButton(f"{icon} {name}{' (Default)' if is_def else ''}")
            btn.setCheckable(True)
            if i == selected_idx:
                btn.setChecked(True)
                self.selected_user = user
                self.populate_playlists(user)

            btn.clicked.connect(lambda checked, u=user, b=btn: self.on_user_selected(u, b))
            self.user_btn_layout.addWidget(btn)
            self.user_buttons.append(btn)

    def on_user_selected(self, user: Dict[str, Any], clicked_btn: QtWidgets.QPushButton):
        self.selected_user = user
        for b in self.user_buttons:
            b.setChecked(b == clicked_btn)
        self.populate_playlists(user)

    def populate_playlists(self, user: Dict[str, Any]):
        self.pl_combo.clear()
        playlists = user.get("playlists", [])
        if playlists:
            for pl in playlists:
                name = pl.get("name") if isinstance(pl, dict) else str(pl)
                self.pl_combo.addItem(name)
            self.radio_existing_pl.setEnabled(True)
        else:
            self.radio_existing_pl.setEnabled(False)
            if self.radio_existing_pl.isChecked():
                self.radio_lib_only.setChecked(True)

    # --------------------------------------------------------------------------
    # Pre-Flight Diff & Ingestion Execution
    # --------------------------------------------------------------------------

    def get_input_urls(self) -> List[str]:
        return [l.strip() for l in self.url_edit.toPlainText().splitlines() if l.strip()]

    def run_preflight_diff(self):
        urls = self.get_input_urls()
        if not urls:
            QtWidgets.QMessageBox.warning(self, "No URLs", "Please enter at least one streaming link.")
            return

        user_id = self.selected_user.get("id") if self.selected_user else None
        self.diff_btn.setEnabled(False)
        self.diff_btn.setText("🔍 Resolving...")

        self.resolve_worker = ResolveWorker(self.api, urls, user_id)
        self.resolve_worker.resolved_signal.connect(self.on_diff_resolved)
        self.resolve_worker.error_signal.connect(self.on_diff_error)
        self.resolve_worker.start()

    def on_diff_resolved(self, data: Dict[str, Any]):
        self.diff_btn.setEnabled(True)
        self.diff_btn.setText("🔍 Pre-Flight Diff")
        self.analysis_card.update_diff(data)
        self.show_toast("✓ Pre-flight diff analysis complete")

    def on_diff_error(self, err: str):
        self.diff_btn.setEnabled(True)
        self.diff_btn.setText("🔍 Pre-Flight Diff")
        QtWidgets.QMessageBox.critical(self, "Diff Error", f"Failed to resolve playlist: {err}")

    def start_ingestion(self):
        urls = self.get_input_urls()
        if not urls:
            QtWidgets.QMessageBox.warning(self, "No URLs", "Please enter at least one Spotify or YouTube Music URL.")
            return

        is_playlist_mode = any("playlist" in u.lower() or "list=" in u.lower() for u in urls)
        wants_playlist = self.radio_existing_pl.isChecked() or self.radio_new_pl.isChecked() or (is_playlist_mode and not self.radio_lib_only.isChecked())

        if wants_playlist and not self.selected_user:
            QtWidgets.QMessageBox.warning(self, "No User", "Please select a target Jellyfin account for playlist creation.")
            return

        if self.radio_existing_pl.isChecked():
            playlist_name = self.pl_combo.currentText()
        elif self.radio_new_pl.isChecked():
            playlist_name = self.pl_new_input.text().strip()
            if not playlist_name:
                QtWidgets.QMessageBox.warning(self, "Missing Name", "Please enter a name for the new playlist.")
                return
        elif is_playlist_mode and not self.radio_lib_only.isChecked():
            playlist_name = "AUTO"
        else:
            playlist_name = "__NO_PLAYLIST__"

        user_id = self.selected_user.get("id") if (wants_playlist and self.selected_user) else None

        payload = {
            "urls": urls,
            "user_id": user_id,
            "playlist_name": playlist_name,
            "bitrate": self.bitrate_combo.currentData(),
            "embed_lyrics": self.chk_lyrics.isChecked(),
            "embed_cover": self.chk_cover.isChecked(),
        }

        self.tabs.setCurrentWidget(self.tab_progress)
        self.status_lbl.setText("Status: Enqueuing job...")
        self.prog_bar.setValue(0)
        self.console_edit.clear()
        self.all_log_lines.clear()
        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.ingest_worker = IngestWorker(self.api, payload)
        self.ingest_worker.started_signal.connect(self.on_ingest_started)
        self.ingest_worker.error_signal.connect(self.on_ingest_error)
        self.ingest_worker.start()

    def on_ingest_started(self, res: Dict[str, Any]):
        self.active_job_id = res.get("job_id")
        self.status_lbl.setText(f"Status: Job {self.active_job_id} running")
        self.show_toast(f"🚀 Ingestion job queued: {self.active_job_id}")

    def on_ingest_error(self, err: str):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.status_lbl.setText("Status: Failed to start job")
        QtWidgets.QMessageBox.critical(self, "Ingestion Error", str(err))

    def cancel_ingestion(self):
        if not self.active_job_id:
            return
        self.status_lbl.setText("Status: Cancelling job...")
        self.cancel_btn.setEnabled(False)

        self.cancel_worker = CancelWorker(self.api, self.active_job_id)
        self.cancel_worker.cancelled_signal.connect(self.on_cancelled)
        self.cancel_worker.error_signal.connect(self.on_cancel_error)
        self.cancel_worker.start()

    def on_cancelled(self, res: Dict[str, Any]):
        self.status_lbl.setText("Status: Cancelled by user")
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.show_toast("🛑 Ingestion cancelled cleanly")

    def on_cancel_error(self, err: str):
        self.cancel_btn.setEnabled(True)
        QtWidgets.QMessageBox.critical(self, "Cancel Error", f"Failed to cancel job: {err}")

    # --------------------------------------------------------------------------
    # WebSocket Event Stream Handling
    # --------------------------------------------------------------------------

    def start_websocket(self):
        ws_url = self.config.get("wsUrl") or (
            self.config.get("daemonUrl", "http://127.0.0.1:8095")
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .rstrip("/")
            + "/ws/events"
        )
        self.ws_worker = WebSocketClientWorker(ws_url)
        self.ws_worker.connected_signal.connect(self.on_ws_connected)
        self.ws_worker.disconnected_signal.connect(self.on_ws_disconnected)
        self.ws_worker.event_signal.connect(self.on_ws_event)
        self.ws_worker.error_signal.connect(self.on_ws_error)
        self.ws_worker.start()

    def restart_websocket(self):
        if self.ws_worker:
            self.ws_worker.stop()
            self.ws_worker.wait(1000)
        self.start_websocket()

    def on_ws_connected(self):
        self.append_log("[WS] Connected to live daemon event stream", is_important=True)

    def on_ws_disconnected(self):
        self.append_log("[WS] Disconnected from daemon event stream", is_important=False)

    def on_ws_error(self, err: str):
        pass

    def on_ws_event(self, frame: Dict[str, Any]):
        event_type = frame.get("event")
        job_id = frame.get("job_id")

        if event_type == "job_started":
            total = frame.get("total_tracks", 0)
            missing = frame.get("to_download", 0)
            self.status_lbl.setText(f"Status: Downloading {missing} tracks (Total: {total})...")
            self.append_log(f"▶ Job started: {total} total tracks, {missing} missing tracks to download", True)

        elif event_type == "stage_transition":
            stage_name = frame.get("stage_name", "")
            desc = frame.get("description", "")
            self.stage_badge.setText(f"Stage: {stage_name}")
            self.append_log(f"🔄 Stage: {stage_name} - {desc}", True)

        elif event_type == "progress":
            prog = parse_progress_payload(frame)
            self.prog_bar.setValue(int(prog.percentage))
            if prog.current_title:
                self.track_lbl.setText(prog.current_title)
            self.stats_lbl.setText(f"Track: {prog.current_track}/{prog.total_tracks} | Speed: {prog.speed} | ETA: {prog.eta_seconds}s")

        elif event_type == "track_completed":
            track = frame.get("track", "")
            artist = frame.get("artist", "")
            synced = "🎵 Synced" if frame.get("lyrics_synced") else ""
            self.append_log(f"✔ Completed: {artist} - {track} {synced}", True)

        elif event_type == "track_failed":
            track = frame.get("track", "")
            err = frame.get("error", "Unknown error")
            self.append_log(f"✖ Failed: {track} ({err})", True)

        elif event_type == "log":
            msg = frame.get("message", "")
            lvl = frame.get("level", "INFO")
            is_imp = lvl in ("WARNING", "ERROR", "CRITICAL")
            self.append_log(f"[{lvl}] {msg}", is_imp)

        elif event_type == "job_completed":
            self.start_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.prog_bar.setValue(100)
            dl = frame.get("downloaded", 0)
            skip = frame.get("skipped", 0)
            failed = frame.get("failed", 0)
            dur = frame.get("duration_seconds", 0.0)
            msg = f"🎉 Job finished in {dur}s! Downloaded: {dl}, Skipped: {skip}, Failed: {failed}"
            self.status_lbl.setText("Status: Complete! 🎉")
            self.append_log(msg, True)
            self.show_toast(msg)

        elif event_type == "job_cancelled":
            self.start_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.status_lbl.setText("Status: Cancelled")
            self.append_log("🛑 Job was cancelled by user", True)

    def append_log(self, text: str, is_important: bool = False):
        self.all_log_lines.append((text, is_important))
        if not self.filter_chk.isChecked() or is_important:
            self.console_edit.appendPlainText(text)

    def refilter_logs(self):
        self.console_edit.clear()
        for text, is_imp in self.all_log_lines:
            if not self.filter_chk.isChecked() or is_imp:
                self.console_edit.appendPlainText(text)

    def copy_log(self):
        text = self.console_edit.toPlainText()
        if text:
            QtWidgets.QApplication.clipboard().setText(text)
            self.show_toast("📋 Console logs copied to clipboard!")

    def closeEvent(self, event: QtGui.QCloseEvent):
        if self.ws_worker:
            self.ws_worker.stop()
            self.ws_worker.wait(500)
        event.accept()


# ==============================================================================
# 9. Application Entry Point
# ==============================================================================

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Jellyfin Music Downloader")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_() if hasattr(app, "exec_") else app.exec())


if __name__ == "__main__":
    main()
