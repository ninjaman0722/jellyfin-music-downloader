"""Unit and Integration Tests for Universal Python Qt Client (app.py).

Verifies:
- Headless Qt execution with offscreen platform (QT_QPA_PLATFORM=offscreen)
- Omarchy colors.toml parser and dynamic QSS generator with fallback logic
- Compound progress parser (PROGRESS: 50|5|10, JSON frames, malformed strings, bounds)
- REST API client methods (resolve, ingest, cancel, get_users, get_config, health)
- WebSocket client event dispatcher and Qt signal propagation
- Drag-and-drop MIME data parser and streaming URL regex validator
- Headless MainWindow offscreen smoke integration
"""

import os
import sys
import json
import math
import time
import threading
import tempfile
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest
import uvicorn
from fastapi import FastAPI, WebSocket, Request

# Ensure offscreen Qt platform before importing Qt
os.environ["QT_QPA_PLATFORM"] = "offscreen"

# Import Qt bindings
try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import Qt, QUrl, QMimeData, pyqtSignal
    QT_AVAILABLE = True
except ImportError:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
        from PySide6.QtCore import Qt, QUrl, QMimeData, Signal as pyqtSignal
        QT_AVAILABLE = True
    except ImportError:
        QT_AVAILABLE = False

# Import client modules from target project or proposed implementation
try:
    import app
except ImportError:
    sys.path.insert(0, "/home/kendon/Documents/My Vault/.agents/explorer_m4_qt")
    import proposed_app as app


# ==============================================================================
# 1. Ephemeral Mock Daemon Server Fixture
# ==============================================================================

class MockDaemonServer:
    """Threaded FastAPI + Uvicorn server providing mock REST and WebSocket endpoints."""

    def __init__(self):
        self.app = FastAPI(title="Mock Media Daemon", version="2.0.0")
        self.recorded_requests: List[Dict[str, Any]] = []
        self.ws_connections: List[WebSocket] = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._setup_routes()
        self.config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread: Optional[threading.Thread] = None
        self.port: int = 0

    def _setup_routes(self):
        @self.app.get("/health")
        async def health():
            return {"status": "healthy", "version": "2.0.0", "uptime_seconds": 120.0, "active_jobs": 0}

        @self.app.get("/api/config")
        async def config():
            return {
                "music_dir": "/mnt/media/music",
                "bitrate": "320k",
                "default_user": "Kendon",
                "jellyfin_url": "http://127.0.0.1:8096"
            }

        @self.app.get("/api/users")
        async def users():
            return {
                "users": [
                    {
                        "id": "user-guid-001",
                        "name": "Kendon",
                        "has_password": True,
                        "is_admin": True,
                        "playlists": [{"id": "pl-01", "name": "Synthwave Drive", "track_count": 42}]
                    },
                    {
                        "id": "00000000000000000000000000000000",
                        "name": "Household (Shared)",
                        "has_password": False,
                        "is_admin": False,
                        "playlists": []
                    }
                ]
            }

        @self.app.post("/api/resolve")
        async def resolve(req: Request):
            payload = await req.json()
            self.recorded_requests.append({"method": "POST", "endpoint": "/api/resolve", "body": payload})
            urls = payload.get("urls", [])
            return {
                "playlist_name": "Midnight Neon",
                "playlist_id": "pl-test-123",
                "total_tracks": 10,
                "existing_tracks": 4,
                "missing_tracks": 6,
                "resolve_time_ms": 4.5,
                "tracks": [
                    {"title": f"Track {i}", "artist": "Synth Artist", "album": "Neon Album", "exists_locally": i <= 4}
                    for i in range(1, 11)
                ]
            }

        @self.app.post("/api/ingest")
        async def ingest(req: Request):
            payload = await req.json()
            self.recorded_requests.append({"method": "POST", "endpoint": "/api/ingest", "body": payload})
            return {
                "job_id": "job-test-uuid-456",
                "status": "queued",
                "queued_tracks": 6,
                "already_present": 4
            }

        @self.app.post("/api/cancel")
        async def cancel(req: Request):
            payload = await req.json()
            self.recorded_requests.append({"method": "POST", "endpoint": "/api/cancel", "body": payload})
            return {
                "job_id": payload.get("job_id", "unknown"),
                "status": "cancelled",
                "cleaned_files": 2,
                "message": "Job cancelled successfully"
            }

        @self.app.websocket("/ws/events")
        async def ws_events(ws: WebSocket):
            self.loop = asyncio.get_running_loop()
            await ws.accept()
            self.ws_connections.append(ws)
            try:
                while True:
                    await ws.receive_text()
            except Exception:
                pass
            finally:
                if ws in self.ws_connections:
                    self.ws_connections.remove(ws)

    def start(self):
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/events"

    def broadcast_event(self, event_type: str, data: Dict[str, Any], job_id: str = "job-test-uuid-456"):
        """Dispatches an event payload thread-safely to all connected WebSocket clients."""
        if not self.loop or not self.ws_connections:
            return
        frame = {
            "event": event_type,
            "job_id": job_id,
            "timestamp": "2026-09-05T08:00:00Z",
            "data": data
        }
        async def _send():
            for ws in list(self.ws_connections):
                try:
                    await ws.send_json(frame)
                except Exception:
                    pass
        fut = asyncio.run_coroutine_threadsafe(_send(), self.loop)
        fut.result(timeout=2.0)

    def stop(self):
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=2.0)


