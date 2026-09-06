"""Empirical Stress Test Suite: 50-Track Sequential Chunking & Order Preservation.

Milestone 3 Challenge Harness for Jellyfin REST Integration.
Validates:
1. Exact boundary sizes for add_items_to_playlist:
   0, 1, 49, 50, 51, 100, 120, 150, and 250 tracks.
2. Invariant: len(chunk) <= 50 for EVERY outgoing HTTP request.
3. Strict sequential ordering across chunk boundaries:
   [0..49] in chunk 1, [50..99] in chunk 2, [100..119] in chunk 3, etc.
4. Validation / capping of chunk_size > 50 (e.g. 75, 100, 500) and non-positive values (0, -1).
5. Transient HTTP 500/503 retry resilience during chunk transmission:
   - Recovery on retry at transport layer (_request)
   - Recovery on retry at chunk loop layer (add_items_to_playlist)
   - Failure handling on persistent 500/503 errors
   - Fast failure without blind retries on 401/403 permission errors
"""

from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from server.app.jellyfin import (
    MAX_CHUNK_SIZE,
    JellyfinClient,
    JellyfinPermissionError,
    JellyfinServerError,
)


@pytest.fixture
def stress_client() -> JellyfinClient:
    """Provides a JellyfinClient configured with minimal backoff for fast stress-testing."""
    return JellyfinClient(
        base_url="http://stress-jellyfin:8096",
        token="stress-test-token-xyz",
        timeout=2.0,
        max_retries=2,
        retry_backoff=0.001,
    )


# ==============================================================================
# 1. Boundary Tests for add_items_to_playlist: 0, 1, 49, 50, 51, 100, 120, 150, 250
# ==============================================================================

@pytest.mark.asyncio
async def test_boundary_0_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 0 tracks makes 0 HTTP requests and returns True."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", [])
    assert result is True
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_boundary_1_track(respx_mock, stress_client: JellyfinClient):
    """Verify 1 track makes exactly 1 HTTP request with 1 item."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = ["track-guid-000"]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 1
    ids = route.calls[0].request.url.params["ids"].split(",")
    assert len(ids) == 1
    assert ids == track_ids


@pytest.mark.asyncio
async def test_boundary_49_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 49 tracks makes exactly 1 HTTP request with 49 items."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(49)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 1
    ids = route.calls[0].request.url.params["ids"].split(",")
    assert len(ids) == 49
    assert ids == track_ids


@pytest.mark.asyncio
async def test_boundary_50_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 50 tracks (exact boundary) makes exactly 1 HTTP request with 50 items."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(50)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 1
    ids = route.calls[0].request.url.params["ids"].split(",")
    assert len(ids) == 50
    assert ids == track_ids


@pytest.mark.asyncio
async def test_boundary_51_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 51 tracks (boundary + 1) splits into exactly 2 requests (50, 1)."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(51)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 2
    c1 = route.calls[0].request.url.params["ids"].split(",")
    c2 = route.calls[1].request.url.params["ids"].split(",")
    assert len(c1) == 50
    assert len(c2) == 1
    assert c1 + c2 == track_ids


@pytest.mark.asyncio
async def test_boundary_100_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 100 tracks (2 full chunks) splits into exactly 2 requests (50, 50)."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(100)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 2
    c1 = route.calls[0].request.url.params["ids"].split(",")
    c2 = route.calls[1].request.url.params["ids"].split(",")
    assert len(c1) == 50
    assert len(c2) == 50
    assert c1 + c2 == track_ids


@pytest.mark.asyncio
async def test_boundary_120_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 120 tracks splits into exactly 3 requests (50, 50, 20)."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(120)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 3
    c1 = route.calls[0].request.url.params["ids"].split(",")
    c2 = route.calls[1].request.url.params["ids"].split(",")
    c3 = route.calls[2].request.url.params["ids"].split(",")
    assert len(c1) == 50
    assert len(c2) == 50
    assert len(c3) == 20
    assert c1 + c2 + c3 == track_ids


@pytest.mark.asyncio
async def test_boundary_150_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 150 tracks (3 full chunks) splits into exactly 3 requests (50, 50, 50)."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(150)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 3
    c1 = route.calls[0].request.url.params["ids"].split(",")
    c2 = route.calls[1].request.url.params["ids"].split(",")
    c3 = route.calls[2].request.url.params["ids"].split(",")
    assert len(c1) == 50
    assert len(c2) == 50
    assert len(c3) == 50
    assert c1 + c2 + c3 == track_ids


@pytest.mark.asyncio
async def test_boundary_250_tracks(respx_mock, stress_client: JellyfinClient):
    """Verify 250 tracks (5 full chunks) splits into exactly 5 requests of 50 each."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"trk-{i:03d}" for i in range(250)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 5
    reconstructed = []
    for call_idx in range(5):
        chunk = route.calls[call_idx].request.url.params["ids"].split(",")
        assert len(chunk) == 50
        reconstructed.extend(chunk)
    assert reconstructed == track_ids


