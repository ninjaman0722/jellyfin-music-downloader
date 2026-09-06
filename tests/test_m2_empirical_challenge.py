"""Empirical Challenge Harness for Milestone 2: Concurrency, Atomic .part Flow, Sibling Isolation, LRCLIB Boundaries, and UTF-8 Tagging.

Authored by Empirical Challenger 2 (challenger_m2_2).
"""

from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional
import httpx
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
import pytest

from server.app.process import ProcessManager
from server.app.downloader import (
    Downloader,
    DownloadTrack,
    TrackResult,
    generate_track_path,
    sanitize_path_component,
)
from server.app.lyrics import (
    LRCLIBClient,
    LyricsResult,
    parse_lrc_lines,
    strip_lrc_timestamps,
)
from server.app.tagger import (
    AudioTagger,
    TrackMetadata,
)


def make_synthetic_flac(path: Path) -> Path:
    """Constructs a valid minimal FLAC file with a 34-byte STREAMINFO block."""
    sr = 44100
    ch = 1  # 2 channels - 1
    bps = 15  # 16 bits - 1
    total_samples = 132300  # 3 seconds
    val = (sr << 44) | (ch << 41) | (bps << 36) | total_samples

    data = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x0e"
        + b"\x00\x00\x0e"
        + struct.pack(">Q", val)
        + b"\x00" * 16
    )
    header = b"\x80\x00\x00\x22"  # is_last=1, type=0 (STREAMINFO), len=34
    flac_bytes = b"fLaC" + header + data

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(flac_bytes)
    return path


# ==============================================================================
# Challenge Suite 1: Concurrent Worker Pool Bounds (4-6 workers)
# ==============================================================================

@pytest.mark.asyncio
async def test_challenge_concurrency_bounds_4_workers():
    """Empirical challenge: 4-worker pool with 24 tracks MUST NEVER exceed 4 active concurrent tasks."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_c4", "user1", "Album")

        tracks = [
            DownloadTrack(id=f"t{i}", title=f"Track {i}", artist="Artist", album="Album", track_number=i)
            for i in range(1, 25)
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

            # Simulate I/O work
            Path(temp_dir).write_bytes(b"AUDIO_DATA")
            await asyncio.sleep(0.04)

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

        results = await downloader.download_missing_tracks(tracks, "job_c4")

        assert len(results) == 24
        assert all(r.success for r in results)
        assert max_active == 4, f"Expected exactly 4 concurrent workers utilized under load, observed {max_active}"
        assert max_active <= concurrency_limit, f"Max concurrent workers {max_active} exceeded limit {concurrency_limit}"


@pytest.mark.asyncio
async def test_challenge_concurrency_bounds_6_workers():
    """Empirical challenge: 6-worker pool with 30 tracks MUST NEVER exceed 6 active concurrent tasks."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_c6", "user1", "Album")

        tracks = [
            DownloadTrack(id=f"t{i}", title=f"Track {i}", artist="Artist", album="Album", track_number=i)
            for i in range(1, 31)
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
            await asyncio.sleep(0.03)

            async with lock:
                active_workers -= 1
            return (0, "OK", "")

        concurrency_limit = 6
        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=concurrency_limit,
            command_runner=mock_runner,
        )

        results = await downloader.download_missing_tracks(tracks, "job_c6")

        assert len(results) == 30
        assert all(r.success for r in results)
        assert max_active == 6, f"Expected exactly 6 concurrent workers utilized under load, observed {max_active}"
        assert max_active <= concurrency_limit, f"Max concurrent workers {max_active} exceeded limit {concurrency_limit}"