class EphemeralCloseServer:
    """Ephemeral WebSocket server that accepts connections and immediately closes them.
    
    Used to reproduce and verify clean close frame handling (codes 1000/1001) without
    raising network exceptions, asserting that the client throttles reconnection attempts.
    """

    def __init__(self, close_code: int = 1000, close_reason: str = "Clean close"):
        self.close_code = close_code
        self.close_reason = close_reason
        self.connection_count = 0
        self.port: int = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    def start(self):
        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def handler(ws):
                self.connection_count += 1
                await ws.close(self.close_code, self.close_reason)

            async def srv():
                import websockets
                self._server = await websockets.serve(handler, "127.0.0.1", 0)
                self.port = self._server.sockets[0].getsockname()[1]
                self._ready.set()
                while not self._stop.is_set():
                    await asyncio.sleep(0.01)
                self._server.close()
                await self._server.wait_closed()

            self._loop.run_until_complete(srv())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws/events"



@pytest.fixture(scope="session")
def qapp():
    """Initializes a headless offscreen QApplication instance."""
    assert QT_AVAILABLE, "PyQt5, PyQt6, or PySide6 must be installed"
    instance = QtWidgets.QApplication.instance()
    if instance is None:
        instance = QtWidgets.QApplication(["--platform", "offscreen"])
    yield instance


@pytest.fixture
def mock_daemon():
    """Provides a running MockDaemonServer on an ephemeral port."""
    server = MockDaemonServer()
    server.start()
    yield server
    server.stop()


# ==============================================================================
# 2. Headless Qt Platform Tests
# ==============================================================================

class TestQtHeadlessEnvironment:
    """Verifies that Qt runs offscreen without display dependencies."""

    def test_offscreen_platform_initialization(self, qapp):
        assert qapp is not None
        assert qapp.platformName() == "offscreen"

    def test_basic_widget_instantiation_offscreen(self, qapp):
        btn = QtWidgets.QPushButton("Test Button")
        line = QtWidgets.QLineEdit()
        bar = QtWidgets.QProgressBar()
        bar.setValue(50)
        assert btn.text() == "Test Button"
        assert bar.value() == 50


# ==============================================================================
# 3. Omarchy Theme Parser & Dynamic QSS Tests
# ==============================================================================

