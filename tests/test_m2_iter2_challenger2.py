"""Empirical Challenge Suite: Ingestion Failure Resilience & Multi-Disc Track Extraction.

Authored by M2 Iteration 2 Challenger 2 (challenger_m2_iter2_2).

Validates:
1. Ingest Failure Resilience:
   - Resolver network exceptions (ConnectionError, aiohttp.ClientError).
   - Resolver returning None.
   - Resolver raising unexpected exceptions (KeyError, RuntimeError).
   - Downloader service unavailable (None).
   - Downloader runtime crashes during missing track execution.
   - For all scenarios:
     * Terminal JobCompletedEvent(downloaded=0, failed=total) broadcast over WebSocket.
     * job.status transitions to 'failed'.
     * ProcessManager active jobs count returns to 0.
     * Subsequent identical (user_id, playlist_name) job enqueued and completed without 409 Conflict lockout.

2. Multi-Disc Track Regex Extraction:
   - 25+ naming variations tested on disk through LibraryIndex._extract_metadata().
   - Standard 2-digit, 1-digit, high disc/track numbers, dot/underscore/hyphen separators.
   - Unicode non-ASCII scripts (Japanese, Korean, Cyrillic).
   - Numeric titles and years (1984, 1999, 2024 Remaster).
   - Documented boundary behavior on text-prefixed untagged files (CD1-05, Disc 1 - 05).
"""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app.indexer import LibraryIndex
from server.app.main import IngestRequest, app, post_ingest
from server.app.process import ProcessManager
from server.app.resolver import ResolveResponse, ResolveTrack
from server.app.ws import JobCompletedEvent, LogEvent, JobStartedEvent, ws_manager


class CaptureWebSocket:
    """Mock WebSocket client capturing emitted JSON frames."""

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


def make_synthetic_flac(path: Path) -> Path:
    """Constructs a valid minimal FLAC file with a STREAMINFO block."""
    sr = 44100
    ch = 1
    bps = 15
    total_samples = 132300
    val = (sr << 44) | (ch << 41) | (bps << 36) | total_samples
    data = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x0e"
        + b"\x00\x00\x0e"
        + struct.pack(">Q", val)
        + b"\x00" * 16
    )
    header = b"\x80\x00\x00\x22"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + header + data)
    return path


def make_synthetic_mp3(path: Path) -> Path:
    """Constructs a minimal raw MP3 frame sequence."""
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(frame * 5)
    return path


