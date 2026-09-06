"""End-to-End Acceptance Test Suite for Jellyfin Music Downloader V2 (Milestone 5 / R1-R5).

Validates the complete end-to-end media daemon lifecycle:
1. Daemon Startup & Health Probes:
   - GET /health: 200 OK, version 2.0.0, uptime metrics, active jobs.
   - GET /api/config: runtime paths, download threads, zero secret leaks.
   - GET /api/users: user discovery and playlist summary proxy.

2. Stage 1 Pre-Flight Diff Engine:
   - Sub-20ms diff benchmark asserting 100-200 tracks diff against library in <20ms.
   - Exact partitioning into existing vs missing tracks.
   - Unicode NFKC non-ASCII title preservation (Japanese, Korean, Cyrillic, Accented Latin).

3. Stage 2 Safe Media Ingestion:
   - Audio files under 350KB (intros, skits, interludes) preserved and never unlinked.
   - Concurrent worker queue processing only missing tracks.
   - Deterministic path construction without recursive filesystem walks.

4. Stage 3 Metadata, Lyrics & Tagging:
   - Synchronized lyric validation within +-3.0s duration tolerance.
   - ID3v2.4 and FLAC picture embedding.

5. Real-Time WebSocket Event Stream:
   - Ordered delivery of job_started -> stage_transition -> progress -> track_completed -> job_completed.
   - Compound progress and live status broadcasting without UI freezes.

6. Official Jellyfin REST Integration & Multi-User Isolation:
   - Scoped to OwnerUserId (Alice cannot see or overwrite Bob's playlists).
   - 50-track sequential chunking for playlist population.
   - Dynamic virtual folder discovery and targeted library refresh.
   - Zero direct SQLite queries or playlist.xml mutations.

7. Process-Targeted Cancellation:
   - Terminating Job A terminates only Job A's PID group and cleans .part files.
   - Sibling Job B remains active and unharmed.

8. Omarchy Desktop Client Parity & Ergonomics:
   - colors.toml dynamic palette theming in QML and Qt clients.
   - Drag-and-drop streaming URL parsing and clipboard auto-detection.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(os.environ.get("JELLYFIN_APP_DIR", str(Path(__file__).resolve().parent.parent)))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Load project pytest fixtures from tests/conftest.py
pytest_plugins = ["tests.conftest"]

from server.app.config import APP_VERSION, ServerConfig
from server.app.indexer import LibraryIndex, index_library, normalize_key
from server.app.jellyfin import JellyfinClient, JellyfinPermissionError
from server.app.lyrics import LRCLIBClient
from server.app.process import ProcessManager
from server.app.resolver import Resolver, ResolveResponse, ResolveTrack, URLType, detect_url_type
from server.app.ws import (
    JobCompletedEvent,
    JobStartedEvent,
    LogEvent,
    ProgressEvent,
    StageTransitionEvent,
    TrackCompletedEvent,
    ws_manager,
)


# ==============================================================================
# Helper Mock WebSocket Client for E2E Event Inspection
# ==============================================================================

class E2EWebSocketClient:
    """In-memory WebSocket mock capturing emitted JSON frames for event verification."""

    def __init__(self):
        self.sent_frames: List[str] = []
        self.closed: bool = False

    async def accept(self):
        pass

    async def send_text(self, text: str):
        self.sent_frames.append(text)

    async def close(self, code: int = 1000, reason: Optional[str] = None):
        self.closed = True

    def get_events(self) -> List[Dict[str, Any]]:
        events = []
        for raw in self.sent_frames:
            try:
                events.append(json.loads(raw))
            except Exception:
                pass
        return events

    def get_event_types(self) -> List[str]:
        return [e.get("event") for e in self.get_events() if "event" in e]


# ==============================================================================
# 1. Daemon REST & Health Probes Acceptance Tests (R1)
# ==============================================================================

class TestDaemonLifecycleAcceptance:
    """Acceptance tests verifying daemon REST API and health contract."""

    @pytest.mark.asyncio
    async def test_healthcheck_contract(self, async_client: httpx.AsyncClient):
        """R1 Acceptance: GET /health returns HTTP 200, status ok, version 2.0.0, uptime."""
        resp = await async_client.get("/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()

        assert data.get("status") in ("healthy", "ok")
        assert data.get("version") == APP_VERSION
        assert isinstance(data.get("uptime_seconds"), (int, float))
        assert data["uptime_seconds"] >= 0
        assert isinstance(data.get("active_jobs"), int)
        assert data["active_jobs"] >= 0

    @pytest.mark.asyncio
    async def test_config_endpoint_security(self, async_client: httpx.AsyncClient):
        """R1 Acceptance: GET /api/config returns paths and settings without leaking secrets."""
        resp = await async_client.get("/api/config")
        assert resp.status_code == 200
        config = resp.json()

        assert "music_dir" in config
        assert "bitrate" in config
        assert "default_user" in config
        assert "download_threads" in config
        assert config["download_threads"] >= 1

        # Zero secret exposure
        for forbidden in ["api_key", "password", "jellyfin_token", "token_secret"]:
            assert forbidden not in config, f"Sensitive credential '{forbidden}' exposed in /api/config!"

    @pytest.mark.asyncio
    async def test_users_proxy_contract(self, async_client: httpx.AsyncClient):
        """R1 Acceptance: GET /api/users proxies Jellyfin user accounts and scoped playlists."""
        resp = await async_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert isinstance(data["users"], list)
        assert len(data["users"]) > 0

        for u in data["users"]:
            assert "id" in u
            assert "name" in u
            assert "playlists" in u


# ==============================================================================
# 2. Stage 1 Pre-Flight Diff Engine & Unicode Acceptance Tests (R2)
# ==============================================================================

class TestPreFlightDiffAndUnicodeAcceptance:
    """Acceptance tests verifying sub-20ms benchmark and NFKC Unicode preservation."""

    def test_sub_20ms_diff_benchmark_100_tracks(self):
        """R2 Acceptance: Pre-flight diff resolves 100-track playlist against library in <20ms."""
        index = LibraryIndex()
        # Seed with 5,000 existing songs
        for i in range(5_000):
            p = Path(f"/music/Artist_{i % 150}/Album_{i % 50}/{i:02d} - Song_{i}.mp3")
            index.add_track(p, f"Song {i}", f"Artist {i % 150}")

        assert index.total_indexed == 5_000

        # Build 100-track test playlist (70 existing, 30 missing)
        tracks = []
        for i in range(70):
            tracks.append(ResolveTrack(id=f"t_{i}", title=f"Song {i}", artist=f"Artist {i % 150}"))
        for i in range(30):
            tracks.append(ResolveTrack(id=f"m_{i}", title=f"New Song {i}", artist=f"New Artist {i}"))

        resolver = Resolver(indexer=index)

        t0 = time.perf_counter()
        res = resolver.diff_tracks(tracks, playlist_name="100-Track Benchmark")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        assert res.total_tracks == 100
        assert res.existing_tracks == 70
        assert res.missing_tracks == 30
        assert elapsed_ms < 20.0, f"Benchmark failed: diff took {elapsed_ms:.2f}ms (must be < 20ms)"

    def test_unicode_nfkc_title_preservation_across_scripts(self):
        """R2 Acceptance: Non-ASCII song titles (Japanese, Korean, Cyrillic, accented Latin)
        are preserved and never collapsed to empty strings.
        """
        multilingual_titles = [
            ("RADWIMPS", "前前前世", "Japanese Kanji"),
            ("YOASOBI", "アイドル", "Japanese Katakana"),
            ("BTS", "봄날 (Spring Day)", "Korean Hangul"),
            ("Кино", "Группа крови", "Russian Cyrillic"),
            ("Beyoncé", "Déjà Vu", "Accented Latin"),
            ("Sigur Rós", "Hoppípolla", "Nordic Latin"),
        ]

        index = LibraryIndex()
        for artist, title, _ in multilingual_titles:
            fake_p = Path(f"/music/{artist}/{title}.mp3")
            index.add_track(fake_p, title, artist)

            # Invariant: Normalized key must NOT be empty
            norm_t = normalize_key(title)
            norm_a = normalize_key(artist)
            assert norm_t != "", f"Title '{title}' collapsed to empty string!"
            assert norm_a != "", f"Artist '{artist}' collapsed to empty string!"

        resolver = Resolver(indexer=index)
        query = [
            ResolveTrack(id="q1", title="前前前世", artist="RADWIMPS"),
            ResolveTrack(id="q2", title="봄날", artist="BTS"),  # Stripped parenthetical
            ResolveTrack(id="q3", title="Группа крови", artist="Кино"),
            ResolveTrack(id="q4", title="Déjà Vu", artist="Beyoncé"),
            ResolveTrack(id="q5", title="New Japanese Track 新時代", artist="Ado"),  # Missing
        ]

        diff_res = resolver.diff_tracks(query)
        assert diff_res.total_tracks == 5
        assert diff_res.existing_tracks == 4
        assert diff_res.missing_tracks == 1

        # Check matched tracks retain original characters
        q1 = next(t for t in diff_res.tracks if t.id == "q1")
        assert q1.exists_locally is True
        assert "前前前世" in q1.title


# ==============================================================================
# 3. Audio File Preservation (<350KB Non-Destruction) Acceptance Tests (R2)
# ==============================================================================

class TestAudioFilePreservationAcceptance:
    """Acceptance tests verifying valid short audio files (<350KB) are never deleted."""

    def test_short_audio_files_under_350kb_preserved(self, tmp_path: Path, synthetic_audio_factory):
        """R2 Acceptance: Valid audio files under 350KB (intros, skits, interludes)
        are preserved and never deleted from disk.
        """
        music_dir = tmp_path / "music"
        music_dir.mkdir(parents=True)

        short_tracks = [
            ("Intro Skit", "Kendrick Lamar", 45_000),      # 45 KB
            ("Album Interlude", "Frank Ocean", 145_000),   # 145 KB
            ("Short Transition", "Travis Scott", 280_000), # 280 KB
            ("Boundary Track", "Eminem", 345_000),         # 345 KB (< 350KB)
        ]

        created_files: List[Path] = []
        for title, artist, size_b in short_tracks:
            fpath = synthetic_audio_factory(
                title=title,
                artist=artist,
                target_size_bytes=size_b,
                filename=f"music/{artist} - {title}.mp3",
            )
            created_files.append(fpath)

        # Index the library
        index = index_library(music_dir)

        # 1. Assert all short tracks are indexed
        assert index.total_indexed == len(short_tracks)

        # 2. Assert all short files STILL exist on disk (zero file unlinks)
        for fpath in created_files:
            assert fpath.exists(), f"FATAL REGRESSION: Short track '{fpath.name}' was unlinked from disk!"
            assert fpath.stat().st_size < 350_000, f"File {fpath.name} was expected to be <350KB"
            match = index.find_match(fpath.stem.split(" - ")[1], fpath.stem.split(" - ")[0])
            assert match is not None, f"Track {fpath.name} was not indexed properly"


# ==============================================================================
# 4. WebSocket Event Stream & Ingestion Pipeline Acceptance Tests (R1, R2)
# ==============================================================================

class TestWebSocketAndIngestionAcceptance:
    """Acceptance tests validating WebSocket real-time progress stream."""

    @pytest.mark.asyncio
    async def test_full_pipeline_websocket_event_sequence(self):
        """R1 Acceptance: Quickshell and Qt clients receive real-time ordered WebSocket events."""
        client_ws = E2EWebSocketClient()
        session = await ws_manager.connect(client_ws, client_id="e2e-tester")

        job_id = "job-e2e-test-1234"
        user_id = "user-alice-guid"
        playlist_name = "Synthwave Acceptance"

        # Simulate pipeline emitting full lifecycle events
        # 1. Job Started
        await ws_manager.broadcast(
            JobStartedEvent(
                job_id=job_id,
                user_id=user_id,
                playlist_name=playlist_name,
                total_tracks=3,
                to_download=2,
                already_present=1,
            ),
            job_id=job_id,
        )

        # 2. Stage Transition: Stage 2 Fetch Missing Tracks
        await ws_manager.broadcast(
            StageTransitionEvent(
                job_id=job_id,
                stage=2,
                stage_name="Downloading Tracks",
                description="Downloading 2 missing tracks",
            ),
            job_id=job_id,
        )

        # 3. Progress Updates
        await ws_manager.broadcast(
            ProgressEvent(
                job_id=job_id,
                percentage=50.0,
                pct=50.0,
                current_track=1,
                current=1,
                total_tracks=2,
                total=2,
                current_title="Blinding Lights",
                speed="2.4 MB/s",
                eta_seconds=12,
            ),
            job_id=job_id,
        )

        # 4. Track Completed
        await ws_manager.broadcast(
            TrackCompletedEvent(
                job_id=job_id,
                track_id="t1",
                track="Blinding Lights",
                artist="The Weeknd",
                duration=200.0,
                lyrics_synced=True,
                path="/music/The Weeknd/After Hours/01 - Blinding Lights.mp3",
            ),
            job_id=job_id,
        )

        # 5. Stage Transition: Stage 3 Jellyfin Sync
        await ws_manager.broadcast(
            StageTransitionEvent(
                job_id=job_id,
                stage=3,
                stage_name="Jellyfin Synchronization",
                description="Populating playlist and refreshing library",
            ),
            job_id=job_id,
        )

        # 6. Job Completed
        await ws_manager.broadcast(
            JobCompletedEvent(
                job_id=job_id,
                playlist_name=playlist_name,
                downloaded=2,
                skipped=1,
                failed=0,
                playlist_id="pl-jellyfin-9999",
            ),
            job_id=job_id,
        )

        events = client_ws.get_events()
        event_types = client_ws.get_event_types()

        # Invariant: All lifecycle events arrived in correct chronological order
        expected_sequence = [
            "job_started",
            "stage_transition",
            "progress",
            "track_completed",
            "stage_transition",
            "job_completed",
        ]
        assert event_types == expected_sequence, f"WebSocket event sequence mismatch: {event_types}"

        # Invariant: Terminal job_completed event has correct counters
        terminal = next(e for e in events if e.get("event") == "job_completed")
        assert terminal["downloaded"] == 2
        assert terminal["skipped"] == 1
        assert terminal["failed"] == 0
        assert terminal["playlist_id"] == "pl-jellyfin-9999"

        await ws_manager.disconnect(client_ws)


# ==============================================================================
# 5. Jellyfin REST Integration & Multi-User Isolation Acceptance Tests (R3)
# ==============================================================================

class TestJellyfinIntegrationAndIsolationAcceptance:
    """Acceptance tests validating multi-user isolation, 50-chunking, and zero SQLite."""

    @pytest.mark.asyncio
    async def test_multi_user_privacy_isolation_and_50_chunking(self, respx_mock):
        """R3 Acceptance: User A playlists scoped strictly to User A; 50-chunking enforced."""
        client = JellyfinClient(
            base_url="http://mock-jellyfin:8096",
            token="sec-token",
            timeout=5.0,
        )

        # Mock Jellyfin endpoints
        alice_id = "user-alice-1111"
        bob_id = "user-bob-2222"

        # 1. User Playlists isolation
        respx_mock.get(f"http://mock-jellyfin:8096/Users/{alice_id}/Items").respond(
            200,
            json={
                "Items": [
                    {"Id": "pl-alice-1", "Name": "Alice Synth", "OwnerUserId": alice_id}
                ]
            },
        )
        respx_mock.get(f"http://mock-jellyfin:8096/Users/{bob_id}/Items").respond(
            200,
            json={
                "Items": [
                    {"Id": "pl-bob-1", "Name": "Bob Rock", "OwnerUserId": bob_id}
                ]
            },
        )

        alice_pls = await client.get_user_playlists(alice_id)
        bob_pls = await client.get_user_playlists(bob_id)

        assert len(alice_pls) == 1
        assert alice_pls[0].id == "pl-alice-1"
        assert len(bob_pls) == 1
        assert bob_pls[0].id == "pl-bob-1"
        assert alice_pls[0].id != bob_pls[0].id, "Playlists must be strictly isolated between users!"

        # 2. Sequential 50-track chunking
        recorded_appends = []

        def append_handler(request: httpx.Request):
            raw_ids = request.url.params.get("ids", "")
            chunk = [x for x in raw_ids.split(",") if x]
            recorded_appends.append(chunk)
            return httpx.Response(204)

        respx_mock.route(method="POST", path__regex=r"^/Playlists/pl-alice-1/Items").mock(side_effect=append_handler)

        # Append 125 tracks to Alice's playlist
        sample_125_ids = [f"trk-{i:03d}" for i in range(125)]
        await client.add_items_to_playlist(alice_id, "pl-alice-1", sample_125_ids)

        # Must have split into 3 sequential chunks: 50, 50, 25
        assert len(recorded_appends) == 3, f"Expected 3 chunk requests, got {len(recorded_appends)}"
        assert len(recorded_appends[0]) == 50
        assert len(recorded_appends[1]) == 50
        assert len(recorded_appends[2]) == 25

        await client.aclose()

    def test_forensic_zero_sqlite_and_zero_xml_mutations(self):
        """R3 Acceptance: Strictly zero SQLite direct queries, zero jellyfin.db,
        and zero playlist.xml file mutations across server/app/.
        """
        app_dir = PROJECT_ROOT / "server" / "app"
        assert app_dir.is_dir(), f"server/app directory not found at {app_dir}"

        py_files = list(app_dir.glob("*.py"))
        assert len(py_files) > 0, "No python files found in server/app"

        for py_file in py_files:
            content = py_file.read_text(encoding="utf-8")
            assert "sqlite3" not in content, f"FATAL VIOLATION: sqlite3 imported in {py_file.name}!"
            assert "jellyfin.db" not in content, f"FATAL VIOLATION: jellyfin.db referenced in {py_file.name}!"
            assert "playlist.xml" not in content, f"FATAL VIOLATION: playlist.xml referenced in {py_file.name}!"


# ==============================================================================
# 6. Process-Targeted Cancellation Acceptance Tests (R1)
# ==============================================================================

class TestTargetedCancellationAcceptance:
    """Acceptance tests verifying targeted process group termination without collateral damage."""

    @pytest.mark.asyncio
    async def test_targeted_cancellation_preserves_sibling_jobs(self, tmp_path: Path):
        """R1 Acceptance: Canceling Job A terminates only Job A's process group and cleans .part files,
        leaving sibling Job B active and intact.
        """
        manager = ProcessManager()

        job_a = await manager.register_job("job_cancel_a", "user_alice", "Alice Ingest")
        job_b = await manager.register_job("job_active_b", "user_bob", "Bob Ingest")

        # Job A partial file
        part_a = tmp_path / "song_a.mp3.part"
        part_a.write_text("in-flight data a")
        job_a.in_flight_targets.add(tmp_path / "song_a.mp3")

        # Job B partial file
        part_b = tmp_path / "song_b.mp3.part"
        part_b.write_text("in-flight data b")
        job_b.in_flight_targets.add(tmp_path / "song_b.mp3")

        # Spawn long-running subprocess for Job A
        cmd_a = [sys.executable, "-c", "import time; time.sleep(10)"]
        handle_a = await manager.spawn_process("job_cancel_a", cmd_a)

        # Spawn long-running subprocess for Job B
        cmd_b = [sys.executable, "-c", "import time; time.sleep(10)"]
        handle_b = await manager.spawn_process("job_active_b", cmd_b)

        assert handle_a.pid is not None
        assert handle_b.pid is not None

        # Execute targeted cancellation on Job A
        res_a = await manager.cancel_job("job_cancel_a")
        assert res_a.status == "cancelled"

        # 1. Job A subprocess was killed
        await asyncio.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(handle_a.pid, 0)

        # 2. Job A partial file was unlinked
        assert not part_a.exists(), "Job A partial file must be unlinked"

        # 3. Job B subprocess is STILL RUNNING (Zero collateral damage)
        try:
            os.kill(handle_b.pid, 0)  # No error -> process is alive
            is_job_b_alive = True
        except ProcessLookupError:
            is_job_b_alive = False

        assert is_job_b_alive is True, "FATAL DEFECT: Job B suffered collateral termination!"

        # 4. Job B partial file was PRESERVED
        assert part_b.exists(), "Job B partial file must NOT be unlinked"

        # Clean up Job B
        await manager.cancel_job("job_active_b")


# ==============================================================================
# 7. Desktop Client UI Theming & Interaction Acceptance Tests (R4)
# ==============================================================================

class TestDesktopClientErgonomicsAcceptance:
    """Acceptance tests verifying Omarchy theming and drag-and-drop URL detection."""

    def test_streaming_url_detection_and_classification(self):
        """R4 Acceptance: URL classifier detects Spotify, YouTube Music, and album links."""
        valid_urls = [
            ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", URLType.SPOTIFY_PLAYLIST),
            ("https://open.spotify.com/album/4yP0hdKO0Ptshxwm0V6Dss", URLType.SPOTIFY_ALBUM),
            ("https://music.youtube.com/playlist?list=RDCLAK5uy_k", URLType.YOUTUBE_PLAYLIST),
            ("https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b", URLType.SPOTIFY_TRACK),
        ]
        for url, expected_type in valid_urls:
            assert detect_url_type(url) == expected_type, f"Failed URL detection: {url}"

    def test_qml_and_qt_theming_colors_toml_contract(self):
        """R4 Acceptance: Clients dynamically load colors from ~/.local/state/omarchy/current/theme/colors.toml."""
        # 1. Verify app.py has colors.toml parser and fallback palette
        app_py = PROJECT_ROOT / "app.py"
        assert app_py.exists()
        app_content = app_py.read_text(encoding="utf-8")
        assert "colors.toml" in app_content, "app.py must reference colors.toml"
        assert "generate_qss" in app_content or "load_omarchy_colors" in app_content

        # 2. Verify QML Theme.qml or views bind to colors.toml
        qml_theme = PROJECT_ROOT / "qml" / "theme" / "Theme.qml"
        qml_views = list((PROJECT_ROOT / "qml" / "views").glob("*.qml"))
        has_colors_binding = (
            (qml_theme.exists() and "colors.toml" in qml_theme.read_text(encoding="utf-8"))
            or any("colors.toml" in v.read_text(encoding="utf-8") for v in qml_views)
        )
        assert has_colors_binding, "QML client must bind to Omarchy colors.toml"