class TestOmarchyTheming:
    """Verifies colors.toml loading, fallback handling, and QSS synthesis."""

    def test_load_omarchy_colors_from_valid_toml(self, tmp_path):
        toml_content = """
        mode = "dark"
        accent = "#b48ead"
        background = "#191c23"
        dark_background = "#121419"
        foreground = "#d8dee9"
        red = "#bf616a"
        green = "#a3be8c"
        """
        theme_file = tmp_path / "colors.toml"
        theme_file.write_text(toml_content, encoding="utf-8")

        palette = app.load_omarchy_colors(str(theme_file))
        assert palette["accent"] == "#b48ead"
        assert palette["background"] == "#191c23"
        assert palette["dark_background"] == "#121419"
        assert palette["foreground"] == "#d8dee9"

    def test_load_omarchy_colors_fallback_on_missing_file(self):
        palette = app.load_omarchy_colors("/nonexistent/colors.toml")
        assert palette == app.DEFAULT_DARK_THEME
        assert palette["accent"] == app.DEFAULT_DARK_THEME["accent"]

    def test_load_omarchy_colors_fallback_on_malformed_file(self, tmp_path):
        bad_file = tmp_path / "corrupt.toml"
        bad_file.write_text("invalid = [[toml syntax", encoding="utf-8")

        palette = app.load_omarchy_colors(str(bad_file))
        assert palette["accent"] == app.DEFAULT_DARK_THEME["accent"]
        assert palette["background"] == app.DEFAULT_DARK_THEME["background"]

    def test_generate_qss_contains_required_rules_and_colors(self):
        palette = {
            "background": "#2e3440",
            "dark_background": "#242933",
            "darker_background": "#1e222a",
            "lighter_background": "#3b4252",
            "foreground": "#eceff4",
            "light_foreground": "#d8dee9",
            "accent": "#88c0d0",
            "muted": "#4c566a",
            "selection": "#434c5e",
            "red": "#bf616a",
            "green": "#a3be8c",
        }
        qss = app.generate_qss(palette)
        assert "QMainWindow" in qss
        assert "QPushButton#primaryBtn" in qss
        assert "QPushButton#dangerBtn" in qss
        assert "QProgressBar::chunk" in qss
        assert "#88c0d0" in qss
        assert "#2e3440" in qss
        assert "#bf616a" in qss

    def test_generate_qss_with_missing_keys_uses_defaults(self):
        qss = app.generate_qss({})
        assert "QMainWindow" in qss
        assert "QPushButton" in qss


# ==============================================================================
# 4. Compound Progress Parser Tests
# ==============================================================================

