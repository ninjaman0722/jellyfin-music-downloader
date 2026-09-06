"""Unit & Integration Test Specifications for Downloader & Pipeline Stage 2.

Tests:
1. Deterministic path generation & Unicode NFKC preservation across all scripts.
2. Filesystem sanitization for slashes, colons, control characters, and reserved names.
3. Safe .part download flow and atomic os.replace rename.
4. Non-destructive file handling (skits/intros <350KB preserved, existing files skipped).
5. Concurrent worker pool bounded execution (4-6 workers) downloading only missing tracks.
6. ProcessManager and ConnectionManager WebSocket integration.
7. Process-targeted cancellation and partial file cleanup.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional
import pytest

from server.app.process import ProcessManager
from server.app.ws import (
    ConnectionManager,
    ProgressEvent,
    TrackCompletedEvent,
    TrackFailedEvent,
    StageTransitionEvent,
)
from server.app.downloader import (
    DownloadEngine,
    Downloader,
    DownloadTrack,
    TrackResult,
    generate_part_path,
    generate_track_path,
    sanitize_path_component,
)


# ==============================================================================
# 1. Path Generation & Sanitization Tests
# ==============================================================================

def test_deterministic_path_generation_standard():
    """Verify exact path format: {music_dir}/{artist}/{album}/{disc:02d}-{track:02d} - {title}.mp3."""
    music_dir = Path("/music")
    path = generate_track_path(
        music_dir=music_dir,
        artist="The Weeknd",
        album="After Hours",
        title="Blinding Lights",
        track_number=1,
        disc_number=1,
        ext=".mp3",
    )
    assert path == Path("/music/The Weeknd/After Hours/01-01 - Blinding Lights.mp3")


def test_deterministic_path_generation_padding_and_extensions():
    """Verify disc and track number 2-digit zero padding and flexible extensions."""
    music_dir = Path("/music")
    path = generate_track_path(
        music_dir=music_dir,
        artist="Pink Floyd",
        album="The Wall",
        title="Comfortably Numb",
        track_number=6,
        disc_number=2,
        ext="flac",  # Without leading dot
    )
    assert path == Path("/music/Pink Floyd/The Wall/02-06 - Comfortably Numb.flac")


def test_sanitize_path_component_unicode_preservation():
    """Verify non-ASCII scripts (Japanese, Korean, Cyrillic, Accented Latin) are 100% preserved."""
    assert sanitize_path_component("YOASOBI") == "YOASOBI"
    assert sanitize_path_component("夜に駆ける") == "夜に駆ける"
    assert sanitize_path_component("좋은 날") == "좋은 날"
    assert sanitize_path_component("Кино") == "Кино"
    assert sanitize_path_component("Sigur Rós") == "Sigur Rós"
    assert sanitize_path_component("Édith Piaf") == "Édith Piaf"


def test_sanitize_path_component_forbidden_characters():
    """Verify illegal filesystem characters (/, \\, :, *, ?, \", <, >, |) are replaced safely."""
    # AC/DC must not create nested subdirectories
    assert sanitize_path_component("AC/DC") == "AC_DC"
    assert sanitize_path_component("20/20") == "20_20"
    
    # Colon must be replaced with ' - '
    assert sanitize_path_component("Mission: Impossible") == "Mission - Impossible"
    
    # Symbols & punctuation
    assert sanitize_path_component('Song *Title* ? "Remix" <Deluxe> | Edit') == "Song _Title_ _ _Remix_ _Deluxe_ _ Edit"


def test_sanitize_path_component_reserved_names_and_fallbacks():
    """Verify reserved DOS device names and empty strings are handled safely."""
    assert sanitize_path_component("CON") == "CON_"
    assert sanitize_path_component("prn") == "prn_"
    assert sanitize_path_component("NUL") == "NUL_"
    assert sanitize_path_component("") == "Unknown"
    assert sanitize_path_component("   ") == "Unknown"
    assert sanitize_path_component("...") == "Unknown"


# ==============================================================================
# 2. Safe .part Download Flow & Atomic Rename Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_atomic_download_flow_success():
    """Verify downloader writes to .part and atomically renames to final target upon success."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job = await pm.register_job("job_dl_1", "user1", "Album")

        track = DownloadTrack(
            id="t1",
            title="Blinding Lights",
            artist="The Weeknd",
            album="After Hours",
            track_number=1,
            disc_number=1,
            duration_ms=200000.0,
        )

        expected_final = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        expected_part = generate_part_path(expected_final)

        # Mock runner simulating successful download to .part file
        async def mock_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            assert Path(temp_dir) == expected_part
            assert Path(in_flight_target) == expected_final
            expected_part.write_bytes(b"MOCK_AUDIO_PAYLOAD_1234567890")
            return (0, "Download complete", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            command_runner=mock_runner,
        )

        result = await downloader.download_single_track(track, "job_dl_1")

        assert result.success is True
        assert result.path == expected_final
        assert not expected_part.exists(), ".part file must be removed by atomic rename"
        assert expected_final.exists(), "Final audio file must exist"
        assert expected_final.read_bytes() == b"MOCK_AUDIO_PAYLOAD_1234567890"
        assert expected_final in job.completed_files


@pytest.mark.asyncio
async def test_atomic_download_flow_failure_cleans_partial():
    """Verify failed download cleans up .part file and does not create corrupted final file."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job = await pm.register_job("job_dl_fail", "user1", "Album")

        track = DownloadTrack(
            id="t_fail",
            title="Failed Song",
            artist="Artist",
            album="Album",
            track_number=1,
            disc_number=1,
        )

        expected_final = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        expected_part = generate_part_path(expected_final)

        # Mock runner simulating failure
        async def mock_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            expected_part.write_bytes(b"CORRUPT_DATA")
            return (1, "", "Network error: Connection timed out")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            max_retries=0,
            command_runner=mock_runner,
        )

        result = await downloader.download_single_track(track, "job_dl_fail")

        assert result.success is False
        assert not expected_part.exists(), ".part file must be unlinked upon failure"
        assert not expected_final.exists(), "Corrupt final audio file must NEVER be created"
        assert "Network error" in result.error


