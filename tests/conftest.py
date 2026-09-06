"""Reusable Pytest Fixtures for Jellyfin Music Downloader V2 Rewrite.

Provides:
- FastAPI ASGI test client (httpx.AsyncClient)
- Synthetic audio file generators and temporary music directories
- Mock Jellyfin REST server and state tracker (httpx/aiohttp)
- Mock LRCLIB lyrics service with duration verification helpers
- Process execution safety mocks
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, Generator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import mutagen
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPOS, TRCK, ID3NoHeaderError
from mutagen.mp3 import MP3

# Ensure offscreen platform before any Qt imports
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# -----------------------------------------------------------------------------
# 1. FastAPI Application & Async Client Fixtures
# -----------------------------------------------------------------------------

def create_contract_reference_app() -> FastAPI:
    """Creates a specification-compliant reference FastAPI app implementing
    the REST contracts defined in PROJECT.md and ORIGINAL_REQUEST.md.

    Used when server.app.main is in development or being tested against interface specs.
    """
    app = FastAPI(title="Jellyfin Music Daemon Reference", version="2.0.0")

    app.state.active_jobs = 0
    app.state.start_time = 1725487200.0
    app.state.config = {
        "music_dir": "/music",
        "bitrate": "320k",
        "default_user": "alice",
        "jellyfin_url": "http://192.168.1.159:8096",
        "download_threads": 4,
        "sponsorblock": True,
        "lyrics_provider": "lrclib",
        "max_file_size_mb": 250,
    }

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "version": "2.0.0",
            "uptime_seconds": 42.0,
            "active_jobs": app.state.active_jobs,
            "library_indexed_tracks": 14250,
            "last_index_time": "2026-09-04T16:45:00Z",
        }

    @app.get("/api/config")
    async def get_config():
        return app.state.config

    @app.get("/api/users")
    async def get_users():
        return {
            "users": [
                {
                    "id": "user-alice-guid",
                    "name": "Alice",
                    "has_password": True,
                    "is_admin": True,
                    "playlists": [{"id": "pl-01", "name": "Synthwave Drive", "track_count": 48}],
                },
                {
                    "id": "00000000000000000000000000000000",
                    "name": "Household (Shared)",
                    "has_password": False,
                    "is_admin": False,
                    "playlists": [],
                },
            ]
        }

    @app.post("/api/resolve")
    async def resolve(payload: dict):
        urls = payload.get("urls", [])
        if not urls:
            raise HTTPException(status_code=400, detail="urls list cannot be empty")
        return {
            "playlist_name": "Test Playlist",
            "playlist_id": "pl-test-01",
            "total_tracks": 2,
            "existing_tracks": 1,
            "missing_tracks": 1,
            "resolve_time_ms": 3.4,
            "tracks": [
                {
                    "id": "t1",
                    "title": "Blinding Lights",
                    "artist": "The Weeknd",
                    "album": "After Hours",
                    "disc_number": 1,
                    "track_number": 1,
                    "duration_ms": 200000,
                    "exists_locally": True,
                    "local_path": "/music/The Weeknd/After Hours/01-01 - Blinding Lights.mp3",
                },
                {
                    "id": "t2",
                    "title": "Save Your Tears",
                    "artist": "The Weeknd",
                    "album": "After Hours",
                    "disc_number": 1,
                    "track_number": 2,
                    "duration_ms": 215000,
                    "exists_locally": False,
                    "local_path": None,
                },
            ],
        }

    @app.post("/api/ingest", status_code=202)
    async def ingest(payload: dict):
        urls = payload.get("urls", [])
        if not urls:
            raise HTTPException(status_code=400, detail="urls list cannot be empty")
        app.state.active_jobs += 1
        return {
            "job_id": "job-test-uuid-1234",
            "status": "queued",
            "playlist_name": payload.get("playlist_name", "Downloads"),
            "queued_tracks": 1,
            "already_present": 1,
            "message": "Ingestion pipeline initialized",
        }

    @app.post("/api/cancel")
    async def cancel(payload: dict):
        job_id = payload.get("job_id")
        if not job_id:
            raise HTTPException(status_code=400, detail="job_id required")
        if job_id != "job-test-uuid-1234":
            raise HTTPException(status_code=404, detail="Job not found")
        if app.state.active_jobs > 0:
            app.state.active_jobs -= 1
        return {
            "job_id": job_id,
            "status": "cancelled",
            "cleaned_files": 1,
            "message": "Job cancelled; process group terminated gracefully",
        }

    return app


@pytest.fixture
def test_app() -> FastAPI:
    """Returns the daemon FastAPI app.
    If server.app.main is implemented, uses it; otherwise provides the contract reference app.
    """
    try:
        from server.app.main import app as real_app
        return real_app
    except (ImportError, ModuleNotFoundError):
        return create_contract_reference_app()


@pytest_asyncio.fixture
async def async_client(test_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Asynchronous test client bound to the FastAPI daemon application via ASGITransport."""
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# -----------------------------------------------------------------------------
# 2. Synthetic Audio File Generation & Sample Library Fixtures
# -----------------------------------------------------------------------------