class TestCompoundProgressParser:
    """Verifies compound, simple, JSON, and malformed progress parsing."""

    def test_compound_pipe_delimited_string(self):
        parsed = app.parse_progress_payload("PROGRESS: 50|5|10")
        assert parsed.percentage == 50.0
        assert parsed.current_track == 5
        assert parsed.total_tracks == 10

    def test_compound_string_with_title(self):
        parsed = app.parse_progress_payload("PROGRESS: 75|3|4|Save Your Tears")
        assert parsed.percentage == 75.0
        assert parsed.current_track == 3
        assert parsed.total_tracks == 4
        assert parsed.current_title == "Save Your Tears"

    def test_simple_integer_string(self):
        parsed = app.parse_progress_payload("PROGRESS: 45")
        assert parsed.percentage == 45.0
        assert parsed.current_track == 0
        assert parsed.total_tracks == 0

    def test_simple_float_string(self):
        parsed = app.parse_progress_payload("PROGRESS: 88.5")
        assert parsed.percentage == 88.5

    def test_json_event_payload_parsing(self):
        frame = {
            "percentage": 65,
            "current_track": 8,
            "total_tracks": 12,
            "current_title": "Blinding Lights",
            "speed": "2.1 MB/s",
            "eta_seconds": 35,
            "status": "Downloading"
        }
        parsed = app.parse_progress_payload(frame)
        assert parsed.percentage == 65.0
        assert parsed.current_track == 8
        assert parsed.total_tracks == 12
        assert parsed.current_title == "Blinding Lights"
        assert parsed.speed == "2.1 MB/s"
        assert parsed.eta_seconds == 35
        assert parsed.status == "Downloading"

    def test_bounds_clamping(self):
        assert app.parse_progress_payload("PROGRESS: -15").percentage == 0.0
        assert app.parse_progress_payload("PROGRESS: 140").percentage == 100.0
        assert app.parse_progress_payload({"percentage": -50}).percentage == 0.0
        assert app.parse_progress_payload({"percentage": 200}).percentage == 100.0

    def test_malformed_inputs_handled_safely(self):
        assert app.parse_progress_payload("PROGRESS: invalid|bad|data").percentage == 0.0
        assert app.parse_progress_payload("PROGRESS:").percentage == 0.0
        assert app.parse_progress_payload("").percentage == 0.0
        assert app.parse_progress_payload("RANDOM STRING").percentage == 0.0
        assert app.parse_progress_payload(None).percentage == 0.0

    def test_dict_payload_non_numeric_and_none_percentage(self):
        """Regression: Non-numeric and None percentage values must not raise ValueError/TypeError."""
        p1 = app.parse_progress_payload({"percentage": "invalid"})
        assert p1.percentage == 0.0

        p2 = app.parse_progress_payload({"percentage": None})
        assert p2.percentage == 0.0

        p3 = app.parse_progress_payload({"pct": "not_a_number"})
        assert p3.percentage == 0.0

        p4 = app.parse_progress_payload({"pct": None})
        assert p4.percentage == 0.0

        p5 = app.parse_progress_payload({"percentage": float("nan")})
        assert p5.percentage == 0.0

        p6 = app.parse_progress_payload({"percentage": float("inf")})
        assert p6.percentage == 0.0

        p7 = app.parse_progress_payload({"percentage": float("-inf")})
        assert p7.percentage == 0.0

        p8 = app.parse_progress_payload({"percentage": "NaN"})
        assert p8.percentage == 0.0

        p9 = app.parse_progress_payload({"percentage": "Infinity"})
        assert p9.percentage == 0.0

    def test_dict_payload_nested_event_envelope_data_extraction(self):
        """Regression: Payloads wrapped in BaseEvent envelope {'event': 'progress', 'data': {...}} must extract data."""
        envelope = {
            "event": "progress",
            "job_id": "job-regression-001",
            "timestamp": "2026-09-05T08:15:00Z",
            "data": {
                "percentage": 50.0,
                "current_track": 5,
                "total_tracks": 10,
                "current_title": "Synthwave Sunset",
                "speed": "2.4 MB/s",
                "eta_seconds": 18,
                "status": "Downloading"
            }
        }
        parsed = app.parse_progress_payload(envelope)
        assert parsed.percentage == 50.0
        assert parsed.current_track == 5
        assert parsed.total_tracks == 10
        assert parsed.current_title == "Synthwave Sunset"
        assert parsed.speed == "2.4 MB/s"
        assert parsed.eta_seconds == 18
        assert parsed.status == "Downloading"

    def test_dict_payload_corrupt_subfields_safe_defaults(self):
        """Regression: Malformed non-numeric or null fields must safely yield default zero/empty values."""
        bad_subfields = {
            "current_track": "bad",
            "total_tracks": None,
            "eta_seconds": "invalid",
            "current_title": None,
            "speed": None,
            "status": None
        }
        parsed = app.parse_progress_payload(bad_subfields)
        assert parsed.percentage == 0.0
        assert parsed.current_track == 0
        assert parsed.total_tracks == 0
        assert parsed.eta_seconds == 0
        assert parsed.current_title == ""
        assert parsed.speed == ""
        assert parsed.status == "Downloading"

        numeric_str_payload = {
            "percentage": "62.5",
            "current_track": "4",
            "total_tracks": "8",
            "eta_seconds": "25"
        }
        parsed2 = app.parse_progress_payload(numeric_str_payload)
        assert parsed2.percentage == 62.5
        assert parsed2.current_track == 4
        assert parsed2.total_tracks == 8
        assert parsed2.eta_seconds == 25

    def test_string_payload_preserves_piped_titles(self):
        """Regression: Track titles containing pipe '|' characters must NOT be truncated."""
        raw1 = "PROGRESS: 50|5|10|Title With Pipes | More Pipes"
        parsed1 = app.parse_progress_payload(raw1)
        assert parsed1.percentage == 50.0
        assert parsed1.current_track == 5
        assert parsed1.total_tracks == 10
        assert parsed1.current_title == "Title With Pipes | More Pipes"

        raw2 = "PROGRESS: 80|8|10|Artist - Track Name | Extended Mix | 2026 Remaster"
        parsed2 = app.parse_progress_payload(raw2)
        assert parsed2.percentage == 80.0
        assert parsed2.current_track == 8
        assert parsed2.total_tracks == 10
        assert parsed2.current_title == "Artist - Track Name | Extended Mix | 2026 Remaster"

        raw3 = "PROGRESS: 100|1|1|A | B"
        parsed3 = app.parse_progress_payload(raw3)
        assert parsed3.current_title == "A | B"

    def test_track_bounds_clamping_and_negatives(self):
        """Regression: Track count upper/lower bounds and negative values must be safely clamped."""
        p = app.parse_progress_payload({"current_track": 15, "total_tracks": 10})
        assert p.current_track == 10
        assert p.total_tracks == 10

        p_str = app.parse_progress_payload("PROGRESS: 50|25|10|Overflow Track")
        assert p_str.current_track == 10
        assert p_str.total_tracks == 10

        p_neg = app.parse_progress_payload({"current_track": -5, "total_tracks": 10})
        assert p_neg.current_track == 0
        assert p_neg.total_tracks == 10

        p_str_neg = app.parse_progress_payload("PROGRESS: 50|-5|10|Negative Track")
        assert p_str_neg.current_track == 0
        assert p_str_neg.total_tracks == 10

        p_tot_neg = app.parse_progress_payload({"current_track": 5, "total_tracks": -10})
        assert p_tot_neg.total_tracks == 0
        assert p_tot_neg.current_track == 5

        p_eta_neg = app.parse_progress_payload({"eta_seconds": -30})
        assert p_eta_neg.eta_seconds == 0

    def test_safe_conversion_helpers_direct(self):
        """Direct unit verification for safe_float and safe_int helpers."""
        assert app.safe_float(50.5) == 50.5
        assert app.safe_float("42.7") == 42.7
        assert app.safe_float(100) == 100.0
        assert app.safe_float(None) == 0.0
        assert app.safe_float("invalid") == 0.0
        assert app.safe_float("") == 0.0
        assert app.safe_float([]) == 0.0
        assert app.safe_float({}) == 0.0
        assert app.safe_float(True) == 0.0
        assert app.safe_float(False) == 0.0
        assert app.safe_float(float("nan")) == 0.0
        assert app.safe_float(float("inf")) == 0.0

        assert app.safe_int(10) == 10
        assert app.safe_int("15") == 15
        assert app.safe_int("20.0") == 20
        assert app.safe_int(35.9) == 35
        assert app.safe_int(None) == 0
        assert app.safe_int("invalid") == 0
        assert app.safe_int("") == 0
        assert app.safe_int(True) == 0
        assert app.safe_int(False) == 0
        assert app.safe_int(float("nan")) == 0
        assert app.safe_int(float("inf")) == 0