# ==============================================================================
# 2. Invariant: len(chunk) <= 50 for Every Outgoing Request
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("track_count", [0, 1, 2, 25, 49, 50, 51, 99, 100, 101, 120, 149, 150, 151, 200, 250, 317])
async def test_all_outgoing_chunks_bounded_to_max_50(respx_mock, stress_client: JellyfinClient, track_count: int):
    """Verify that regardless of total track count, NO outgoing chunk ever exceeds 50 items."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"item-{i:04d}" for i in range(track_count)]

    result = await stress_client.add_items_to_playlist("u-test", "pl-test", track_ids)
    assert result is True

    if track_count == 0:
        assert route.call_count == 0
        return

    expected_chunk_count = (track_count + 49) // 50
    assert route.call_count == expected_chunk_count

    total_received_items = 0
    for call in route.calls:
        chunk_ids = call.request.url.params["ids"].split(",")
        chunk_len = len(chunk_ids)
        # INVARIANT: len(chunk) <= 50
        assert chunk_len <= MAX_CHUNK_SIZE, f"Chunk exceeded max 50! Found: {chunk_len}"
        assert chunk_len > 0, "Chunk had 0 items!"
        total_received_items += chunk_len

    assert total_received_items == track_count


# ==============================================================================
# 3. Exact Sequential Order Preservation Across Chunk Boundaries
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("track_count", [51, 100, 120, 150, 250])
async def test_strict_sequential_order_across_chunk_boundaries(
    respx_mock,
    stress_client: JellyfinClient,
    track_count: int,
):
    """Verify items are strictly partitioned sequentially across chunk boundaries without permutation."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"ordered-track-{i:04d}" for i in range(track_count)]

    result = await stress_client.add_items_to_playlist("user-alice", "pl-seq", track_ids)
    assert result is True

    reconstructed_order = []
    for chunk_idx, call in enumerate(route.calls):
        chunk = call.request.url.params["ids"].split(",")
        expected_start = chunk_idx * 50
        expected_end = min(expected_start + 50, track_count)
        expected_slice = track_ids[expected_start:expected_end]

        assert chunk == expected_slice, f"Chunk {chunk_idx} violated sequential order!"
        reconstructed_order.extend(chunk)

    assert reconstructed_order == track_ids


# ==============================================================================
# 4. Chunk Size Validation & Capping (chunk_size > 50 and <= 0)
# ==============================================================================

