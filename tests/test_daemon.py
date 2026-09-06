"""Tests for Asynchronous Backend Daemon REST Endpoints (M1 / Tier 1 & Tier 2).

Validates:
- GET /health: Daemon status, version 2.0.0, uptime, active job metrics
- GET /api/config: Runtime configuration, storage directories, Jellyfin URL
- GET /api/users: Scoped user listing proxy
- POST /api/resolve: Input validation and playlist pre-flight diff summary
- POST /api/ingest: Queueing ingestion jobs with 202 Accepted
- POST /api/cancel: Process-targeted cancellation and error handling on invalid IDs
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_healthcheck_endpoint(async_client: httpx.AsyncClient):
    """Tier 1: Verify GET /health returns liveness status, version, uptime, and active jobs."""
    response = await async_client.get("/health")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()

    # Core healthcheck invariants per PROJECT.md § 91 & ORIGINAL_REQUEST.md R1
    assert "status" in data, "Missing 'status' in health response"
    assert data["status"] in ("healthy", "ok"), f"Unexpected status value: {data['status']}"
    assert data.get("version") == "2.0.0", f"Expected version '2.0.0', got {data.get('version')}"
    assert isinstance(data.get("uptime_seconds"), (int, float)), "uptime_seconds must be numeric"
    assert data["uptime_seconds"] >= 0, "uptime_seconds cannot be negative"
    assert isinstance(data.get("active_jobs"), int), "active_jobs must be integer"
    assert data["active_jobs"] >= 0, "active_jobs cannot be negative"


@pytest.mark.asyncio
async def test_config_endpoint(async_client: httpx.AsyncClient):
    """Tier 1: Verify GET /api/config returns active server configuration parameters."""
    response = await async_client.get("/api/config")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    config = response.json()

    # Required configuration fields per PROJECT.md § 95
    required_fields = [
        "music_dir",
        "bitrate",
        "default_user",
        "jellyfin_url",
        "download_threads",
    ]
    for field in required_fields:
        assert field in config, f"Missing required config field: {field}"

    assert isinstance(config["music_dir"], str), "music_dir must be string path"
    assert isinstance(config["download_threads"], int), "download_threads must be int"
    assert config["download_threads"] >= 1, "download_threads must be at least 1"

    # Security check: Token/secret values must NOT be exposed in plain public config
    forbidden_tokens = ["api_key", "password", "token_secret"]
    for forbidden in forbidden_tokens:
        assert forbidden not in config, f"Sensitive secret '{forbidden}' exposed in /api/config"


@pytest.mark.asyncio
async def test_users_proxy_endpoint(async_client: httpx.AsyncClient):
    """Tier 1: Verify GET /api/users proxies active Jellyfin user accounts."""
    response = await async_client.get("/api/users")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert "users" in data, "Response missing 'users' list"
    assert isinstance(data["users"], list), "'users' must be a list"
    assert len(data["users"]) > 0, "'users' list should not be empty in test environment"

    first_user = data["users"][0]
    assert "id" in first_user, "User object missing 'id'"
    assert "name" in first_user, "User object missing 'name'"
    assert "playlists" in first_user, "User object missing 'playlists'"


@pytest.mark.asyncio
async def test_resolve_endpoint_validation(async_client: httpx.AsyncClient):
    """Tier 2: Verify POST /api/resolve validates input and returns diff structure."""
    # 1. Empty URLs list should trigger 400 or 422
    empty_resp = await async_client.post("/api/resolve", json={"urls": []})
    assert empty_resp.status_code in (400, 422), f"Expected 400/422 on empty urls, got {empty_resp.status_code}"

    # 2. Valid URL triggers resolution
    valid_resp = await async_client.post(
        "/api/resolve",
        json={"urls": ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"]},
    )
    assert valid_resp.status_code == 200, f"Expected 200, got {valid_resp.status_code}: {valid_resp.text}"
    diff_data = valid_resp.json()

    assert "playlist_name" in diff_data
    assert "total_tracks" in diff_data
    assert "existing_tracks" in diff_data
    assert "missing_tracks" in diff_data
    assert diff_data["total_tracks"] == diff_data["existing_tracks"] + diff_data["missing_tracks"]
    assert "tracks" in diff_data
    assert isinstance(diff_data["tracks"], list)


@pytest.mark.asyncio
async def test_ingest_queue_endpoint_validation(async_client: httpx.AsyncClient):
    """Tier 2: Verify POST /api/ingest enqueues jobs and validates required parameters."""
    # 1. Empty URLs should be rejected with 400 or 422
    invalid_resp = await async_client.post(
        "/api/ingest",
        json={
            "urls": [],
            "playlist_name": "My Ingest",
        },
    )
    assert invalid_resp.status_code in (400, 422), f"Expected 400/422 on empty urls, got {invalid_resp.status_code}"

    # 2. Ingest without user_id (library-only) is accepted with 202
    lib_resp = await async_client.post(
        "/api/ingest",
        json={
            "urls": ["https://open.spotify.com/artist/2qNp0oOz8q9x0Sg1yJ9z9a"],
            "playlist_name": "__NO_PLAYLIST__",
        },
    )
    assert lib_resp.status_code == 202, f"Expected 202 Accepted for library-only ingest, got {lib_resp.status_code}"
    lib_body = lib_resp.json()
    assert "job_id" in lib_body
    assert lib_body.get("status") == "queued"

    # 3. Valid playlist request with user_id returns 202 Accepted with job_id
    valid_resp = await async_client.post(
        "/api/ingest",
        json={
            "urls": ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"],
            "user_id": "user-kendon-guid",
            "playlist_name": "Synthwave",
            "bitrate": "320k",
            "embed_lyrics": True,
            "embed_cover": True,
        },
    )
    assert valid_resp.status_code == 202, f"Expected 202 Accepted, got {valid_resp.status_code}"
    body = valid_resp.json()
    assert "job_id" in body, "Response missing 'job_id'"
    assert body.get("status") == "queued", f"Expected status 'queued', got {body.get('status')}"


@pytest.mark.asyncio
async def test_cancel_nonexistent_job_returns_404(async_client: httpx.AsyncClient):
    """Tier 2: Verify POST /api/cancel returns 404 when requested job does not exist."""
    response = await async_client.post("/api/cancel", json={"job_id": "job-does-not-exist-99999"})
    assert response.status_code == 404, f"Expected 404 for unknown job, got {response.status_code}"


@pytest.mark.asyncio
async def test_cancel_valid_job(async_client: httpx.AsyncClient):
    """Tier 1: Verify POST /api/cancel succeeds when target job is active."""
    # First queue a job
    ingest_resp = await async_client.post(
        "/api/ingest",
        json={
            "urls": ["https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"],
            "user_id": "user-kendon-guid",
            "playlist_name": "To Cancel",
        },
    )
    job_id = ingest_resp.json()["job_id"]

    # Cancel the queued job
    cancel_resp = await async_client.post("/api/cancel", json={"job_id": job_id})
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.json()
    assert cancel_data["job_id"] == job_id
    assert cancel_data["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unknown_route_returns_404(async_client: httpx.AsyncClient):
    """Tier 2: Verify unmapped endpoints return HTTP 404."""
    response = await async_client.get("/api/unknown_route_endpoint")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_ingest_pipeline_resolution_failure_broadcasts_job_completed(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
):
    """Verify that when resolver.resolve() fails during ingestion,
    the background pipeline broadcasts an error LogEvent and terminal JobCompletedEvent(failed=total).
    """
    from unittest.mock import AsyncMock
    from server.app.ws import ws_manager, JobCompletedEvent, JobStartedEvent

    mock_resolver = AsyncMock()
    mock_resolver.resolve.side_effect = RuntimeError("Failed to connect to upstream service")
    test_app.state.resolver = mock_resolver

    broadcast_events = []
    orig_broadcast = ws_manager.broadcast

    async def capture_broadcast(event, job_id=None):
        broadcast_events.append(event)
        return await orig_broadcast(event, job_id=job_id)

    ws_manager.broadcast = capture_broadcast

    try:
        resp = await async_client.post(
            "/api/ingest",
            json={
                "urls": [
                    "https://open.spotify.com/track/invalid1",
                    "https://open.spotify.com/track/invalid2",
                ],
                "user_id": "user-test",
                "playlist_name": "Test Fail",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        pm = test_app.state.process_manager
        job = pm.get_job(job_id)
        assert job is not None
        if job.worker_tasks:
            await asyncio.gather(*job.worker_tasks, return_exceptions=True)

        event_types = [getattr(e, "event", None) for e in broadcast_events]
        assert "job_started" in event_types
        assert "log" in event_types
        assert "job_completed" in event_types

        err_logs = [e for e in broadcast_events if getattr(e, "event", None) == "log" and getattr(e, "level", None) == "ERROR"]
        assert len(err_logs) >= 1
        assert "Pre-flight resolution failed" in err_logs[0].message

        completed_events = [e for e in broadcast_events if getattr(e, "event", None) == "job_completed"]
        assert len(completed_events) == 1
        jc = completed_events[0]
        assert jc.job_id == job_id
        assert jc.downloaded == 0
        assert jc.skipped == 0
        assert jc.failed == 2
        assert jc.playlist_id is None
        assert job.status == "failed"

    finally:
        ws_manager.broadcast = orig_broadcast


@pytest.mark.asyncio
async def test_ingest_pipeline_resolution_returns_none_broadcasts_job_completed(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
):
    """Verify that when resolver.resolve() returns None, the background pipeline
    broadcasts terminal JobCompletedEvent(failed=total) and sets job.status to failed.
    """
    from unittest.mock import AsyncMock
    from server.app.ws import ws_manager, JobCompletedEvent

    mock_resolver = AsyncMock()
    mock_resolver.resolve.return_value = None
    test_app.state.resolver = mock_resolver

    broadcast_events = []
    orig_broadcast = ws_manager.broadcast

    async def capture_broadcast(event, job_id=None):
        broadcast_events.append(event)
        return await orig_broadcast(event, job_id=job_id)

    ws_manager.broadcast = capture_broadcast

    try:
        resp = await async_client.post(
            "/api/ingest",
            json={
                "urls": ["https://open.spotify.com/playlist/test-none"],
                "user_id": "user-test",
                "playlist_name": "None Test",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        pm = test_app.state.process_manager
        job = pm.get_job(job_id)
        assert job is not None
        if job.worker_tasks:
            await asyncio.gather(*job.worker_tasks, return_exceptions=True)

        completed_events = [e for e in broadcast_events if getattr(e, "event", None) == "job_completed"]
        assert len(completed_events) == 1
        jc = completed_events[0]
        assert jc.failed == 1
        assert jc.downloaded == 0
        assert job.status == "failed"

    finally:
        ws_manager.broadcast = orig_broadcast


# ==============================================================================
# Milestone 3 Integration Tests: Jellyfin Lifespan, Users Proxy, & Stage 3 Pipeline
# ==============================================================================

@pytest.mark.asyncio
async def test_daemon_lifespan_jellyfin_lifecycle():
    """M3: Verify JellyfinClient is instantiated on app.state during lifespan and closed on teardown."""
    from server.app.main import lifespan
    from server.app.jellyfin import JellyfinClient

    app = FastAPI(lifespan=lifespan)
    async with lifespan(app):
        assert hasattr(app.state, "jellyfin")
        assert isinstance(app.state.jellyfin, JellyfinClient)
        assert not app.state.jellyfin.client.is_closed
    assert app.state.jellyfin._client is None or app.state.jellyfin._client.is_closed


@pytest.mark.asyncio
async def test_users_proxy_live_jellyfin_discovery(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
):
    """M3: Verify GET /api/users queries app.state.jellyfin and returns user-scoped playlists."""
    from unittest.mock import AsyncMock
    from server.app.jellyfin import JellyfinUser, JellyfinPlaylist

    mock_jf = AsyncMock()
    mock_jf.get_users.return_value = [
        JellyfinUser(id="u-alice", name="Alice", has_password=True, is_admin=True),
        JellyfinUser(id="u-bob", name="Bob", has_password=False, is_admin=False),
    ]
    mock_jf.get_user_playlists.side_effect = lambda uid: [
        JellyfinPlaylist(id=f"pl-{uid}", name=f"{uid} Playlist", item_count=10)
    ] if uid == "u-alice" else []

    orig_jf = getattr(test_app.state, "jellyfin", None)
    test_app.state.jellyfin = mock_jf

    try:
        resp = await async_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        users = data["users"]

        alice = next(u for u in users if u["id"] == "u-alice")
        assert alice["name"] == "Alice"
        assert len(alice["playlists"]) == 1
        assert alice["playlists"][0]["id"] == "pl-u-alice"
        assert alice["playlists"][0]["track_count"] == 10
        assert alice["playlists"][0]["item_count"] == 10

        bob = next(u for u in users if u["id"] == "u-bob")
        assert bob["name"] == "Bob"
        assert len(bob["playlists"]) == 0

        household = next(u for u in users if u["id"] == "00000000000000000000000000000000")
        assert household["name"] == "Household (Shared)"

    finally:
        test_app.state.jellyfin = orig_jf


@pytest.mark.asyncio
async def test_users_proxy_unreachable_jellyfin_falls_back_gracefully(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
):
    """M3: Verify GET /api/users falls back to default configured user when Jellyfin is unreachable."""
    from unittest.mock import AsyncMock
    from server.app.jellyfin import JellyfinConnectionError

    mock_jf = AsyncMock()
    mock_jf.get_users.side_effect = JellyfinConnectionError("Connection refused")
    orig_jf = getattr(test_app.state, "jellyfin", None)
    test_app.state.jellyfin = mock_jf

    try:
        resp = await async_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert len(data["users"]) > 0
        first_user = data["users"][0]
        assert first_user["id"] == "user-kendon-guid"
        assert first_user["name"] == "Kendon"
    finally:
        test_app.state.jellyfin = orig_jf


@pytest.mark.asyncio
async def test_users_proxy_401_unauthorized_falls_back_gracefully(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
):
    """M3: Verify GET /api/users falls back gracefully when token is 401 Unauthorized without crashing."""
    from unittest.mock import AsyncMock
    from server.app.jellyfin import JellyfinAuthError

    mock_jf = AsyncMock()
    mock_jf.get_users.side_effect = JellyfinAuthError("Invalid API token", status_code=401)
    orig_jf = getattr(test_app.state, "jellyfin", None)
    test_app.state.jellyfin = mock_jf

    try:
        resp = await async_client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["users"]) > 0
        assert data["users"][0]["id"] == "user-kendon-guid"
    finally:
        test_app.state.jellyfin = orig_jf


@pytest.mark.asyncio
async def test_ingest_pipeline_stage3_jellyfin_assembly_end_to_end(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
    tmp_path: Path,
):
    """M3: Verify end-to-end Stage 3 Ingestion Pipeline wires library refresh,
    track resolution, playlist creation, chunked append, and terminal JobCompletedEvent.
    """
    from unittest.mock import AsyncMock
    from server.app.downloader import TrackResult
    from server.app.resolver import ResolveResponse, ResolveTrack
    from server.app.ws import ws_manager, JobCompletedEvent

    # Mock resolver
    mock_resolver = AsyncMock()
    mock_resolver.resolve.return_value = ResolveResponse(
        playlist_name="Synthwave 2026",
        playlist_id="pl-res-01",
        total_tracks=2,
        existing_tracks=1,
        missing_tracks=1,
        tracks=[
            ResolveTrack(id="t1", title="Existing Song", artist="Artist", exists_locally=True, local_path=str(tmp_path / "t1.mp3")),
            ResolveTrack(id="t2", title="Downloaded Song", artist="Artist", exists_locally=False),
        ],
    )
    test_app.state.resolver = mock_resolver

    # Mock downloader
    fake_downloaded = tmp_path / "t2.mp3"
    fake_downloaded.write_text("fake audio")
    mock_downloader = AsyncMock()
    mock_downloader.download_missing_tracks.return_value = [
        TrackResult(track_id="t2", title="Downloaded Song", artist="Artist", album="Album", path=fake_downloaded, success=True),
    ]
    test_app.state.downloader = mock_downloader

    # Mock Jellyfin
    mock_jf = AsyncMock()
    mock_jf.refresh_library = AsyncMock()
    mock_jf.resolve_track_item_ids = AsyncMock(return_value=["jf-item-guid-1", "jf-item-guid-2"])
    mock_jf.create_or_get_playlist = AsyncMock(return_value="jf-pl-real-guid-999")
    mock_jf.add_items_to_playlist = AsyncMock(return_value=True)
    orig_jf = getattr(test_app.state, "jellyfin", None)
    test_app.state.jellyfin = mock_jf

    # Capture broadcast events
    broadcast_events = []
    orig_broadcast = ws_manager.broadcast

    async def capture_broadcast(event, job_id=None):
        broadcast_events.append(event)
        return await orig_broadcast(event, job_id=job_id)

    ws_manager.broadcast = capture_broadcast

    try:
        resp = await async_client.post(
            "/api/ingest",
            json={
                "urls": ["https://open.spotify.com/playlist/test-jellyfin-e2e"],
                "user_id": "u-alice-guid",
                "playlist_name": "Synthwave 2026",
            },
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        pm = test_app.state.process_manager
        job = pm.get_job(job_id)
        assert job is not None
        if job.worker_tasks:
            await asyncio.gather(*job.worker_tasks, return_exceptions=True)

        # Assert Stage 3 Jellyfin operations were executed in exact order
        mock_jf.refresh_library.assert_awaited_once()
        mock_jf.resolve_track_item_ids.assert_awaited_once()
        tracks_passed = mock_jf.resolve_track_item_ids.call_args[1]["tracks"]
        assert len(tracks_passed) == 2
        assert tracks_passed[0]["title"] == "Existing Song"
        assert tracks_passed[1]["title"] == "Downloaded Song"

        mock_jf.create_or_get_playlist.assert_awaited_once_with(
            user_id="u-alice-guid",
            playlist_name="Synthwave 2026",
        )
        assert mock_jf.add_items_to_playlist.call_count == 1
        call_kwargs = mock_jf.add_items_to_playlist.call_args[1]
        assert call_kwargs["user_id"] == "u-alice-guid"
        assert call_kwargs["playlist_id"] == "jf-pl-real-guid-999"
        assert call_kwargs["item_ids"] == ["jf-item-guid-1", "jf-item-guid-2"]

        # Assert terminal JobCompletedEvent contains real Jellyfin playlist GUID
        completed_events = [e for e in broadcast_events if getattr(e, "event", None) == "job_completed"]
        assert len(completed_events) == 1
        jc = completed_events[0]
        assert jc.playlist_id == "jf-pl-real-guid-999"
        assert jc.downloaded == 1
        assert jc.skipped == 1
        assert jc.failed == 0
        assert job.status == "completed"

    finally:
        ws_manager.broadcast = orig_broadcast
        test_app.state.jellyfin = orig_jf


@pytest.mark.asyncio
async def test_ingest_pipeline_stage3_50_track_chunking_execution(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
    tmp_path: Path,
):
    """M3: Verify pipeline splits 120 tracks into 3 sequential chunks (50, 50, 20)."""
    from unittest.mock import AsyncMock
    from server.app.downloader import TrackResult
    from server.app.resolver import ResolveResponse, ResolveTrack

    total_count = 120
    tracks = [
        ResolveTrack(id=f"t{i}", title=f"Song {i}", artist="Artist", exists_locally=True, local_path=str(tmp_path / f"t{i}.mp3"))
        for i in range(total_count)
    ]
    mock_resolver = AsyncMock()
    mock_resolver.resolve.return_value = ResolveResponse(
        playlist_name="Chunk Test",
        playlist_id="pl-chunk",
        total_tracks=total_count,
        existing_tracks=total_count,
        missing_tracks=0,
        tracks=tracks,
    )
    test_app.state.resolver = mock_resolver

    mock_downloader = AsyncMock()
    mock_downloader.download_missing_tracks.return_value = []
    test_app.state.downloader = mock_downloader

    mock_jf = AsyncMock()
    mock_jf.refresh_library = AsyncMock()
    mock_jf.resolve_track_item_ids = AsyncMock(return_value=[f"guid-{i:03d}" for i in range(total_count)])
    mock_jf.create_or_get_playlist = AsyncMock(return_value="pl-chunk-real")
    mock_jf.add_items_to_playlist = AsyncMock(return_value=True)
    orig_jf = getattr(test_app.state, "jellyfin", None)
    test_app.state.jellyfin = mock_jf

    try:
        resp = await async_client.post(
            "/api/ingest",
            json={"urls": ["https://open.spotify.com/playlist/chunk"], "user_id": "u-1", "playlist_name": "Chunk Test"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        pm = test_app.state.process_manager
        job = pm.get_job(job_id)
        if job and job.worker_tasks:
            await asyncio.gather(*job.worker_tasks, return_exceptions=True)

        assert mock_jf.add_items_to_playlist.call_count == 3
        c1 = mock_jf.add_items_to_playlist.call_args_list[0][1]["item_ids"]
        c2 = mock_jf.add_items_to_playlist.call_args_list[1][1]["item_ids"]
        c3 = mock_jf.add_items_to_playlist.call_args_list[2][1]["item_ids"]
        assert len(c1) == 50
        assert len(c2) == 50
        assert len(c3) == 20
        assert c1[0] == "guid-000" and c2[0] == "guid-050" and c3[0] == "guid-100"

    finally:
        test_app.state.jellyfin = orig_jf


@pytest.mark.asyncio
async def test_ingest_pipeline_stage3_jellyfin_error_preserves_downloaded_tracks(
    async_client: httpx.AsyncClient,
    test_app: FastAPI,
    tmp_path: Path,
):
    """M3: Verify Jellyfin assembly errors do not delete downloaded files or fail download job."""
    from unittest.mock import AsyncMock
    from server.app.downloader import TrackResult
    from server.app.jellyfin import JellyfinConnectionError
    from server.app.resolver import ResolveResponse, ResolveTrack

    fake_file = tmp_path / "preserve.mp3"
    fake_file.write_text("precious audio bytes")

    mock_resolver = AsyncMock()
    mock_resolver.resolve.return_value = ResolveResponse(
        playlist_name="Error Resilience",
        playlist_id="pl-err",
        total_tracks=1,
        existing_tracks=0,
        missing_tracks=1,
        tracks=[ResolveTrack(id="t1", title="Song", artist="Artist", exists_locally=False)],
    )
    test_app.state.resolver = mock_resolver

    mock_downloader = AsyncMock()
    mock_downloader.download_missing_tracks.return_value = [
        TrackResult(track_id="t1", title="Song", artist="Artist", album="Album", path=fake_file, success=True),
    ]
    test_app.state.downloader = mock_downloader

    mock_jf = AsyncMock()
    mock_jf.refresh_library.side_effect = JellyfinConnectionError("Jellyfin server down")
    mock_jf.resolve_track_item_ids.side_effect = JellyfinConnectionError("Jellyfin server down")
    orig_jf = getattr(test_app.state, "jellyfin", None)
    test_app.state.jellyfin = mock_jf

    try:
        resp = await async_client.post(
            "/api/ingest",
            json={"urls": ["https://open.spotify.com/playlist/resilience"], "user_id": "u-1", "playlist_name": "Error Resilience"},
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        pm = test_app.state.process_manager
        job = pm.get_job(job_id)
        if job and job.worker_tasks:
            await asyncio.gather(*job.worker_tasks, return_exceptions=True)

        # Downloaded audio file must be 100% preserved
        assert fake_file.exists()
        assert fake_file.read_text() == "precious audio bytes"
        # Job marked completed because downloads succeeded
        assert job.status == "completed"

    finally:
        test_app.state.jellyfin = orig_jf