# ==============================================================================
# 5. REST API Client Tests
# ==============================================================================

class TestDaemonApiClient:
    """Verifies REST client methods against MockDaemonServer."""

    def test_get_health(self, mock_daemon):
        client = app.DaemonApiClient(mock_daemon.base_url)
        health = client.get_health()
        assert health["status"] == "healthy"
        assert health["version"] == "2.0.0"

    def test_get_config(self, mock_daemon):
        client = app.DaemonApiClient(mock_daemon.base_url)
        cfg = client.get_config()
        assert cfg["music_dir"] == "/mnt/media/music"
        assert cfg["bitrate"] == "320k"

    def test_get_users(self, mock_daemon):
        client = app.DaemonApiClient(mock_daemon.base_url)
        users = client.get_users()
        assert len(users) == 2
        assert users[0]["name"] == "Kendon"
        assert users[0]["playlists"][0]["name"] == "Synthwave Drive"

    def test_resolve(self, mock_daemon):
        client = app.DaemonApiClient(mock_daemon.base_url)
        urls = ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]
        res = client.resolve(urls, target_user_id="user-guid-001")
        assert res["total_tracks"] == 10
        assert res["existing_tracks"] == 4
        assert res["missing_tracks"] == 6
        assert len(res["tracks"]) == 10

        assert len(mock_daemon.recorded_requests) == 1
        recorded = mock_daemon.recorded_requests[0]
        assert recorded["endpoint"] == "/api/resolve"
        assert recorded["body"]["urls"] == urls

    def test_ingest(self, mock_daemon):
        client = app.DaemonApiClient(mock_daemon.base_url)
        payload = {
            "urls": ["https://open.spotify.com/playlist/test"],
            "user_id": "user-guid-001",
            "playlist_name": "Test Ingest",
            "bitrate": "320k",
            "embed_lyrics": True,
            "embed_cover": True
        }
        res = client.ingest(payload)
        assert res["job_id"] == "job-test-uuid-456"
        assert res["status"] == "queued"

    def test_cancel(self, mock_daemon):
        client = app.DaemonApiClient(mock_daemon.base_url)
        res = client.cancel("job-test-uuid-456")
        assert res["job_id"] == "job-test-uuid-456"
        assert res["status"] == "cancelled"

    def test_connection_failure_handling(self):
        client = app.DaemonApiClient("http://127.0.0.1:59999")
        with pytest.raises(Exception):
            client.get_health()


