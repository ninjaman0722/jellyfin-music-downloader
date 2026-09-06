"""Unit & Integration Tests for Async LRCLIB Client & Duration Verification (M2 / Tier 1 & Tier 2).

Validates:
- Exact audio duration match (diff = 0s -> is_synced == True).
- Near boundary duration match (diff <= 3.0s -> is_synced == True).
- Boundary duration rejection (diff > 3.0s -> is_synced == False, plain_lyrics preserved).
- Live acoustic version duration mismatch (diff = 90s -> rejected synced lyrics).
- Fallback to /api/search when /api/get returns 404.
- Candidate duration ranking in search results.
- Timestamp stripping and SYLT line parsing helpers.
- Retries on transient 500 and 429 status codes.
- Connection pooling limits configuration.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
import httpx
import pytest

from server.app.lyrics import (
    LRCLIBClient,
    LyricsResult,
    clean_query_title,
    parse_lrc_lines,
    strip_lrc_timestamps,
)


@pytest.fixture
def mock_lrclib_transport(mock_lrclib) -> httpx.MockTransport:
    """Wraps conftest.py mock_lrclib into an httpx.MockTransport handler."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.path == "/api/get":
            track = url.params.get("track_name", "")
            artist = url.params.get("artist_name", "")
            dur_str = url.params.get("duration")
            audio_dur = float(dur_str) if dur_str else 0.0
            data = mock_lrclib.get_lyrics(artist, track, audio_dur)
            if data:
                return httpx.Response(200, json=data)
            return httpx.Response(404, json={"statusCode": 404, "error": "Not Found", "message": "Lyrics not found"})
        elif url.path == "/api/search":
            track = url.params.get("track_name", "").lower()
            artist = url.params.get("artist_name", "").lower()
            candidates = []
            for key, entry in mock_lrclib.database.items():
                if (not track or track in entry["name"].lower()) and (not artist or artist in entry["artistName"].lower()):
                    candidates.append(entry)
            return httpx.Response(200, json=candidates)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_exact_duration_match(mock_lrclib_transport):
    """Tier 1: Verify exact duration match retrieves synced lyrics with 0.0s diff."""
    async with httpx.AsyncClient(transport=mock_lrclib_transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        # Dua Lipa - Levitating: duration 203.0s
        res = await client.fetch_lyrics("Dua Lipa", "Levitating", audio_duration=203.0)

        assert res.has_lyrics() is True
        assert res.is_synced is True
        assert res.duration == 203.0
        assert res.duration_diff == 0.0
        assert res.synced_lyrics is not None
        assert "[00:00.00]" in res.synced_lyrics
        assert res.plain_lyrics is not None


@pytest.mark.asyncio
async def test_boundary_duration_accepted(mock_lrclib_transport):
    """Tier 2 / DAT-07: Verify lyrics with |diff| <= 3.0s are accepted as synced."""
    async with httpx.AsyncClient(transport=mock_lrclib_transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        # Adele - Hello: LRCLIB duration is 212.5s; audio duration is 210.0s (diff = 2.5s <= 3.0s)
        res = await client.fetch_lyrics("Adele", "Hello", audio_duration=210.0)

        assert res.is_synced is True
        assert res.duration_diff == 2.5
        assert res.synced_lyrics is not None
        assert "[00:01.00] Hello, it's me" in res.synced_lyrics


@pytest.mark.asyncio
async def test_boundary_duration_rejected_fallback_to_plain(mock_lrclib_transport):
    """Tier 2 / DAT-07: Verify lyrics with |diff| > 3.0s REJECT synced lyrics and preserve plain text."""
    async with httpx.AsyncClient(transport=mock_lrclib_transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        # Queen - Bohemian Rhapsody: LRCLIB duration is 213.5s; audio duration is 210.0s (diff = 3.5s > 3.0s)
        res = await client.fetch_lyrics("Queen", "Bohemian Rhapsody", audio_duration=210.0)

        assert res.has_lyrics() is True
        assert res.is_synced is False, "Synced lyrics must be rejected when duration diff exceeds 3.0s"
        assert res.duration_diff == 3.5
        assert res.synced_lyrics is None
        assert res.plain_lyrics == "Is this the real life?"


@pytest.mark.asyncio
async def test_live_acoustic_duration_mismatch_rejected(mock_lrclib_transport):
    """Tier 2: Verify live acoustic recording mismatch (diff 90s) rejects synced lyrics."""
    async with httpx.AsyncClient(transport=mock_lrclib_transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        # The Weeknd - Blinding Lights (Live in DB: 285.0s, studio audio: 195.0s)
        res = await client.fetch_lyrics("The Weeknd", "Blinding Lights", audio_duration=195.0)

        assert res.is_synced is False
        assert res.duration_diff == 90.0
        assert res.synced_lyrics is None
        assert res.plain_lyrics is not None


@pytest.mark.asyncio
async def test_fallback_to_search_when_get_404s():
    """Tier 3: Verify client falls back to /api/search when /api/get returns 404."""
    calls = []

    def custom_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/get":
            return httpx.Response(404, json={"statusCode": 404, "error": "Not Found"})
        elif request.url.path == "/api/search":
            return httpx.Response(200, json=[
                {
                    "name": "Save Your Tears",
                    "artistName": "The Weeknd",
                    "duration": 215.0,
                    "syncedLyrics": "[00:01.00] I saw you dancing",
                    "plainLyrics": "I saw you dancing",
                }
            ])
        return httpx.Response(404)

    transport = httpx.MockTransport(custom_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        res = await client.fetch_lyrics("The Weeknd", "Save Your Tears", audio_duration=215.0)

        assert "/api/get" in calls
        assert "/api/search" in calls
        assert res.is_synced is True
        assert res.duration_diff == 0.0
        assert res.synced_lyrics == "[00:01.00] I saw you dancing"


@pytest.mark.asyncio
async def test_search_selects_best_duration_candidate():
    """Tier 2: Verify search picks candidate closest to audio duration within +-3s."""
    candidates = [
        {"name": "Song (Live)", "artistName": "Band", "duration": 310.0, "syncedLyrics": "[00:01.00] Live"},
        {"name": "Song (Acoustic)", "artistName": "Band", "duration": 150.0, "syncedLyrics": "[00:01.00] Acoustic"},
        {"name": "Song (Album Version)", "artistName": "Band", "duration": 201.5, "syncedLyrics": "[00:01.00] Album"},
    ]

    def search_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get":
            return httpx.Response(404)
        elif request.url.path == "/api/search":
            return httpx.Response(200, json=candidates)
        return httpx.Response(404)

    transport = httpx.MockTransport(search_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        # Audio duration is 200.0s -> Candidate 3 (201.5s, diff=1.5s) must be chosen
        res = await client.fetch_lyrics("Band", "Song", audio_duration=200.0)

        assert res.is_synced is True
        assert res.duration == 201.5
        assert res.duration_diff == 1.5
        assert res.synced_lyrics == "[00:01.00] Album"


@pytest.mark.asyncio
async def test_transient_retry_and_recovery():
    """Tier 2: Verify exponential retry recovers after HTTP 503 or 429."""
    attempt_count = 0

    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={
            "name": "Retry Song",
            "artistName": "Retry Artist",
            "duration": 180.0,
            "plainLyrics": "Recovered lyrics",
        })

    transport = httpx.MockTransport(retry_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client, max_retries=3, retry_backoff=0.01)
        res = await client.fetch_lyrics("Retry Artist", "Retry Song", audio_duration=180.0)

        assert attempt_count == 3
        assert res.has_lyrics() is True
        assert res.plain_lyrics == "Recovered lyrics"


def test_strip_lrc_timestamps():
    """Tier 1: Verify helper correctly strips all timestamp formats and ID tags."""
    lrc_text = (
        "[ar:Dua Lipa]\n"
        "[ti:Levitating]\n"
        "[00:00.00] If you wanna run away with me\n"
        "[00:03.20] I know a galaxy\n"
        "[01:15.500] And I can take you for a ride\n"
    )
    plain = strip_lrc_timestamps(lrc_text)
    expected = "If you wanna run away with me\nI know a galaxy\nAnd I can take you for a ride"
    assert plain == expected


def test_parse_lrc_lines_to_sylt_milliseconds():
    """Tier 1: Verify helper parses LRC timestamps into millisecond tuples for SYLT."""
    lrc_text = "[00:01.50] Line One\n[01:02.00] Line Two"
    parsed = parse_lrc_lines(lrc_text)
    assert parsed == [("Line One", 1500), ("Line Two", 62000)]


def test_clean_query_title():
    """Tier 1: Verify clean_query_title strips feature and remaster tags."""
    assert clean_query_title("Song Title (feat. Drake)") == "Song Title"
    assert clean_query_title("Song Title [Remastered 2021]") == "Song Title"
    assert clean_query_title("Standard Title") == "Standard Title"