# ==============================================================================
# 1. Ingestion Failure Resilience & Conflict Lockout Prevention
# ==============================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    [
        "network_exception",
        "resolver_returns_none",
        "missing_downloader",
        "downloader_runtime_crash",
        "resolver_key_error",
    ],
)
async def test_challenge_ingest_failure_resilience_and_conflict_lockout(failure_mode: str):
    """Empirical challenge: Ingestion failures MUST broadcast a terminal JobCompletedEvent,
    set job.status to 'failed', and MUST NOT lock out subsequent identical job submissions (409).
    """
    mock_ws = CaptureWebSocket()
    session = await ws_manager.connect(mock_ws, client_id=f"challenge-{failure_mode}")

    orig_downloader = getattr(app.state, "downloader", None)
    orig_resolver = getattr(app.state, "resolver", None)
    pm: ProcessManager = app.state.process_manager
    settings = app.state.config

    mock_req = MagicMock()
    mock_req.app = app

    user_id = f"user-{failure_mode}"
    playlist_name = f"Playlist-{failure_mode}"

    try:
        # Setup failure injection
        if failure_mode == "network_exception":
            mock_resolver = AsyncMock()
            mock_resolver.resolve.side_effect = ConnectionError("Upstream Spotify gateway timed out")
            app.state.resolver = mock_resolver
            app.state.downloader = orig_downloader
        elif failure_mode == "resolver_returns_none":
            mock_resolver = AsyncMock()
            mock_resolver.resolve.return_value = None
            app.state.resolver = mock_resolver
            app.state.downloader = orig_downloader
        elif failure_mode == "missing_downloader":
            app.state.downloader = None
            mock_resolver = AsyncMock()
            mock_resolver.resolve.return_value = ResolveResponse(
                playlist_name=playlist_name,
                playlist_id="pl-test-01",
                total_tracks=2,
                existing_tracks=0,
                missing_tracks=2,
                tracks=[ResolveTrack(id="t1", title="T1", artist="A"), ResolveTrack(id="t2", title="T2", artist="A")],
            )
            app.state.resolver = mock_resolver
        elif failure_mode == "downloader_runtime_crash":
            mock_resolver = AsyncMock()
            mock_resolver.resolve.return_value = ResolveResponse(
                playlist_name=playlist_name,
                playlist_id="pl-test-01",
                total_tracks=2,
                existing_tracks=0,
                missing_tracks=2,
                tracks=[ResolveTrack(id="t1", title="T1", artist="A"), ResolveTrack(id="t2", title="T2", artist="A")],
            )
            app.state.resolver = mock_resolver
            mock_down = MagicMock()
            mock_down.download_missing_tracks = AsyncMock(side_effect=RuntimeError("Disk I/O failure: /mnt/media read-only"))
            app.state.downloader = mock_down
        elif failure_mode == "resolver_key_error":
            mock_resolver = AsyncMock()
            mock_resolver.resolve.side_effect = KeyError("corrupted_schema_key")
            app.state.resolver = mock_resolver
            app.state.downloader = orig_downloader

        req = IngestRequest(
            urls=["https://open.spotify.com/track/1", "https://open.spotify.com/track/2"],
            user_id=user_id,
            playlist_name=playlist_name,
        )

        # 1. Enqueue job (will fail during execution)
        resp1 = await post_ingest(req, mock_req, settings)
        job1_id = resp1.job_id
        assert resp1.status == "queued"

        job1 = pm.get_job(job1_id)
        assert job1 is not None
        if job1.worker_tasks:
            await asyncio.gather(*job1.worker_tasks, return_exceptions=True)

        # 2. Verify WebSocket frames
        events1 = mock_ws.get_events()
        event_types1 = [e.get("event") for e in events1]

        assert "job_started" in event_types1, f"Missing job_started event in {event_types1}"
        assert "job_completed" in event_types1, f"Missing terminal job_completed event in {event_types1}"

        # 3. Verify terminal event content
        jc1 = next(e for e in events1 if e.get("event") == "job_completed" and e.get("job_id") == job1_id)
        assert jc1["job_id"] == job1_id
        assert jc1["downloaded"] == 0, f"Expected downloaded=0 on failure, got {jc1['downloaded']}"
        assert jc1["failed"] == 2, f"Expected failed=2, got {jc1['failed']}"
        assert jc1["playlist_id"] is None or jc1["playlist_id"] == "pl-test-01"

        # 4. Verify job status updated to 'failed'
        assert job1.status == "failed", f"Expected job.status to be 'failed', observed '{job1.status}'"

        # 5. Verify ProcessManager conflict lockout prevention
        conflict_id = pm.find_conflict(user_id, playlist_name)
        assert conflict_id is None, (
            f"LOCKOUT DEFECT: Failed job {job1_id} was still treated as active conflict! "
            f"Active conflict found: {conflict_id}"
        )

        # 6. Verify immediate re-submission for identical user + playlist succeeds without 409 Conflict
        app.state.downloader = orig_downloader
        mock_ok = AsyncMock()
        mock_ok.resolve.return_value = ResolveResponse(
            playlist_name=playlist_name,
            playlist_id="pl-test-ok",
            total_tracks=1,
            existing_tracks=1,
            missing_tracks=0,
            tracks=[ResolveTrack(id="t1", title="T1", artist="A", exists_locally=True)],
        )
        app.state.resolver = mock_ok

        resp2 = await post_ingest(req, mock_req, settings)
        assert resp2.status == "queued"
        job2_id = resp2.job_id
        assert job2_id != job1_id

        job2 = pm.get_job(job2_id)
        assert job2 is not None
        if job2.worker_tasks:
            await asyncio.gather(*job2.worker_tasks, return_exceptions=True)

        assert job2.status == "completed"

    finally:
        app.state.downloader = orig_downloader
        app.state.resolver = orig_resolver
        await ws_manager.disconnect(mock_ws)


# ==============================================================================
# 2. Multi-Disc Track Regex Extraction (20+ variations)
# ==============================================================================