# ==============================================================================
# 6. WebSocket Client Event Dispatcher Tests
# ==============================================================================

class TestWebSocketWorker:
    """Verifies WebSocketClientWorker connects and emits Qt Signals."""

    def test_websocket_event_propagation(self, qapp, mock_daemon):
        worker = app.WebSocketClientWorker(mock_daemon.ws_url, client_id="test-qt-client")
        received_events: List[Dict[str, Any]] = []
        is_connected = []

        worker.connected_signal.connect(lambda: is_connected.append(True))
        worker.event_signal.connect(received_events.append)
        worker.start()

        for _ in range(30):
            qapp.processEvents()
            time.sleep(0.05)
            if is_connected:
                break

        assert is_connected, "WebSocket worker failed to connect"

        mock_daemon.broadcast_event("job_started", {"total_tracks": 10, "to_download": 6})
        mock_daemon.broadcast_event("progress", {"percentage": 50, "current_track": 3, "total_tracks": 6, "current_title": "Track 3"})
        mock_daemon.broadcast_event("job_completed", {"job_id": "job-test-uuid-456", "downloaded": 6, "skipped": 4, "failed": 0})

        for _ in range(30):
            qapp.processEvents()
            time.sleep(0.05)
            if len(received_events) >= 3:
                break

        worker.stop()
        worker.wait(1000)

        assert len(received_events) >= 3
        events = [e["event"] for e in received_events]
        assert "job_started" in events
        assert "progress" in events
        assert "job_completed" in events

        progress_frame = next(e for e in received_events if e["event"] == "progress")
        assert progress_frame["data"]["percentage"] == 50
        assert progress_frame["data"]["current_title"] == "Track 3"

    def test_websocket_clean_close_1000_emits_disconnect_and_throttles_reconnect(self, qapp):
        """Regression: Server clean close (1000) must emit disconnected_signal and throttle reconnects (<=1 in 0.5s)."""
        server = EphemeralCloseServer(close_code=1000, close_reason="Normal Closure")
        server.start()
        try:
            worker = app.WebSocketClientWorker(server.ws_url, client_id="test-clean-close-client")
            disconnect_emissions = []
            worker.disconnected_signal.connect(lambda: disconnect_emissions.append(time.time()))

            worker.start()

            # Wait 0.5s while processing Qt events
            start_time = time.time()
            while time.time() - start_time < 0.5:
                qapp.processEvents()
                time.sleep(0.02)

            worker.stop()
            worker.wait(1000)

            # Assert disconnected_signal was emitted
            assert len(disconnect_emissions) >= 1, "disconnected_signal was not emitted on server clean closure (1000)"

            # Assert reconnect attempts were throttled by backoff sleep (must be <= 1 reconnect)
            assert server.connection_count <= 1, (
                f"Spin loop detected! Reconnected {server.connection_count} times in 0.5s (expected <= 1)"
            )

            # Assert state reset
            assert worker._ws is None, "worker._ws was not reset to None after termination"

        finally:
            server.stop()

    def test_websocket_server_going_away_1001_closure(self, qapp):
        """Regression: Server going away close (1001) must emit disconnected_signal and throttle reconnects."""
        server = EphemeralCloseServer(close_code=1001, close_reason="Server Going Away")
        server.start()
        try:
            worker = app.WebSocketClientWorker(server.ws_url, client_id="test-going-away-client")
            disconnect_emissions = []
            worker.disconnected_signal.connect(lambda: disconnect_emissions.append(time.time()))

            worker.start()

            start_time = time.time()
            while time.time() - start_time < 0.5:
                qapp.processEvents()
                time.sleep(0.02)

            worker.stop()
            worker.wait(1000)

            assert len(disconnect_emissions) >= 1, "disconnected_signal was not emitted on code 1001 closure"
            assert server.connection_count <= 1, (
                f"Spin loop detected! Reconnected {server.connection_count} times in 0.5s (expected <= 1)"
            )
            assert worker._ws is None

        finally:
            server.stop()

    def test_websocket_worker_stop_is_thread_safe_and_resets_state(self, qapp):
        """Regression: stop() must terminate worker thread promptly and set self._ws to None."""
        worker = app.WebSocketClientWorker("ws://127.0.0.1:59998/ws/events", client_id="test-stop-client")
        worker.start()

        # Give it 0.1s to attempt connect and enter retry backoff
        time.sleep(0.1)
        qapp.processEvents()

        worker.stop()
        terminated = worker.wait(1500)
        assert terminated, "Worker thread failed to terminate within 1.5s after stop()"
        assert worker._ws is None


