"""Empirical Verification Tests for WebSocket Event Stream, Connection Manager & Schemas.

Validates:
- WebSocket connection handshake (/ws/events?client_id=...) emitting 'connected' event.
- Keepalive ping/pong control frames.
- Strict Pydantic event models for all 10 V2 event types:
  - job_started, stage_transition, progress, track_completed, track_failed,
    log, job_completed, job_cancelled, job_error, ping.
- Dual-compatibility progress parsing (percentage/current_track/total_tracks and pct/current/total).
- Selective routing for job-subscribed vs global clients.
- Dead-socket pruning and connection lifecycle management.
- Periodic heartbeat_worker execution.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from server.app.main import app
from server.app.ws import (
    ConnectedEvent,
    ConnectionManager,
    JobCancelledEvent,
    JobCompletedEvent,
    JobErrorEvent,
    JobStartedEvent,
    LogEvent,
    PingEvent,
    ProgressEvent,
    StageTransitionEvent,
    TrackCompletedEvent,
    TrackFailedEvent,
    heartbeat_worker,
    ws_manager,
)


def test_ws_connection_and_handshake():
    """Verify client connection emits initial 'connected' frame with client_id."""
    client = TestClient(app)
    with client.websocket_connect("/ws/events?client_id=test-client-1") as ws:
        msg = ws.receive_json()
        assert msg["event"] == "connected"
        assert msg["client_id"] == "test-client-1"
        assert "active_clients" in msg["data"]
        assert msg["data"]["ping_interval_seconds"] == 30


def test_ws_ping_pong():
    """Verify sending ping receives pong response."""
    client = TestClient(app)
    with client.websocket_connect("/ws/events?client_id=test-ping-pong") as ws:
        # Initial connect frame
        ws.receive_json()

        # Plain text ping
        ws.send_text("ping")
        resp = ws.receive_text()
        assert resp == "pong"

        # JSON ping
        ws.send_text(json.dumps({"action": "ping"}))
        json_resp = ws.receive_json()
        assert json_resp["event"] == "pong"


def test_progress_event_dual_compatibility():
    """Verify ProgressEvent schema satisfies dual-compatibility contract for QML and Qt."""
    prog = ProgressEvent(
        job_id="job-compat-1",
        percentage=50.0,
        pct=50.0,
        current_track=5,
        current=5,
        total_tracks=10,
        total=10,
        current_title="Synthwave Sunset",
        speed="3.2 MB/s",
        eta_seconds=25,
        status="Downloading",
    )
    raw = json.loads(prog.model_dump_json())

    # Invariants: both representations present and matching
    assert raw["event"] == "progress"
    assert raw["job_id"] == "job-compat-1"
    assert raw["percentage"] == 50.0
    assert raw["pct"] == 50.0
    assert raw["current_track"] == 5
    assert raw["current"] == 5
    assert raw["total_tracks"] == 10
    assert raw["total"] == 10
    assert raw["current_title"] == "Synthwave Sunset"
    assert raw["speed"] == "3.2 MB/s"
    assert raw["eta_seconds"] == 25


def test_event_models_serialization():
    """Verify serialization invariants across all 10 V2 event types."""
    # 1. JobStartedEvent
    js = JobStartedEvent(
        job_id="job-1",
        user_id="user-1",
        playlist_name="Mix",
        total_tracks=20,
        to_download=15,
        already_present=5,
    )
    assert js.event == "job_started"
    assert js.total_tracks == 20

    # 2. StageTransitionEvent
    st = StageTransitionEvent(
        job_id="job-1",
        stage=2,
        stage_name="Fetch Missing Tracks",
        description="Downloading 15 tracks with 4 workers",
    )
    assert st.event == "stage_transition"
    assert st.stage == 2

    # 3. TrackCompletedEvent
    tc = TrackCompletedEvent(
        job_id="job-1",
        track_id="tr-1",
        track="Song A",
        artist="Artist B",
        duration=210.5,
        lyrics_synced=True,
        cover_embedded=True,
        path="/music/Artist B/Album/01 - Song A.mp3",
    )
    assert tc.event == "track_completed"
    assert tc.lyrics_synced is True

    # 4. TrackFailedEvent
    tf = TrackFailedEvent(
        job_id="job-1",
        track_id="tr-2",
        track="Song B",
        artist="Artist C",
        error="HTTP 404 Not Found",
        retrying=False,
    )
    assert tf.event == "track_failed"
    assert tf.error == "HTTP 404 Not Found"

    # 5. LogEvent
    le = LogEvent(
        job_id="job-1",
        level="INFO",
        message="Fetched metadata successfully",
        logger_name="metadata",
    )
    assert le.event == "log"
    assert le.level == "INFO"

    # 6. JobCompletedEvent
    jc = JobCompletedEvent(
        job_id="job-1",
        playlist_id="pl-001",
        playlist_name="Mix",
        downloaded=15,
        skipped=5,
        failed=0,
        duration_seconds=42.8,
    )
    assert jc.event == "job_completed"
    assert jc.downloaded == 15

    # 7. JobCancelledEvent
    jcan = JobCancelledEvent(
        job_id="job-1",
        reason="User requested cancellation",
        cleaned_files=2,
    )
    assert jcan.event == "job_cancelled"
    assert jcan.cleaned_files == 2

    # 8. JobErrorEvent
    je = JobErrorEvent(
        job_id="job-1",
        error="Network timeout",
        details="Connection dropped",
    )
    assert je.event == "job_error"
    assert je.error == "Network timeout"

    # 9. PingEvent
    pe = PingEvent(data={"server_time": 1725487200.0})
    assert pe.event == "ping"

    # 10. ConnectedEvent
    ce = ConnectedEvent(client_id="cid-99")
    assert ce.event == "connected"


@pytest.mark.asyncio
async def test_connection_manager_broadcast_and_selective_subscription():
    """Verify ConnectionManager delivers job-specific events only to subscribed or global clients."""
    manager = ConnectionManager()

    mock_ws_global = MagicMock()
    mock_ws_global.accept = AsyncMock()
    mock_ws_global.send_text = AsyncMock()

    mock_ws_job1 = MagicMock()
    mock_ws_job1.accept = AsyncMock()
    mock_ws_job1.send_text = AsyncMock()

    mock_ws_job2 = MagicMock()
    mock_ws_job2.accept = AsyncMock()
    mock_ws_job2.send_text = AsyncMock()

    # Connect clients
    await manager.connect(mock_ws_global, "client-global")
    await manager.connect(mock_ws_job1, "client-job1")
    await manager.connect(mock_ws_job2, "client-job2")

    assert manager.count() == 3

    # Subscribe job1 and job2
    manager.subscribe_job(mock_ws_job1, "job-A")
    manager.subscribe_job(mock_ws_job2, "job-B")

    # 1. Broadcast event for job-A
    event_a = {"event": "progress", "job_id": "job-A", "percentage": 10.0}
    await manager.broadcast(event_a, job_id="job-A")

    # Global client and job1 client must receive; job2 client must NOT
    assert mock_ws_global.send_text.await_count == 1
    assert mock_ws_job1.send_text.await_count == 1
    assert mock_ws_job2.send_text.await_count == 0

    # 2. Broadcast event for job-B
    event_b = {"event": "progress", "job_id": "job-B", "percentage": 20.0}
    await manager.broadcast(event_b, job_id="job-B")

    assert mock_ws_global.send_text.await_count == 2
    assert mock_ws_job1.send_text.await_count == 1
    assert mock_ws_job2.send_text.await_count == 1

    # 3. Unsubscribe job1 and verify
    manager.unsubscribe_job(mock_ws_job1, "job-A")
    event_a2 = {"event": "progress", "job_id": "job-A", "percentage": 30.0}
    await manager.broadcast(event_a2, job_id="job-A")

    # Since job1 unsubscribed and has no subscribed_jobs, it is now global and receives
    assert mock_ws_job1.send_text.await_count == 2

    # Disconnect all
    await manager.close_all()
    assert manager.count() == 0


@pytest.mark.asyncio
async def test_connection_manager_dead_socket_pruning():
    """Verify dead sockets throwing errors during send are automatically pruned."""
    manager = ConnectionManager()

    dead_ws = MagicMock()
    dead_ws.accept = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=RuntimeError("Connection closed"))

    healthy_ws = MagicMock()
    healthy_ws.accept = AsyncMock()
    healthy_ws.send_text = AsyncMock()

    await manager.connect(dead_ws, "client-dead")
    await manager.connect(healthy_ws, "client-healthy")
    assert manager.count() == 2

    # Broadcast triggers prune of dead_ws
    await manager.broadcast({"event": "test"})

    assert manager.count() == 1
    healthy_ws.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_heartbeat_worker_broadcast():
    """Verify heartbeat_worker sends ping frames periodically."""
    mock_manager = MagicMock()
    mock_manager.send_heartbeat = AsyncMock()

    worker_task = asyncio.create_task(heartbeat_worker(mock_manager, interval=0.02))
    await asyncio.sleep(0.05)
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    assert mock_manager.send_heartbeat.await_count >= 1