@pytest.mark.asyncio
async def test_challenge_concurrency_fewer_tracks_than_workers():
    """Verify that when missing tracks < concurrency, exactly len(missing_tracks) workers are spawned."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_c_few", "user1", "Album")

        tracks = [
            DownloadTrack(id="t1", title="Track 1", artist="Artist", album="Album", track_number=1),
            DownloadTrack(id="t2", title="Track 2", artist="Artist", album="Album", track_number=2),
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
            await asyncio.sleep(0.04)

            async with lock:
                active_workers -= 1
            return (0, "OK", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=6,  # 6 allowed, but only 2 tracks
            command_runner=mock_runner,
        )

        results = await downloader.download_missing_tracks(tracks, "job_c_few")
        assert len(results) == 2
        assert max_active <= 2, f"Spawned {max_active} workers for 2 tracks, should be <= 2"


@pytest.mark.asyncio
async def test_challenge_download_tracks_generator_concurrency():
    """Verify download_tracks() async generator strictly bounds concurrency to queue_size."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_c_gen", "user1", "Album")

        tracks = [
            DownloadTrack(id=f"t{i}", title=f"Track {i}", artist="Artist", album="Album", track_number=i)
            for i in range(1, 21)
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
            await asyncio.sleep(0.03)

            async with lock:
                active_workers -= 1
            return (0, "OK", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=5,
            command_runner=mock_runner,
        )

        yielded_results = []
        async for res in downloader.download_tracks(tracks, "job_c_gen", queue_size=5):
            yielded_results.append(res)

        assert len(yielded_results) == 20
        assert all(r.success for r in yielded_results)
        assert max_active <= 5, f"Generator exceeded concurrency limit 5: {max_active}"


def test_challenge_downloader_concurrency_clamping():
    """Verify Downloader concurrency is safely clamped to [1, 16]."""
    pm = ProcessManager()
    assert Downloader("/tmp", pm, concurrency=0).concurrency == 1
    assert Downloader("/tmp", pm, concurrency=-10).concurrency == 1
    assert Downloader("/tmp", pm, concurrency=100).concurrency == 16
    assert Downloader("/tmp", pm, concurrency=4).concurrency == 4
    assert Downloader("/tmp", pm, concurrency=6).concurrency == 6


# ==============================================================================
# Challenge Suite 2: Atomic .part Download Flow Under Simulated Interruption
# ==============================================================================

@pytest.mark.asyncio
async def test_challenge_atomic_part_simulated_sigkill():
    """Simulate SIGKILL (-9 / 137) during download: partial .part MUST be deleted, final audio file MUST NOT exist."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_sigkill", "user1", "Album")

        track = DownloadTrack(id="t1", title="Killed Track", artist="Artist", album="Album", track_number=1)
        target = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        part = target.with_name(f".part_{target.stem}.mp3")

        async def killed_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            # Runner writes partial corrupt bytes before dying
            part.write_bytes(b"CORRUPT_BYTES_KILLED_AT_50_PERCENT")
            return (137, "", "Process killed by SIGKILL")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            max_retries=0,
            command_runner=killed_runner,
        )

        result = await downloader.download_single_track(track, "job_sigkill")

        assert result.success is False
        assert not part.exists(), "In-flight .part file must be cleaned up on failure"
        assert not target.exists(), "Final audio file must NEVER exist for failed/killed download"


@pytest.mark.asyncio
async def test_challenge_atomic_part_zero_byte_exit_zero():
    """Runner exits with rc=0 but wrote 0 bytes: MUST NOT rename 0-byte file to final audio path."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_zero_byte", "user1", "Album")

        track = DownloadTrack(id="t1", title="Zero Byte Track", artist="Artist", album="Album", track_number=1)
        target = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        part = target.with_name(f".part_{target.stem}.mp3")

        async def zero_byte_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            part.write_bytes(b"")  # 0 bytes
            return (0, "Success", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            max_retries=1,
            command_runner=zero_byte_runner,
        )

        result = await downloader.download_single_track(track, "job_zero_byte")

        assert result.success is False
        assert not part.exists(), ".part file must be unlinked"
        assert not target.exists(), "Final audio file must NEVER be created with 0 bytes"
        assert "empty or missing" in result.error