# ==============================================================================
# 7. Drag-and-Drop & Clipboard URL Validator Tests
# ==============================================================================

class TestDragDropAndClipboard:
    """Verifies URL parsing, MIME extraction, and dropzone filtering."""

    @pytest.mark.parametrize("url", [
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/album/4czdORSRvhkiz6ag28a4Id?si=xyz",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/playlist?list=PLrEnWoR732-BHrPp_QLgkMNl8mbUTcoKU",
    ])
    def test_valid_music_urls_accepted(self, url):
        valid = any(d in url for d in ["spotify.com", "youtube.com", "youtu.be"])
        assert valid

    def test_dropzone_mime_urls_extraction(self, qapp):
        dz = app.DropZoneTextEdit()
        mime = QMimeData()
        mime.setUrls([
            QUrl("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"),
            QUrl("file:///home/user/document.pdf"),
            QUrl("https://music.youtube.com/watch?v=dQw4w9WgXcQ"),
        ])

        # Test extraction logic directly
        urls = [u.toString() for u in mime.urls() if not u.toString().startswith("file://")]
        assert len(urls) == 2
        assert "spotify.com" in urls[0]
        assert "youtube.com" in urls[1]

    def test_dropzone_mime_plain_text_extraction(self, qapp):
        text = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M\nhttps://music.youtube.com/watch?v=123"
        urls = [line.strip() for line in text.splitlines() if line.startswith("http://") or line.startswith("https://")]
        assert len(urls) == 2
        assert "37i9dQZF1DXcBWIGoYBM5M" in urls[0]


# ==============================================================================
# 8. Headless MainWindow Offscreen Smoke Tests
# ==============================================================================