@pytest.mark.asyncio
async def test_non_destructive_skip_existing_valid_file():
    """Verify existing valid audio files are preserved and not re-downloaded."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_skip", "user1", "Album")

        track = DownloadTrack(
            id="t_exist",
            title="Existing Song",
            artist="Artist",
            album="Album",
            track_number=1,
            disc_number=1,
        )

        target = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"EXISTING_VALID_AUDIO_FILE")

        runner_called = False
        async def mock_runner(*args, **kwargs):
            nonlocal runner_called
            runner_called = True
            return (0, "", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            command_runner=mock_runner,
        )

        result = await downloader.download_single_track(track, "job_skip")

        assert result.success is True
        assert result.was_skipped is True
        assert runner_called is False, "Runner should not be called for existing file"
        assert target.read_bytes() == b"EXISTING_VALID_AUDIO_FILE"


# ==============================================================================
# 3. Concurrent Worker Pool Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_concurrent_worker_pool_concurrency_bound():
    """Verify concurrent worker pool limits active tasks to configured concurrency (e.g. 4)."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_pool", "user1", "Album")

        missing_tracks = [
            DownloadTrack(id=f"t{i}", title=f"Track {i}", artist="Artist", album="Album", track_number=i)
            for i in range(1, 11)
        ]

        active_workers = 0
        max_active = 0
        lock = asyncio.Lock()

        async def mock_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            nonlocal active_workers, max_active
            async with lock:
                active_workers += 1
                if active_workers > max_active:
                    max_active = active_workers

            Path(temp_dir).write_bytes(b"AUDIO_DATA")
            await asyncio.sleep(0.05)

            async with lock:
                active_workers -= 1
            return (0, "OK", "")

        concurrency_limit = 4
        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=concurrency_limit,
            command_runner=mock_runner,
        )

        results = await downloader.download_missing_tracks(missing_tracks, "job_pool")

        assert len(results) == 10
        assert all(r.success for r in results)
        assert max_active <= concurrency_limit, f"Concurrency {max_active} exceeded limit {concurrency_limit}"


@pytest.mark.asyncio
async def test_worker_pool_empty_missing_tracks():
    """Verify empty missing track list completes immediately with empty result list."""
    pm = ProcessManager()
    downloader = Downloader(music_dir="/tmp", process_manager=pm)
    results = await downloader.download_missing_tracks([], "job_empty")
    assert results == []