@pytest.mark.asyncio
async def test_challenge_atomic_part_cancellation_during_active_write():
    """Cancel during active write: .part MUST be cleaned up and CancelledError propagated."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_cancel_active", "user1", "Album")

        track = DownloadTrack(id="t1", title="Cancelling Track", artist="Artist", album="Album", track_number=1)
        target = generate_track_path(music_dir, track.artist, track.album, track.title, 1, 1)
        part = target.with_name(f".part_{target.stem}.mp3")

        async def slow_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            part.write_bytes(b"WRITING_HALF_SONG...")
            await asyncio.sleep(5.0)
            return (0, "", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            command_runner=slow_runner,
        )

        task = asyncio.create_task(downloader.download_single_track(track, "job_cancel_active"))
        await asyncio.sleep(0.05)  # Let it start writing
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert not part.exists(), ".part file MUST be unlinked upon CancelledError"
        assert not target.exists(), "Final audio file MUST NOT exist upon cancellation"


# ==============================================================================
# Challenge Suite 3: Sibling File & Sibling .part Isolation
# ==============================================================================

@pytest.mark.asyncio
async def test_challenge_sibling_part_isolation_concurrent_album():
    """Two concurrent downloads in the same album folder: Worker 1 failure MUST NOT affect Worker 2's .part file."""
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_sibling", "user1", "Album")

        track_fail = DownloadTrack(id="t_fail", title="Fail Song", artist="SameArtist", album="SameAlbum", track_number=1)
        track_pass = DownloadTrack(id="t_pass", title="Pass Song", artist="SameArtist", album="SameAlbum", track_number=2)

        target_fail = generate_track_path(music_dir, track_fail.artist, track_fail.album, track_fail.title, 1, 1)
        target_pass = generate_track_path(music_dir, track_pass.artist, track_pass.album, track_pass.title, 2, 1)

        part_fail = target_fail.with_name(f".part_{target_fail.stem}.mp3")
        part_pass = target_pass.with_name(f".part_{target_pass.stem}.mp3")

        pass_worker_saw_own_part = False

        async def selective_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            nonlocal pass_worker_saw_own_part
            if in_flight_target == target_fail:
                part_fail.write_bytes(b"FAIL_DATA")
                await asyncio.sleep(0.02)
                return (1, "", "Network error")
            else:
                part_pass.write_bytes(b"PASS_DATA_AUDIO_VALID")
                # Wait until fail worker has completed and cleaned up
                await asyncio.sleep(0.10)
                # Verify our own .part file is STILL intact
                if part_pass.exists() and part_pass.read_bytes() == b"PASS_DATA_AUDIO_VALID":
                    pass_worker_saw_own_part = True
                return (0, "OK", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=2,
            max_retries=0,
            command_runner=selective_runner,
        )

        results = await downloader.download_missing_tracks([track_fail, track_pass], "job_sibling")

        assert len(results) == 2
        fail_res = next(r for r in results if r.track_id == "t_fail")
        pass_res = next(r for r in results if r.track_id == "t_pass")

        assert fail_res.success is False
        assert pass_res.success is True
        assert pass_worker_saw_own_part is True, "Sibling .part file was corrupted or prematurely unlinked by failing worker"

        # Final filesystem checks
        assert not part_fail.exists(), "Failing .part must be unlinked"
        assert not target_fail.exists(), "Failing target must NOT exist"
        assert not part_pass.exists(), "Passing .part must be atomically renamed"
        assert target_pass.exists(), "Passing target MUST exist"
        assert target_pass.read_bytes() == b"PASS_DATA_AUDIO_VALID"