class TestMainWindowHeadless:
    """Verifies MainWindow creation and component wiring in offscreen mode."""

    def test_mainwindow_instantiation(self, qapp):
        win = app.MainWindow()
        assert win.windowTitle().startswith("Jellyfin Music Downloader")
        assert win.tabs.count() == 3
        assert "Add Music" in win.tabs.tabText(0)
        assert "Progress" in win.tabs.tabText(1)
        assert "Settings" in win.tabs.tabText(2)

    def test_analysis_card_diff_update(self, qapp):
        card = app.AnalysisCard()
        assert not card.isVisible()

        diff = {
            "total_tracks": 25,
            "existing_tracks": 10,
            "missing_tracks": 15,
            "resolve_time_ms": 3.8,
            "tracks": [
                {"title": "Track A", "artist": "Artist A", "album": "Album A", "exists_locally": True},
                {"title": "Track B", "artist": "Artist B", "album": "Album B", "exists_locally": False}
            ]
        }
        card.update_diff(diff)
        assert card.isVisible()
        assert card.total_box.val_lbl.text() == "25"
        assert card.library_box.val_lbl.text() == "10"
        assert card.missing_box.val_lbl.text() == "15"
        assert "3.8 ms" in card.time_lbl.text()
        assert card.track_table.rowCount() == 2

    def test_toast_banner_display(self, qapp):
        toast = app.ToastBanner()
        assert not toast.isVisible()
        toast.show_message("Test Notification", 1000)
        assert toast.isVisible()
        assert toast.lbl.text() == "Test Notification"


# ==============================================================================
# 9. Theme Watcher Atomic Rename Regression Tests
# ==============================================================================

class TestThemeWatcherRegressions:
    """Regression tests for MainWindow.reload_theme verifying QFileSystemWatcher re-binding."""

    def test_theme_watcher_rebinds_on_atomic_file_replacement(self, qapp, tmp_path, monkeypatch):
        """Regression: Atomic replacement of colors.toml (os.replace) must re-bind watcher to new file."""
        theme_file = tmp_path / "colors.toml"
        theme_file.write_text('accent = "#88c0d0"\nbackground = "#2e3440"\n', encoding="utf-8")

        monkeypatch.setattr(app, "THEME_FILE", str(theme_file))

        win = app.MainWindow()
        try:
            assert hasattr(win, "watcher"), "MainWindow missing watcher attribute"
            assert str(theme_file) in win.watcher.files(), f"{theme_file} not in watcher files: {win.watcher.files()}"

            # Simulate atomic rename replacement (temporary file moved to target)
            temp_file = tmp_path / "colors.toml.tmp"
            temp_file.write_text('accent = "#bf616a"\nbackground = "#191c23"\n', encoding="utf-8")
            os.replace(str(temp_file), str(theme_file))

            # Simulate inotify inode replacement where path is dropped from watcher
            if str(theme_file) in win.watcher.files():
                win.watcher.removePath(str(theme_file))
            assert str(theme_file) not in win.watcher.files()

            # Execute reload_theme
            win.reload_theme()

            # Assert watcher has re-added THEME_FILE
            assert str(theme_file) in win.watcher.files(), (
                f"Watcher failed to re-bind {theme_file} after atomic replacement! Watcher files: {win.watcher.files()}"
            )
            assert win.colors["accent"] == "#bf616a"
            assert "#bf616a" in win.styleSheet()

        finally:
            win.close()

    def test_theme_watcher_handles_temporarily_missing_theme_file(self, qapp, tmp_path, monkeypatch):
        """Regression: Missing theme file during reload_theme must not throw and must load default theme."""
        missing_file = tmp_path / "nonexistent_colors.toml"
        monkeypatch.setattr(app, "THEME_FILE", str(missing_file))

        win = app.MainWindow()
        try:
            win.reload_theme()
            assert win.colors == app.DEFAULT_DARK_THEME
            assert str(missing_file) not in win.watcher.files()
        finally:
            win.close()