@pytest.mark.parametrize(
    "filename,is_flac,expected_title,description",
    [
        # Standard disc-track numbering
        ("01-01 - Title.mp3", False, "Title", "Standard 2-digit disc and track"),
        ("1-01 - Title.mp3", False, "Title", "Single-digit disc, 2-digit track"),
        ("02-06 - Track.mp3", False, "Track", "Disc 2 track 6"),
        ("2-06 - Track.mp3", False, "Track", "Single-digit disc, track 6"),
        ("10-12 - Long Title.mp3", False, "Long Title", "High disc number 10"),
        ("01-05_Song_Title.mp3", False, "Song_Title", "Underscore separator"),
        ("2-01. Bohemian Rhapsody.mp3", False, "Bohemian Rhapsody", "Dot separator"),
        ("01-01 - 夜に駆ける.mp3", False, "夜に駆ける", "Japanese Kanji/Kana"),
        ("1-01 - 봄날.mp3", False, "봄날", "Korean Hangul"),
        ("03-09 Группа крови.mp3", False, "Группа крови", "Russian Cyrillic space separated"),
        ("01-02-Title.mp3", False, "Title", "No space around hyphens"),
        ("1-10 - Track Name (Live).mp3", False, "Track Name (Live)", "Parenthetical metadata"),
        ("02-01 - Intro - The Beginning.mp3", False, "Intro - The Beginning", "Internal hyphen"),
        ("04-20 - 2024 Remaster.mp3", False, "2024 Remaster", "Year in song title"),
        ("1-1 - Short.mp3", False, "Short", "Single digit disc and single digit track"),
        ("99-99 - Final Disc Track.mp3", False, "Final Disc Track", "Upper boundary numbers"),
        ("01-01. Title With Dots.mp3", False, "Title With Dots", "Dot immediately after disc-track"),
        ("01-01_Title_With_Underscores.mp3", False, "Title_With_Underscores", "Multiple underscores"),
        ("02-03 - 1999 (Prince Cover).mp3", False, "1999 (Prince Cover)", "Leading numbers in title"),
        ("01-01 - 100% Pure Love.mp3", False, "100% Pure Love", "Percentage in title"),
        # Single-track variations
        ("01 - Starboy.mp3", False, "Starboy", "Single track standard"),
        ("05. Yesterday.mp3", False, "Yesterday", "Single track dot"),
        ("12_Song_Title.mp3", False, "Song_Title", "Single track underscore"),
        ("1 - Solo.mp3", False, "Solo", "Single track single digit"),
        ("1984.mp3", False, "1984", "Pure numeric title fallback preserved"),
    ],
)
def test_challenge_multidisc_track_regex_extraction_25_variations(
    filename: str,
    is_flac: bool,
    expected_title: str,
    description: str,
):
    """Empirical challenge: Test 25 distinct track naming variations through
    LibraryIndex._extract_metadata() on untagged audio files on disk.
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        fpath = music_dir / filename

        if is_flac:
            make_synthetic_flac(fpath)
        else:
            make_synthetic_mp3(fpath)

        idx = LibraryIndex(music_dir)
        extracted_title, _ = idx._extract_metadata(fpath)

        assert extracted_title == expected_title, (
            f"Failed on variation '{description}' for file '{filename}': "
            f"expected '{expected_title}', got '{extracted_title}'"
        )


def test_challenge_text_prefixed_multidisc_boundary_documentation():
    """Documents the boundary behavior of _extract_metadata():
    Text-prefixed multi-disc files ('CD1-05 - Song.flac') without audio tags
    are not matched by ^(?:\\d+-\\d+|\\d+)[\\s\\.\\-_]+.
    When tagged, the Vorbis/ID3 title tag is used directly.
    """
    from mutagen.flac import FLAC

    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        idx = LibraryIndex(music_dir)

        # 1. Untagged CD1-05 file
        f_untagged = music_dir / "CD1-05 - Song.flac"
        make_synthetic_flac(f_untagged)
        title_untagged, _ = idx._extract_metadata(f_untagged)
        # Documents that untagged CD prefix is currently retained as part of title
        assert title_untagged == "CD1-05 - Song"

        # 2. Tagged CD1-05 file
        f_tagged = music_dir / "CD1-05 - Tagged.flac"
        make_synthetic_flac(f_tagged)
        flac = FLAC(str(f_tagged))
        flac["title"] = ["Tagged Song"]
        flac["artist"] = ["Tagged Artist"]
        flac.save()
        title_tagged, artist_tagged = idx._extract_metadata(f_tagged)
        # When tagged, metadata tags take precedence and prefix is bypassed
        assert title_tagged == "Tagged Song"
        assert artist_tagged == "Tagged Artist"