@pytest.mark.asyncio
async def test_challenge_sibling_isolation_with_overlapping_stems():
    """Files with overlapping stems ('Track', 'Track Extended', 'Track (Remix)'):
    Cleanup of 'Track' MUST NOT touch siblings!
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job = await pm.register_job("job_overlap", "user1", "Album")

        track1 = DownloadTrack(id="t1", title="Track", artist="Artist", album="Album", track_number=1)
        track2 = DownloadTrack(id="t2", title="Track Extended", artist="Artist", album="Album", track_number=2)
        track3 = DownloadTrack(id="t3", title="Track (Remix)", artist="Artist", album="Album", track_number=3)

        target1 = generate_track_path(music_dir, track1.artist, track1.album, track1.title, 1, 1)
        target2 = generate_track_path(music_dir, track2.artist, track2.album, track2.title, 2, 1)
        target3 = generate_track_path(music_dir, track3.artist, track3.album, track3.title, 3, 1)

        part1 = target1.with_name(f".part_{target1.stem}.mp3")
        part2 = target2.with_name(f".part_{target2.stem}.mp3")
        part3 = target3.with_name(f".part_{target3.stem}.mp3")

        # Create sibling parts on disk
        part1.parent.mkdir(parents=True, exist_ok=True)
        part1.write_bytes(b"PART1")
        part2.write_bytes(b"PART2_EXTENDED")
        part3.write_bytes(b"PART3_REMIX")

        # Simulate downloader failure on track 1
        async def fail_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            return (1, "", "Failed")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            max_retries=0,
            command_runner=fail_runner,
        )

        res = await downloader.download_single_track(track1, "job_overlap")
        assert res.success is False

        # Verify part1 was unlinked
        assert not part1.exists(), "part1 must be unlinked"

        # Verify overlapping stem siblings are 100% UNTOUCHED
        assert part2.exists(), "part2 (Track Extended) must NOT be deleted by stem matching!"
        assert part2.read_bytes() == b"PART2_EXTENDED"
        assert part3.exists(), "part3 (Track Remix) must NOT be deleted by stem matching!"
        assert part3.read_bytes() == b"PART3_REMIX"


# ==============================================================================
# Challenge Suite 4: LRCLIB Duration Verification Boundaries (+-3.0s)
# ==============================================================================

@pytest.mark.parametrize(
    "audio_dur,lrc_dur,expected_diff,should_accept_synced",
    [
        (200.0, 200.0, 0.0, True),     # Exact match
        (200.0, 201.5, 1.5, True),     # Well within boundary
        (200.0, 202.99, 2.99, True),   # 2.99s -> ACCEPT
        (200.0, 203.00, 3.00, True),   # 3.00s -> ACCEPT (Exact <= 3.0s boundary)
        (200.0, 203.0001, 3.0001, False), # 3.0001s -> REJECT
        (200.0, 203.01, 3.01, False),  # 3.01s -> REJECT (> 3.0s boundary)
        (200.0, 203.50, 3.50, False),  # 3.50s -> REJECT
        (200.0, 204.00, 4.00, False),  # 4.00s -> REJECT
        (200.0, 290.00, 90.0, False),  # 90.0s Live/Acoustic -> REJECT
        # Negative diffs (|lrc - audio|)
        (200.0, 197.01, 2.99, True),   # -2.99s -> ACCEPT
        (200.0, 197.00, 3.00, True),   # -3.00s -> ACCEPT
        (200.0, 196.99, 3.01, False),  # -3.01s -> REJECT
    ],
)
def test_challenge_lrclib_duration_verification_boundary(
    audio_dur: float,
    lrc_dur: float,
    expected_diff: float,
    should_accept_synced: bool,
):
    """Empirical challenge: strict verification of +-3.0s boundary conditions (2.99s, 3.00s, 3.01s)."""
    raw_data = {
        "name": "Test Song",
        "artistName": "Test Artist",
        "albumName": "Test Album",
        "duration": lrc_dur,
        "syncedLyrics": "[00:01.00] Synced Line 1\n[00:03.00] Synced Line 2",
        "plainLyrics": "Synced Line 1\nSynced Line 2",
    }

    client = LRCLIBClient()
    result = client.verify_and_build(raw_data, audio_duration=audio_dur)

    assert abs(result.duration_diff - expected_diff) < 1e-4, f"Duration diff calculation mismatch: {result.duration_diff} != {expected_diff}"

    if should_accept_synced:
        assert result.is_synced is True, f"Expected synced lyrics ACCEPTED for diff={expected_diff:.4f}s"
        assert result.synced_lyrics is not None
        assert "[00:01.00]" in result.synced_lyrics
        assert result.plain_lyrics is not None
    else:
        assert result.is_synced is False, f"Expected synced lyrics REJECTED for diff={expected_diff:.4f}s"
        assert result.synced_lyrics is None, "synced_lyrics MUST be None when duration difference > 3.0s"
        assert result.plain_lyrics == "Synced Line 1\nSynced Line 2", "Plain lyrics MUST be preserved when synced lyrics rejected"


def test_challenge_lrclib_synced_rejected_plain_derived():
    """When synced lyrics are rejected due to diff > 3.0s and plainLyrics is None,
    plain lyrics MUST be automatically derived via timestamp stripping.
    """
    raw_data = {
        "name": "Live Acoustic Song",
        "artistName": "Band",
        "duration": 290.0,  # Audio is 200.0s -> diff 90.0s
        "syncedLyrics": "[00:01.00] Acoustic first line\n[00:05.00] Acoustic second line",
        "plainLyrics": None,  # No explicit plain lyrics in payload
    }

    client = LRCLIBClient()
    result = client.verify_and_build(raw_data, audio_duration=200.0)

    assert result.is_synced is False
    assert result.synced_lyrics is None
    assert result.plain_lyrics == "Acoustic first line\nAcoustic second line"


@pytest.mark.asyncio
async def test_challenge_lrclib_search_candidate_ranking_boundary():
    """Verify search selects candidate with diff <= 3.0s over candidates with diff > 3.0s."""
    candidates = [
        # Candidate 1: diff = 3.01s (synced) -> MUST NOT be chosen as synced
        {"name": "Song", "artistName": "Artist", "duration": 203.01, "syncedLyrics": "[00:01.00] Rejected 3.01s"},
        # Candidate 2: diff = 2.99s (synced) -> MUST BE CHOSEN
        {"name": "Song", "artistName": "Artist", "duration": 202.99, "syncedLyrics": "[00:01.00] Accepted 2.99s"},
        # Candidate 3: diff = 10.0s (synced)
        {"name": "Song", "artistName": "Artist", "duration": 210.0, "syncedLyrics": "[00:01.00] Rejected 10s"},
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/get":
            return httpx.Response(404)
        elif request.url.path == "/api/search":
            return httpx.Response(200, json=candidates)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client)
        result = await client.fetch_lyrics("Artist", "Song", audio_duration=200.0)

        assert result.is_synced is True
        assert abs(result.duration_diff - 2.99) < 1e-4
        assert result.synced_lyrics == "[00:01.00] Accepted 2.99s"


# ==============================================================================
# Challenge Suite 5: Mutagen UTF-8 Tagging Serialization / Deserialization
# ==============================================================================

def test_challenge_mutagen_id3v24_utf8_multilingual_and_emoji(tmp_path: Path, synthetic_audio_factory):
    """Verify ID3v2.4 embedding serializes and reloads complex multilingual text, emoji, and SYLT timestamps."""
    mp3_file = synthetic_audio_factory(
        title="init", artist="init", filename="multilingual/track.mp3"
    )

    test_title = "夜に駆ける (Racing into the Night) 🔥 🎵"
    test_artist = "YOASOBI / 幾田りら"
    test_album = "THE BOOK (Deluxe 豪華盤)"
    test_synced = "[00:01.25] 沈むように溶けてゆくように\n[00:05.50] 二人だけの夜が広がる"
    test_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x12\x34\x56\x78" * 10

    meta = TrackMetadata(
        title=test_title,
        artist=test_artist,
        album=test_album,
        album_artist="YOASOBI",
        track_number=1,
        total_tracks=9,
        disc_number=1,
        total_discs=1,
        year=2021,
        genre="J-Pop",
        synced_lyrics=test_synced,
        cover_bytes=test_jpeg,
    )

    AudioTagger.tag_mp3(mp3_file, meta, save_cover=True, save_lrc=True)

    # Reload from disk using raw Mutagen ID3
    loaded = ID3(mp3_file)
    assert loaded.version == (2, 4, 0), "ID3 header version MUST be ID3v2.4"
    assert loaded["TIT2"].text[0] == test_title
    assert loaded["TPE1"].text == ["YOASOBI", "幾田りら"] or loaded["TPE1"].text[0] == test_artist
    assert loaded["TALB"].text[0] == test_album
    assert loaded["TPE2"].text[0] == "YOASOBI"
    assert loaded["TRCK"].text[0] == "1/9"
    assert loaded["TPOS"].text[0] == "1/1"
    assert str(loaded["TDRC"].text[0]) == "2021"
    assert loaded["TCON"].text[0] == "J-Pop"

    # SYLT verification
    sylt = loaded.getall("SYLT")[0]
    assert sylt.text == [
        ("沈むように溶けてゆくように", 1250),
        ("二人だけの夜が広がる", 5500),
    ]

    # USLT verification
    uslt = loaded.getall("USLT")[0]
    assert "沈むように溶けてゆくように\n二人だけの夜が広がる" in uslt.text

    # APIC cover verification
    apic = loaded.getall("APIC")[0]
    assert apic.data == test_jpeg
    assert apic.type == 3  # Front cover


def test_challenge_mutagen_flac_vorbis_multilingual_and_emoji(tmp_path: Path):
    """Verify FLAC Vorbis comments and Picture block serialize and reload complex multilingual text and emoji."""
    flac_file = make_synthetic_flac(tmp_path / "flac_multi" / "01 - Song.flac")

    test_title = "봄날 (Spring Day) 🌸 ❄️"
    test_artist = "방탄소년단 (BTS)"
    test_album = "YOU NEVER WALK ALONE"
    test_synced = "[00:02.00] 보고 싶다 이렇게 말하니까 더 보고 싶다\n[00:08.50] 너희 사진을 보고 있어도 보고 싶다"
    test_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\xab\xcd\xef" * 10

    meta = TrackMetadata(
        title=test_title,
        artist=test_artist,
        album=test_album,
        album_artist="BTS",
        track_number=2,
        total_tracks=18,
        disc_number=1,
        total_discs=1,
        year="2017",
        genre="K-Pop",
        synced_lyrics=test_synced,
        cover_bytes=test_png,
    )

    AudioTagger.tag_flac(flac_file, meta, save_cover=True, save_lrc=True)

    # Reload from disk
    loaded = FLAC(flac_file)
    assert loaded["title"][0] == test_title
    assert loaded["artist"][0] == test_artist
    assert loaded["album"][0] == test_album
    assert loaded["albumartist"][0] == "BTS"
    assert loaded["tracknumber"][0] == "2"
    assert loaded["tracktotal"][0] == "18"
    assert loaded["discnumber"][0] == "1"
    assert loaded["disctotal"][0] == "1"
    assert loaded["date"][0] == "2017"
    assert loaded["genre"][0] == "K-Pop"
    assert loaded["lyrics"][0] == test_synced
    assert "보고 싶다 이렇게 말하니까 더 보고 싶다" in loaded["unsyncedlyrics"][0]

    # Picture block reload
    assert len(loaded.pictures) == 1
    assert loaded.pictures[0].data == test_png
    assert loaded.pictures[0].type == 3
    assert loaded.pictures[0].mime == "image/png"


# ==============================================================================
# Challenge Suite 6: Unexpected Worker Exception Handling
# ==============================================================================

@pytest.mark.asyncio
async def test_challenge_unexpected_exception_in_worker_pool():
    """Empirical challenge: If a download command raises an unexpected exception (e.g. PermissionError or OS crash),
    does download_missing_tracks handle it gracefully without deadlocking or leaving orphan state?
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        await pm.register_job("job_exc", "user1", "Album")

        tracks = [
            DownloadTrack(id="t1", title="Good Song 1", artist="Artist", album="Album", track_number=1),
            DownloadTrack(id="t2", title="Crashing Song", artist="Artist", album="Album", track_number=2),
            DownloadTrack(id="t3", title="Good Song 2", artist="Artist", album="Album", track_number=3),
        ]

        async def exploding_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            if "Crashing Song" in str(cmd):
                raise PermissionError("Simulated filesystem permission denied")
            Path(temp_dir).write_bytes(b"VALID_AUDIO")
            return (0, "OK", "")

        downloader = Downloader(
            music_dir=music_dir,
            process_manager=pm,
            concurrency=2,
            max_retries=1,
            command_runner=exploding_runner,
        )

        results = await downloader.download_missing_tracks(tracks, "job_exc")

        assert len(results) == 3
        t2_res = next(r for r in results if r.track_id == "t2")
        assert t2_res.success is False
        assert "permission denied" in t2_res.error.lower()

        good_results = [r for r in results if r.track_id in ("t1", "t3")]
        assert all(r.success for r in good_results)