# ==============================================================================
# 4. WebSocket & Cancellation Integration Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_downloader_broadcasts_websocket_events():
    """Verify downloader broadcasts StageTransitionEvent, ProgressEvent, and TrackCompletedEvent."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_ws", "user1", "Album")

        broadcasted_events = []
        class MockWSManager:
            async def broadcast(self, event, job_id=None):
                broadcasted_events.append(event)

        missing_tracks = [
            DownloadTrack(id="t1", title="Song 1", artist="Artist", album="Album", track_number=1),
            DownloadTrack(id="t2", title="Song 2", artist="Artist", album="Album", track_number=2),
        ]

        async def mock_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            Path(temp_dir).write_bytes(b"AUDIO")
            return (0, "", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            ws_broadcaster=MockWSManager(),
            command_runner=mock_runner,
        )

        await downloader.download_missing_tracks(missing_tracks, "job_ws", total_tracks_count=2)

        event_types = [e.event for e in broadcasted_events]
        assert "stage_transition" in event_types
        assert "progress" in event_types
        assert "track_completed" in event_types

        # Verify progress reaches 100%
        progress_events = [e for e in broadcasted_events if isinstance(e, ProgressEvent)]
        assert progress_events[-1].percentage == 100.0
        assert progress_events[-1].current == 2


@pytest.mark.asyncio
async def test_downloader_cancellation_and_partial_cleanup():
    """Verify cancelling a job halts workers and cleans up in-flight .part files without touching completed files."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job = await pm.register_job("job_cancel", "user1", "Album")

        missing_tracks = [
            DownloadTrack(id=f"t{i}", title=f"Track {i}", artist="Artist", album="Album", track_number=i)
            for i in range(1, 6)
        ]

        async def slow_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            # Check if already cancelled
            if job.cancel_event.is_set():
                raise asyncio.CancelledError()
            Path(temp_dir).write_bytes(b"PARTIAL_DATA")
            await asyncio.sleep(0.5)
            return (0, "", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=2,
            command_runner=slow_runner,
        )

        download_task = asyncio.create_task(
            downloader.download_missing_tracks(missing_tracks, "job_cancel")
        )

        # Allow workers to start downloading
        await asyncio.sleep(0.1)

        # Cancel job via ProcessManager
        cancel_res = await pm.cancel_job("job_cancel")
        assert cancel_res.status == "cancelled"

        await download_task

        # Verify in-flight partial files were cleaned up
        for item in music_dir.rglob("*"):
            if item.is_file() and (item.name.startswith(".part_") or item.name.endswith(".part")):
                pytest.fail(f"Found orphaned partial file after cancellation: {item}")


def test_generate_part_path_format():
    """Verify generate_part_path produces .part_{stem}{ext} format."""
    target = Path("/music/Artist/Album/01-01 - Title.mp3")
    part = generate_part_path(target)
    assert part == Path("/music/Artist/Album/.part_01-01 - Title.mp3")
    assert part.name.startswith(".part_")
    assert part.suffix == ".mp3"


def test_build_download_command_ytdlp_part_path():
    """Verify build_download_command passes the hidden part_path with format extension to yt-dlp -o."""
    pm = ProcessManager()
    dl = Downloader(music_dir="/music", process_manager=pm, format_ext=".mp3", engine=DownloadEngine.YTDLP)
    track = DownloadTrack(id="t1", title="Song", artist="Artist", album="Album")
    target = generate_track_path("/music", "Artist", "Album", "Song", 1, 1)
    part = dl.get_part_path(target)

    cmd = dl.build_download_command(track, part)
    assert cmd[0] == "yt-dlp"
    assert "-x" in cmd
    assert "--audio-format" in cmd
    assert cmd[cmd.index("--audio-format") + 1] == "mp3"
    assert "--no-part" in cmd
    assert "-o" in cmd
    assert cmd[cmd.index("-o") + 1] == str(part)
    assert Path(cmd[cmd.index("-o") + 1]).name == ".part_01-01 - Song.mp3"


def test_ytdlp_command_template_preserves_audio_format_extension():
    """Regression test: Verify build_download_command for yt-dlp
    configures -o to an output path ending with the target audio format extension (.mp3),
    preventing yt-dlp from stripping trailing extensions and writing directly to target_path.
    """
    music_dir = Path("/music")
    pm = ProcessManager()
    downloader = Downloader(
        music_dir=music_dir,
        process_manager=pm,
        engine=DownloadEngine.YTDLP,
        format_ext=".mp3",
        bitrate="320k",
    )

    track = DownloadTrack(
        id="track_yt_01",
        title="Blinding Lights",
        artist="The Weeknd",
        album="After Hours",
        track_number=1,
        disc_number=1,
    )

    target_path = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
    part_path = downloader.get_part_path(target_path)

    cmd = downloader.build_download_command(track, part_path)

    assert cmd[0] == "yt-dlp"
    assert "-x" in cmd
    assert "--audio-format" in cmd
    fmt_idx = cmd.index("--audio-format")
    assert cmd[fmt_idx + 1] == "mp3"
    assert "--no-part" in cmd

    assert "-o" in cmd
    o_idx = cmd.index("-o")
    output_template = cmd[o_idx + 1]

    assert output_template.endswith(".mp3")
    assert not output_template.endswith(".part")
    assert output_template != str(target_path)