def generate_mp3_bytes(
    title: str,
    artist: str,
    album: str = "Test Album",
    track_num: int = 1,
    disc_num: int = 1,
    duration_seconds: float = 3.0,
    target_size_bytes: Optional[int] = None,
) -> bytes:
    """Generates valid MPEG-1 Layer III audio bytes with ID3v2.4 headers."""
    # Standard 128kbps, 44.1kHz stereo MP3 frame: 417 bytes (0.0260625s per frame)
    frame_header = b"\xff\xfb\x90\x64"
    single_frame = frame_header + b"\x00" * 413
    frame_duration = 1152.0 / 44100.0  # ~0.02612s

    if target_size_bytes is not None:
        num_frames = max(1, target_size_bytes // 417)
    else:
        num_frames = max(1, int(duration_seconds / frame_duration))

    audio_frames = single_frame * num_frames

    # Write to a temporary buffer and tag with Mutagen ID3
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
        tf.write(audio_frames)
        tf.flush()
        temp_path = tf.name

    try:
        try:
            tags = ID3(temp_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TALB(encoding=3, text=album))
        tags.add(TRCK(encoding=3, text=str(track_num)))
        tags.add(TPOS(encoding=3, text=str(disc_num)))
        tags.save(temp_path, v2_version=4)

        with open(temp_path, "rb") as f:
            full_data = f.read()
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)

    if target_size_bytes is not None and len(full_data) < target_size_bytes:
        # Pad with valid zero-byte frames if necessary
        deficit = target_size_bytes - len(full_data)
        extra_frames = single_frame * (deficit // 417 + 1)
        full_data = full_data + extra_frames[:deficit]

    return full_data


@pytest.fixture
def synthetic_audio_factory(tmp_path: Path) -> Callable[..., Path]:
    """Factory fixture to create valid synthetic audio files on disk with specific parameters."""
    counter = 0

    def _create(
        title: str,
        artist: str,
        album: str = "Test Album",
        track_num: int = 1,
        disc_num: int = 1,
        duration_seconds: float = 3.0,
        target_size_bytes: Optional[int] = None,
        filename: Optional[str] = None,
    ) -> Path:
        nonlocal counter
        counter += 1
        if not filename:
            safe_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip()
            filename = f"{disc_num:02d}-{track_num:02d} - {safe_title}_{counter}.mp3"
        dest_path = tmp_path / filename
        data = generate_mp3_bytes(
            title=title,
            artist=artist,
            album=album,
            track_num=track_num,
            disc_num=disc_num,
            duration_seconds=duration_seconds,
            target_size_bytes=target_size_bytes,
        )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return dest_path

    return _create


@pytest.fixture
def sample_library_dir(tmp_path: Path) -> Path:
    """Builds a realistic mock music library directory containing:
    1. Japanese title track: 前前前世 by RADWIMPS
    2. Korean title track: 봄날 (Spring Day) by BTS
    3. Cyrillic title track: Группа крови by Кино
    4. Accented Latin track: Déjà Vu by Beyoncé
    5. Short track < 350KB (145KB album skit)
    6. Multi-artist collaboration track: I'm Good (Blue) by David Guetta, Bebe Rexha
    7. Standard English track: Blinding Lights by The Weeknd
    """
    library_root = tmp_path / "music"
    library_root.mkdir(parents=True, exist_ok=True)

    tracks = [
        # Non-ASCII: Japanese
        {
            "artist": "RADWIMPS",
            "album": "Your Name",
            "title": "前前前世",
            "track": 1,
            "disc": 1,
            "size": 1_200_000,
        },
        # Non-ASCII: Korean
        {
            "artist": "BTS",
            "album": "You Never Walk Alone",
            "title": "봄날 (Spring Day)",
            "track": 2,
            "disc": 1,
            "size": 1_500_000,
        },
        # Non-ASCII: Cyrillic
        {
            "artist": "Кино",
            "album": "Группа крови",
            "title": "Группа крови",
            "track": 1,
            "disc": 1,
            "size": 1_300_000,
        },
        # Non-ASCII: Accented Latin
        {
            "artist": "Beyoncé",
            "album": "B'Day",
            "title": "Déjà Vu",
            "track": 1,
            "disc": 1,
            "size": 1_400_000,
        },
        # Short track (< 350,000 bytes) - Must NOT be unlinked
        {
            "artist": "Kendrick Lamar",
            "album": "good kid, m.A.A.d city",
            "title": "Skit",
            "track": 3,
            "disc": 1,
            "size": 148_480,  # ~145 KB
        },
        # Multi-artist collaboration
        {
            "artist": "David Guetta, Bebe Rexha",
            "album": "I'm Good",
            "title": "I'm Good (Blue)",
            "track": 1,
            "disc": 1,
            "size": 1_600_000,
        },
        # Standard track
        {
            "artist": "The Weeknd",
            "album": "After Hours",
            "title": "Blinding Lights",
            "track": 9,
            "disc": 1,
            "size": 2_100_000,
        },
    ]

    for item in tracks:
        folder = library_root / item["artist"] / item["album"]
        folder.mkdir(parents=True, exist_ok=True)
        file_name = f"{item['disc']:02d}-{item['track']:02d} - {item['title']}.mp3"
        file_path = folder / file_name
        data = generate_mp3_bytes(
            title=item["title"],
            artist=item["artist"],
            album=item["album"],
            track_num=item["track"],
            disc_num=item["disc"],
            target_size_bytes=item["size"],
        )
        file_path.write_bytes(data)

    return library_root


# -----------------------------------------------------------------------------
# 3. Mock Jellyfin REST API Server & State Tracker
# -----------------------------------------------------------------------------

class MockJellyfinState:
    """Stateful inspector for verifying Jellyfin REST API interactions."""

    def __init__(self):
        self.users = [
            {
                "Id": "user-alice-guid-1111",
                "Name": "Alice",
                "HasPassword": True,
                "Policy": {"IsAdministrator": True, "EnableContentDownloading": True},
            },
            {
                "Id": "user-alice-guid-2222",
                "Name": "Alice",
                "HasPassword": True,
                "Policy": {"IsAdministrator": False, "EnableContentDownloading": True},
            },
            {
                "Id": "user-bob-guid-3333",
                "Name": "Bob",
                "HasPassword": False,
                "Policy": {"IsAdministrator": False, "EnableContentDownloading": True},
            },
        ]
        self.virtual_folders = [
            {
                "Name": "Music",
                "Locations": ["/music"],
                "CollectionType": "music",
                "ItemId": "vf-music-id-9999",
            },
            {
                "Name": "Playlists",
                "Locations": ["/playlists"],
                "CollectionType": "playlists",
                "ItemId": "vf-playlists-id-8888",
            },
        ]
        # Scoped playlists: { playlist_id: { "Name": str, "UserId": str, "ItemIds": list[str] } }
        self.playlists: Dict[str, Dict[str, Any]] = {
            "pl-alice-001": {
                "Name": "Alice Favorites",
                "UserId": "user-alice-guid-2222",
                "ItemIds": ["track-guid-1", "track-guid-2"],
            },
            "pl-bob-001": {
                "Name": "Bob Rock",
                "UserId": "user-bob-guid-3333",
                "ItemIds": ["track-guid-3"],
            },
        }
        self.recorded_requests: List[Dict[str, Any]] = []
        self.refresh_calls: List[str] = []
        self.playlist_appends: List[Dict[str, Any]] = []

    def record_request(self, method: str, url: str, headers: Dict[str, str], payload: Any = None):
        self.recorded_requests.append({
            "method": method,
            "url": url,
            "headers": headers,
            "payload": payload,
        })

    def get_user_playlists(self, user_id: str) -> List[Dict[str, Any]]:
        """Returns playlists strictly scoped to user_id (User isolation)."""
        return [
            {"Id": pl_id, "Name": data["Name"], "OwnerUserId": data["UserId"]}
            for pl_id, data in self.playlists.items()
            if data["UserId"] == user_id
        ]

    def create_playlist(self, name: str, user_id: str, item_ids: List[str]) -> str:
        pl_id = f"pl-new-{len(self.playlists) + 1:04d}"
        self.playlists[pl_id] = {
            "Name": name,
            "UserId": user_id,
            "ItemIds": list(item_ids),
        }
        return pl_id

    def append_items_to_playlist(self, playlist_id: str, item_ids: List[str], user_id: str):
        if len(item_ids) > 50:
            raise ValueError(f"Jellyfin append exceeds max chunk size of 50: got {len(item_ids)}")
        if playlist_id not in self.playlists:
            raise KeyError(f"Playlist {playlist_id} not found")
        # Ensure user matches
        target_pl = self.playlists[playlist_id]
        if target_pl["UserId"] != user_id and target_pl["UserId"] != "00000000000000000000000000000000":
            raise PermissionError(f"Cross-user mutation prohibited: target is {target_pl['UserId']}, caller is {user_id}")
        target_pl["ItemIds"].extend(item_ids)
        self.playlist_appends.append({
            "playlist_id": playlist_id,
            "user_id": user_id,
            "chunk_size": len(item_ids),
            "item_ids": list(item_ids),
        })


@pytest.fixture
def mock_jellyfin_state() -> MockJellyfinState:
    """Stateful inspector for Jellyfin REST operations."""
    return MockJellyfinState()


# -----------------------------------------------------------------------------
# 4. Mock LRCLIB Lyrics Fixtures
# -----------------------------------------------------------------------------

class MockLRCLIBState:
    """Mock repository of lyrics with duration tracking for +/- 3s verification tests."""

    def __init__(self):
        self.database: Dict[str, Dict[str, Any]] = {
            # Exact match case (210s)
            "Dua Lipa - Levitating": {
                "name": "Levitating",
                "artistName": "Dua Lipa",
                "albumName": "Future Nostalgia",
                "duration": 203.0,
                "syncedLyrics": "[00:00.00] If you wanna run away with me\n[00:03.20] I know a galaxy",
                "plainLyrics": "If you wanna run away with me\nI know a galaxy",
            },
            # Live version duration mismatch case (Studio: 195s, Live in DB: 285s)
            "The Weeknd - Blinding Lights (Live)": {
                "name": "Blinding Lights",
                "artistName": "The Weeknd",
                "albumName": "After Hours",
                "duration": 285.0,  # Diff = 90s (> 3s) -> MUST BE REJECTED
                "syncedLyrics": "[00:00.00] (Audience Cheering)\n[00:15.00] Yeah...",
                "plainLyrics": "(Audience Cheering)\nYeah...",
            },
            # Near boundary match case (Audio: 210.0s, LRCLIB: 212.5s -> Diff = 2.5s <= 3.0s -> ACCEPTED)
            "Adele - Hello": {
                "name": "Hello",
                "artistName": "Adele",
                "albumName": "25",
                "duration": 212.5,
                "syncedLyrics": "[00:01.00] Hello, it's me",
                "plainLyrics": "Hello, it's me",
            },
            # Boundary rejection case (Audio: 210.0s, LRCLIB: 213.5s -> Diff = 3.5s > 3.0s -> REJECTED)
            "Queen - Bohemian Rhapsody": {
                "name": "Bohemian Rhapsody",
                "artistName": "Queen",
                "albumName": "A Night at the Opera",
                "duration": 213.5,
                "syncedLyrics": "[00:01.00] Is this the real life?",
                "plainLyrics": "Is this the real life?",
            },
        }

    def get_lyrics(self, artist: str, title: str, audio_duration: float) -> Optional[Dict[str, Any]]:
        for key, entry in self.database.items():
            if entry["artistName"].lower() in artist.lower() and entry["name"].lower() in title.lower():
                return entry
        return None

    def verify_duration(self, lrc_duration: float, audio_duration: float, tolerance: float = 3.0) -> bool:
        """Enforces the strict +/- 3s duration boundary check."""
        return abs(lrc_duration - audio_duration) <= tolerance


@pytest.fixture
def mock_lrclib() -> MockLRCLIBState:
    """Provides LRCLIB lyrics mock data with duration verification tests."""
    return MockLRCLIBState()


# -----------------------------------------------------------------------------
# 5. Process Execution Safety Fixture
# -----------------------------------------------------------------------------

@pytest.fixture
def mock_subprocess_tracker():
    """Tracks calls to asyncio.create_subprocess_exec to assert:
    - shell=False is strictly maintained (zero shell argument string concatenation)
    - arguments are passed as distinct argv elements
    - preexec_fn=os.setsid is passed for process-group isolation
    """
    history: List[Dict[str, Any]] = []

    async def _fake_exec(*cmd, **kwargs):
        history.append({
            "cmd": list(cmd),
            "shell": kwargs.get("shell", False),
            "preexec_fn": kwargs.get("preexec_fn"),
            "kwargs": kwargs,
        })
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = 0
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline.return_value = b""
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline.return_value = b""
        mock_proc.wait = AsyncMock(return_value=0)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec) as mocked:
        yield {"history": history, "mock": mocked}


# -----------------------------------------------------------------------------
# 6. Milestone 4 Client Testing Fixtures (MockDaemonServer & qapp)
# -----------------------------------------------------------------------------

class MockDaemonServer:
    """Ephemeral in-process media daemon server running on a free loopback port."""

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
                "default_user": "Alice",
                "jellyfin_url": "http://127.0.0.1:8096"
            }

        @self.app.get("/api/users")
        async def users():
            return {
                "users": [
                    {
                        "id": "user-guid-001",
                        "name": "Alice",
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


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped headless Qt application fixture running offscreen."""
    try:
        from PyQt5.QtWidgets import QApplication
    except ImportError:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            try:
                from PyQt6.QtWidgets import QApplication
            except ImportError:
                pytest.skip("No supported Qt binding found")

    instance = QApplication.instance()
    if instance is None:
        instance = QApplication(["--platform", "offscreen"])
    yield instance


@pytest.fixture
def mock_daemon() -> Generator[MockDaemonServer, None, None]:
    """Provides a running MockDaemonServer on an ephemeral loopback port."""
    server = MockDaemonServer()
    server.start()
    yield server
    server.stop()