# ==============================================================================
# Challenge Suite 7: Multi-Job Isolation & Cross-Job Immunity Under Cancellation
# ==============================================================================

@pytest.mark.asyncio
async def test_challenge_multi_job_concurrent_isolation_cancellation():
    """Verify that cancelling Job A does NOT affect concurrently running Job B,
    even when they share the same music directory.
    """
    with tempfile.TemporaryDirectory() as td:
        music_dir = Path(td)
        pm = ProcessManager()
        job_a = await pm.register_job("job_multi_a", "user_a", "Album A")
        job_b = await pm.register_job("job_multi_b", "user_b", "Album B")

        tracks_a = [
            DownloadTrack(id=f"a_{i}", title=f"Track A {i}", artist="ArtistA", album="AlbumA", track_number=i)
            for i in range(1, 4)
        ]
        tracks_b = [
            DownloadTrack(id=f"b_{i}", title=f"Track B {i}", artist="ArtistB", album="AlbumB", track_number=i)
            for i in range(1, 4)
        ]

        async def runner_a(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            if job_a.cancel_event.is_set():
                raise asyncio.CancelledError()
            Path(temp_dir).write_bytes(b"DATA_A")
            await asyncio.sleep(0.3)
            return (0, "OK", "")

        async def runner_b(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
            Path(temp_dir).write_bytes(b"DATA_B")
            await asyncio.sleep(0.05)
            return (0, "OK", "")

        dl_a = Downloader(music_dir, pm, concurrency=2, command_runner=runner_a)
        dl_b = Downloader(music_dir, pm, concurrency=2, command_runner=runner_b)

        task_a = asyncio.create_task(dl_a.download_missing_tracks(tracks_a, "job_multi_a"))
        task_b = asyncio.create_task(dl_b.download_missing_tracks(tracks_b, "job_multi_b"))

        # Wait a moment, then cancel Job A
        await asyncio.sleep(0.05)
        await pm.cancel_job("job_multi_a")

        # Job B should complete successfully
        results_b = await task_b
        assert len(results_b) == 3
        assert all(r.success for r in results_b), "Job B suffered collateral damage from Job A's cancellation"

        # Job A should complete cleanly with cancelled status
        await task_a

        # Filesystem assertions: Job B's files must exist; Job A's partial files must not exist
        for b_track in tracks_b:
            b_path = generate_track_path(music_dir, b_track.artist, b_track.album, b_track.title, b_track.track_number, 1)
            assert b_path.exists(), f"Job B track {b_path.name} was lost!"

        for a_part in music_dir.rglob("*"):
            if a_part.is_file() and (a_part.name.startswith(".part_") or a_part.name.endswith(".part")):
                pytest.fail(f"Found orphaned partial file after multi-job cancellation: {a_part}")


# ==============================================================================
# Challenge Suite 8: LRCLIB Stress Tests (Malformed LRC, Bad Timestamps, 5xx)
# ==============================================================================

def test_challenge_lrclib_malformed_timestamps():
    """Verify parse_lrc_lines and strip_lrc_timestamps handle malformed tags gracefully without crashing."""
    malformed_lrc = (
        "[invalid] Not a timestamp\n"
        "[01:23] No fractional\n"
        "[02:45.6] One decimal digit\n"
        "[03:12.789] Three decimal digits\n"
        "[ti:Metadata Tag]\n"
        "Plain line without tags\n"
    )

    parsed = parse_lrc_lines(malformed_lrc)
    # [01:23] -> (1*60 + 23)*1000 = 83000
    # [02:45.6] -> (2*60 + 45.6)*1000 = 165600
    # [03:12.789] -> (3*60 + 12.789)*1000 = 192789
    assert len(parsed) == 3
    assert parsed[0] == ("No fractional", 83000)
    assert parsed[1] == ("One decimal digit", 165600)
    assert parsed[2] == ("Three decimal digits", 192789)

    stripped = strip_lrc_timestamps(malformed_lrc)
    assert "Metadata Tag" not in stripped
    assert "Not a timestamp" in stripped
    assert "No fractional" in stripped
    assert "Three decimal digits" in stripped
    assert "Plain line without tags" in stripped


@pytest.mark.asyncio
async def test_challenge_lrclib_repeated_500_graceful_fallback():
    """Verify LRCLIBClient returns empty LyricsResult gracefully when upstream returns 500 repeatedly."""
    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(error_handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://lrclib.net") as http_client:
        client = LRCLIBClient(client=http_client, max_retries=2, retry_backoff=0.01)
        res = await client.fetch_lyrics("CrashArtist", "CrashSong", audio_duration=180.0)

        assert res.has_lyrics() is False
        assert res.synced_lyrics is None
        assert res.plain_lyrics is None
        assert res.track_name == "CrashSong"


# ==============================================================================
# Challenge Suite 9: Path Sanitizer Byte-Boundary Truncation
# ==============================================================================

def test_challenge_path_sanitizer_byte_truncation_boundary():
    """Verify sanitize_path_component truncates at byte boundaries without splitting multibyte UTF-8 characters."""
    # 3-byte Japanese characters: "あ" is 3 bytes in UTF-8
    long_japanese = "あ" * 100  # 300 bytes
    sanitized = sanitize_path_component(long_japanese, max_bytes=200)

    encoded = sanitized.encode("utf-8")
    assert len(encoded) <= 200, f"Byte length {len(encoded)} exceeds max_bytes 200"
    # Verify string is valid UTF-8 and does not end with replacement character \ufffd
    assert not sanitized.endswith("\ufffd")
    # Verify decode succeeded cleanly
    decoded = encoded.decode("utf-8")
    assert decoded == sanitized