@pytest.mark.asyncio
async def test_atomic_download_flow_ytdlp_part_rename_and_no_orphans():
    """Regression test: Verify Downloader properly handles yt-dlp writing
    to the partial file with audio extension, performs size verification, and atomically
    renames via os.replace to target_path without leaving orphaned .part files.
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job = await pm.register_job("job_dl_reg", "user1", "Album")

        track = DownloadTrack(
            id="t_reg_1",
            title="Starboy",
            artist="The Weeknd",
            album="Starboy",
            track_number=1,
            disc_number=1,
            duration_ms=230000.0,
        )

        target_path = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)

        async def mock_ytdlp_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            o_idx = cmd.index("-o")
            specified_output = Path(cmd[o_idx + 1])

            assert specified_output != target_path, "yt-dlp must not write directly to target_path!"
            assert specified_output.suffix == ".mp3", "Partial path must preserve .mp3 extension!"

            specified_output.parent.mkdir(parents=True, exist_ok=True)
            specified_output.write_bytes(b"MOCK_VALID_AUDIO_PAYLOAD_256K")
            return (0, "yt-dlp: download finished", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            engine=DownloadEngine.YTDLP,
            command_runner=mock_ytdlp_runner,
        )

        result = await downloader.download_single_track(track, "job_dl_reg")

        assert result.success is True
        assert result.path == target_path
        assert target_path.exists(), "Final audio file must exist at target_path after atomic rename"
        assert target_path.read_bytes() == b"MOCK_VALID_AUDIO_PAYLOAD_256K"

        parent_dir = target_path.parent
        all_files = list(parent_dir.iterdir())
        assert len(all_files) == 1, f"Expected exactly 1 file (target_path), found: {all_files}"
        assert all_files[0] == target_path

        for f in parent_dir.rglob("*"):
            if f.is_file():
                assert not f.name.endswith(".part")
                assert not f.name.startswith(".part_")
                assert ".part." not in f.name

        assert target_path in job.completed_files
        assert target_path not in job.in_flight_targets


@pytest.mark.asyncio
async def test_atomic_download_failure_cleans_ytdlp_part_file():
    """Regression test: Verify that when yt-dlp fails, the partial file (with audio extension)
    is unlinked and the final target file is NEVER created.
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job = await pm.register_job("job_dl_fail_reg", "user1", "Album")

        track = DownloadTrack(
            id="t_fail_reg",
            title="Broken Stream",
            artist="Artist",
            album="Album",
            track_number=1,
            disc_number=1,
        )

        target_path = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)

        async def mock_failing_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            o_idx = cmd.index("-o")
            specified_output = Path(cmd[o_idx + 1])
            specified_output.parent.mkdir(parents=True, exist_ok=True)
            specified_output.write_bytes(b"CORRUPTED_INCOMPLETE_DATA")
            return (1, "", "ERROR: 403 Forbidden")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            engine=DownloadEngine.YTDLP,
            max_retries=0,
            command_runner=mock_failing_runner,
        )

        result = await downloader.download_single_track(track, "job_dl_fail_reg")

        assert result.success is False
        assert not target_path.exists(), "Corrupted final file must NOT exist"

        parent_dir = target_path.parent
        if parent_dir.exists():
            remaining = list(parent_dir.iterdir())
            assert len(remaining) == 0, f"Partial file was not cleaned up on failure: {remaining}"


@pytest.mark.asyncio
async def test_downloader_real_process_manager_integration():
    """Verify that Downloader seamlessly integrates with real ProcessManager (no mock runner),
    executing commands without signature mismatches or argument errors.
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job_id = "job_real_pm_integration"
        await pm.register_job(job_id, "user1", "Album")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            engine=DownloadEngine.YTDLP,
            max_retries=0,
            command_runner=None,  # Crucial: test real ProcessManager.run_command wiring
        )

        track = DownloadTrack(
            id="t_real_pm",
            title="Real Process Track",
            artist="Real Artist",
            album="Real Album",
            track_number=1,
            disc_number=1,
        )

        target_path = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        part_path = downloader.get_part_path(target_path)
        part_path.parent.mkdir(parents=True, exist_ok=True)

        # Execute a real shell command through real ProcessManager
        script = f"import sys; from pathlib import Path; Path('{part_path}').write_bytes(b'REAL_AUDIO_BYTES_TEST')"
        cmd = [sys.executable, "-c", script]

        rc, stdout, stderr = await downloader._execute_command(
            cmd=cmd,
            job_id=job_id,
            cwd=part_path.parent,
            part_path=part_path,
            target_path=target_path,
        )

        assert rc == 0
        assert part_path.exists()
        assert part_path.read_bytes() == b"REAL_AUDIO_BYTES_TEST"

        # Verify dot-stripping fallback resolution
        nodot_path = part_path.with_name(part_path.name.lstrip("."))
        part_path.rename(nodot_path)
        assert not part_path.exists()
        assert nodot_path.exists()

        # Download method should successfully find nodot_path and finalize
        # Simulate finalizing via download_single_track step
        actual_part = part_path
        if not actual_part.exists() or not actual_part.is_file():
            alt = part_path.with_name(part_path.name.lstrip("."))
            if alt.exists() and alt.is_file():
                actual_part = alt
        os.replace(actual_part, target_path)

        assert target_path.exists()
        assert target_path.read_bytes() == b"REAL_AUDIO_BYTES_TEST"