@pytest.mark.asyncio
async def test_chunk_size_greater_than_50_is_capped_to_50(respx_mock, stress_client: JellyfinClient):
    """Verify passing chunk_size=100 with 120 tracks is automatically capped to 50."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"cap-{i:03d}" for i in range(120)]

    # Attempt to bypass 50 limit by specifying chunk_size=100
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids, chunk_size=100)
    assert result is True

    # Must be capped to 50, resulting in 3 chunks (50, 50, 20), NOT 2 chunks of (100, 20)
    assert route.call_count == 3
    for call in route.calls:
        chunk = call.request.url.params["ids"].split(",")
        assert len(chunk) <= 50


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_chunk_size", [51, 75, 100, 500, 10000])
async def test_various_oversized_chunk_sizes_strictly_capped(
    respx_mock,
    stress_client: JellyfinClient,
    oversized_chunk_size: int,
):
    """Verify any chunk_size > 50 is strictly capped to 50."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"cap-test-{i:03d}" for i in range(110)]

    result = await stress_client.add_items_to_playlist(
        "u-1", "pl-01", track_ids, chunk_size=oversized_chunk_size
    )
    assert result is True
    # 110 items capped at 50 per chunk => 3 chunks (50, 50, 10)
    assert route.call_count == 3
    for call in route.calls:
        assert len(call.request.url.params["ids"].split(",")) <= 50


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_chunk_size", [0, -1, -50])
async def test_non_positive_chunk_size_resets_to_50(
    respx_mock,
    stress_client: JellyfinClient,
    invalid_chunk_size: int,
):
    """Verify non-positive chunk_size resets to default 50 rather than infinite loop or crash."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"non-pos-{i:03d}" for i in range(100)]

    result = await stress_client.add_items_to_playlist(
        "u-1", "pl-01", track_ids, chunk_size=invalid_chunk_size
    )
    assert result is True
    assert route.call_count == 2
    assert len(route.calls[0].request.url.params["ids"].split(",")) == 50
    assert len(route.calls[1].request.url.params["ids"].split(",")) == 50


# ==============================================================================
# 5. Retry Behavior on Transient HTTP 500 / 503 Chunk Errors
# ==============================================================================

@pytest.mark.asyncio
async def test_transient_500_recovers_on_retry_at_transport_layer(respx_mock, stress_client: JellyfinClient):
    """Verify HTTP 500 transient server error on chunk transmission retries and succeeds."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items")
    # First call fails with 500 Internal Server Error, second succeeds with 204
    route.side_effect = [
        httpx.Response(500, text="Internal Server Error: temporary DB lock"),
        httpx.Response(204),
    ]

    track_ids = [f"retry500-{i}" for i in range(10)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 2
    # Verify the items sent on the retry were identical
    assert route.calls[1].request.url.params["ids"] == ",".join(track_ids)


@pytest.mark.asyncio
async def test_transient_503_recovers_on_retry_at_transport_layer(respx_mock, stress_client: JellyfinClient):
    """Verify HTTP 503 Service Unavailable on chunk transmission retries and succeeds."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items")
    # First call fails with 503 Service Unavailable, second succeeds with 204
    route.side_effect = [
        httpx.Response(503, text="Service Unavailable"),
        httpx.Response(204),
    ]

    track_ids = [f"retry503-{i}" for i in range(10)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 2
    assert route.calls[1].request.url.params["ids"] == ",".join(track_ids)


@pytest.mark.asyncio
async def test_multi_chunk_transient_503_on_middle_chunk_recovers(respx_mock, stress_client: JellyfinClient):
    """Verify in a multi-chunk transmission (120 tracks -> 3 chunks), a transient 503 on chunk 2

    retries, recovers, and preserves overall sequence order across all chunks.
    """
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items")
    # Chunk 1 (50 items): 204 success
    # Chunk 2 (50 items): 503 failure on attempt 1 -> 204 success on attempt 2
    # Chunk 3 (20 items): 204 success
    route.side_effect = [
        httpx.Response(204),
        httpx.Response(503, text="Service Busy"),
        httpx.Response(204),
        httpx.Response(204),
    ]

    track_ids = [f"multi-retry-{i:03d}" for i in range(120)]
    result = await stress_client.add_items_to_playlist("u-1", "pl-01", track_ids)

    assert result is True
    assert route.call_count == 4

    # Chunk 1
    c1 = route.calls[0].request.url.params["ids"].split(",")
    assert c1 == track_ids[0:50]

    # Chunk 2 failed attempt
    c2_fail = route.calls[1].request.url.params["ids"].split(",")
    assert c2_fail == track_ids[50:100]

    # Chunk 2 successful retry
    c2_retry = route.calls[2].request.url.params["ids"].split(",")
    assert c2_retry == track_ids[50:100]

    # Chunk 3
    c3 = route.calls[3].request.url.params["ids"].split(",")
    assert c3 == track_ids[100:120]


@pytest.mark.asyncio
async def test_chunk_retry_at_outer_loop_level(respx_mock):
    """Verify that even if _request exhausts its retries (e.g. max_retries=0),

    the outer add_items_to_playlist retry loop catches JellyfinServerError,
    retries the chunk, and successfully appends.
    """
    # Client with max_retries=0 so _request raises immediately on HTTP 500
    zero_retry_client = JellyfinClient(
        base_url="http://stress-jellyfin:8096",
        token="tok",
        max_retries=0,
    )

    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items")
    # Outer loop attempt 1: _request raises JellyfinServerError (500)
    # Outer loop attempt 2: _request returns 204
    route.side_effect = [
        httpx.Response(500, text="Temporary error"),
        httpx.Response(204),
    ]

    with patch("server.app.jellyfin.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await zero_retry_client.add_items_to_playlist("u-1", "pl-01", ["t1", "t2"])
        assert result is True
        assert route.call_count == 2
        # Verify outer sleep was called with backoff
        assert mock_sleep.called


@pytest.mark.asyncio
async def test_persistent_500_exhausts_all_retries_and_returns_false(respx_mock, stress_client: JellyfinClient):
    """Verify that when HTTP 500 persists across all transport and chunk retries,

    add_items_to_playlist logs the error and gracefully returns False (does not crash daemon).
    """
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(500, text="Persistent DB failure")

    with patch("server.app.jellyfin.asyncio.sleep", new_callable=AsyncMock):
        result = await stress_client.add_items_to_playlist("u-1", "pl-01", ["trk-a", "trk-b"])
        assert result is False
        # Transport level: max_retries=2 means 3 attempts per chunk try.
        # Outer chunk loop: 3 attempts.
        # Total attempts: 3 * 3 = 9 calls.
        assert route.call_count >= 3


@pytest.mark.asyncio
async def test_auth_and_permission_errors_fail_fast_without_blind_retries(respx_mock, stress_client: JellyfinClient):
    """Verify HTTP 401/403 fails fast and immediately raises JellyfinPermissionError

    rather than wasting time on useless retry loops.
    """
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(403, text="Forbidden")

    with patch("server.app.jellyfin.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(JellyfinPermissionError):
            await stress_client.add_items_to_playlist("u-alice", "pl-bob", ["trk-1"])

        # Exactly 1 call was made — zero blind retry attempts!
        assert route.call_count == 1
        assert not mock_sleep.called


@pytest.mark.asyncio
async def test_rate_limit_429_recovers_on_retry(respx_mock, stress_client: JellyfinClient):
    """Verify HTTP 429 Too Many Requests triggers backoff and recovers on subsequent attempt."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items")
    route.side_effect = [
        httpx.Response(429, text="Rate limit exceeded"),
        httpx.Response(204),
    ]

    result = await stress_client.add_items_to_playlist("u-1", "pl-01", ["trk-1", "trk-2"])
    assert result is True
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_network_connect_error_recovers_on_retry(respx_mock, stress_client: JellyfinClient):
    """Verify transient network connection failure during chunk transmission retries and recovers."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items")
    route.side_effect = [
        httpx.ConnectError("Socket connection reset by peer"),
        httpx.Response(204),
    ]

    result = await stress_client.add_items_to_playlist("u-1", "pl-01", ["trk-1", "trk-2"])
    assert result is True
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_flexible_signature_chunking_preserves_order(respx_mock, stress_client: JellyfinClient):
    """Verify calling add_items_to_playlist(playlist_id, item_ids, user_id) works identically."""
    route = respx_mock.post(path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"sig-track-{i:03d}" for i in range(120)]

    # Alternative signature: (playlist_id, item_ids, user_id)
    result = await stress_client.add_items_to_playlist("pl-flex-01", track_ids, "user-charlie")
    assert result is True
    assert route.call_count == 3

    reconstructed = []
    for call in route.calls:
        chunk = call.request.url.params["ids"].split(",")
        assert len(chunk) <= 50
        assert call.request.url.params["userId"] == "user-charlie"
        reconstructed.extend(chunk)

    assert reconstructed == track_ids


def test_invalid_argument_types_raise_value_error(stress_client: JellyfinClient):
    """Verify passing invalid types (e.g. integer or non-list) raises ValueError."""
    with pytest.raises(ValueError, match="invalid argument types"):
        asyncio.run(stress_client.add_items_to_playlist(12345, None, "bad"))  # type: ignore

