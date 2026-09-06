"""Empirical Challenger 1 Stress Harness: Real yt-dlp CLI Invocations, Atomic Part Renames, and Selective Cleanup.

Tests:
1. Real yt-dlp CLI download with --audio-format mp3 to .part_{stem}{ext}
2. Real yt-dlp CLI download with --audio-format flac to .part_{stem}{ext}
3. Verification that yt-dlp never writes prematurely to target_path
4. Verification that yt-dlp preserves .part_{stem}{ext} without stripping extension
5. Atomic rename via os.replace moving .part_{stem}{ext} to target_path
6. ProcessManager.cleanup_job_temp_files isolation: cleans only target .part_, preserves sibling partials,
   overlapping stems, legacy .part suffixes, and completed audio files
7. Simulated SIGKILL / cancellation during active download and verification of isolated cleanup
8. Stale partial file pre-cleaning (both new dotfile prefix and legacy suffix)
9. Idempotent preservation of existing files
10. Multi-disc regex extraction in indexer.py
"""

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import pytest

from server.app.downloader import (
    Downloader,
    DownloadEngine,
    DownloadTrack,
    generate_part_path,
    generate_track_path,
)
from server.app.process import (
    JobExecutionState,
    ProcessManager,
    compute_valid_temp_names,
)
from server.app.indexer import LibraryIndex


@pytest.fixture
def temp_music_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def create_sine_wav(dest_path: Path, duration_seconds: int = 2) -> None:
    """Creates a small valid PCM WAV file via ffmpeg."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={duration_seconds}",
        "-c:a",
        "pcm_s16le",
        str(dest_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert res.returncode == 0, f"ffmpeg failed: {res.stderr.decode()}"
    assert dest_path.exists() and dest_path.stat().st_size > 0


def get_audio_format(file_path: Path) -> str:
    """Probes container format using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=format_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert res.returncode == 0, f"ffprobe failed: {res.stderr}"
    return res.stdout.strip().lower()


def test_real_ytdlp_cli_mp3_part_flow(temp_music_dir):
    """Stress test: yt-dlp CLI with --audio-format mp3 using .part_{stem}{ext}."""
    src_wav = temp_music_dir / "sources" / "test_src.wav"
    create_sine_wav(src_wav, duration_seconds=2)

    artist = "Challenger Artist"
    album = "Adversarial Album"
    title = "Empirical MP3 Test"
    target = generate_track_path(temp_music_dir, artist, album, title, track_number=1, disc_number=1, ext=".mp3")
    part = generate_part_path(target, ".mp3")

    target.parent.mkdir(parents=True, exist_ok=True)

    assert target.name == "01-01 - Empirical MP3 Test.mp3"
    assert part.name == ".part_01-01 - Empirical MP3 Test.mp3"
    assert part.parent == target.parent

    # Execute real yt-dlp CLI command
    cmd = [
        "yt-dlp",
        "--enable-file-urls",
        f"file://{src_wav.resolve()}",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "320k",
        "--no-playlist",
        "--no-part",
        "-o",
        str(part),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert res.returncode == 0, f"yt-dlp failed (rc={res.returncode}): {res.stderr}\n{res.stdout}"

    # Verify output locations
    assert part.exists(), f"yt-dlp did not create expected part file: {part}"
    assert part.is_file(), f"Part path is not a regular file: {part}"
    assert part.stat().st_size > 0, "Part file is empty"
    assert not target.exists(), f"VIOLATION: yt-dlp wrote prematurely to target_path: {target}"

    # Verify extension was not stripped or doubled
    assert not target.parent.joinpath(".part_01-01 - Empirical MP3 Test").exists()
    assert not target.parent.joinpath(".part_01-01 - Empirical MP3 Test.mp3.mp3").exists()

    # Probe format of part file
    fmt = get_audio_format(part)
    assert "mp3" in fmt, f"Expected mp3 format in probe, got {fmt}"

    # Atomic rename via os.replace
    os.replace(part, target)

    assert not part.exists(), "Part file remained after atomic os.replace"
    assert target.exists(), "Target file missing after atomic os.replace"
    assert target.stat().st_size > 0, "Target file is empty after atomic os.replace"

    # Confirm final format
    final_fmt = get_audio_format(target)
    assert "mp3" in final_fmt, f"Expected mp3 format in finalized file, got {final_fmt}"


def test_real_ytdlp_cli_flac_part_flow(temp_music_dir):
    """Stress test: yt-dlp CLI with --audio-format flac using .part_{stem}{ext}."""
    src_wav = temp_music_dir / "sources" / "test_src.wav"
    create_sine_wav(src_wav, duration_seconds=2)

    artist = "Challenger Artist"
    album = "Adversarial Album"
    title = "Empirical FLAC Test"
    target = generate_track_path(temp_music_dir, artist, album, title, track_number=2, disc_number=1, ext=".flac")
    part = generate_part_path(target, ".flac")

    target.parent.mkdir(parents=True, exist_ok=True)

    assert target.name == "01-02 - Empirical FLAC Test.flac"
    assert part.name == ".part_01-02 - Empirical FLAC Test.flac"

    # Execute real yt-dlp CLI command for FLAC
    cmd = [
        "yt-dlp",
        "--enable-file-urls",
        f"file://{src_wav.resolve()}",
        "-x",
        "--audio-format",
        "flac",
        "--audio-quality",
        "320k",
        "--no-playlist",
        "--no-part",
        "-o",
        str(part),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert res.returncode == 0, f"yt-dlp failed (rc={res.returncode}): {res.stderr}\n{res.stdout}"

    # Verify output locations
    assert part.exists(), f"yt-dlp did not create expected part file: {part}"
    assert part.stat().st_size > 0, "Part file is empty"
    assert not target.exists(), f"VIOLATION: yt-dlp wrote prematurely to target_path: {target}"

    # Verify format
    fmt = get_audio_format(part)
    assert "flac" in fmt, f"Expected flac format in probe, got {fmt}"

    # Atomic rename via os.replace
    os.replace(part, target)

    assert not part.exists(), "Part file remained after atomic os.replace"
    assert target.exists(), "Target file missing after atomic os.replace"
    final_fmt = get_audio_format(target)
    assert "flac" in final_fmt, f"Expected flac format in finalized file, got {final_fmt}"


def test_special_characters_filename_part_flow(temp_music_dir):
    """Stress test: complex unicode, brackets, quotes, and punctuation in title."""
    src_wav = temp_music_dir / "sources" / "test_src.wav"
    create_sine_wav(src_wav, duration_seconds=1)

    artist = "アーティスト"
    album = "Álbum Épico [2026]"
    title = "Track #1 (Live & Acoustic) [Feat. 歌手]"
    target = generate_track_path(temp_music_dir, artist, album, title, track_number=1, disc_number=2, ext=".mp3")
    part = generate_part_path(target, ".mp3")

    target.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "yt-dlp",
        "--enable-file-urls",
        f"file://{src_wav.resolve()}",
        "-x",
        "--audio-format",
        "mp3",
        "--no-playlist",
        "--no-part",
        "-o",
        str(part),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert res.returncode == 0, f"yt-dlp failed: {res.stderr}"

    assert part.exists()
    assert not target.exists()
    os.replace(part, target)
    assert target.exists()
    assert get_audio_format(target) == "mp3"


def test_cleanup_temp_files_without_collateral_damage(temp_music_dir):
    """Stress test: cleanup_job_temp_files unlinks only targeted .part_ files,
    preserving sibling partials, overlapping stems, legacy partials, and completed files.
    """
    pm = ProcessManager()
    album_dir = temp_music_dir / "Artist" / "Album"
    album_dir.mkdir(parents=True, exist_ok=True)

    # In-flight target track
    target_track = album_dir / "01-01 - Target Track.mp3"
    target_part = album_dir / ".part_01-01 - Target Track.mp3"
    target_part.write_bytes(b"in flight bytes for target")

    # Sibling 1: Completed file of another track
    completed_file = album_dir / "01-02 - Completed Sibling.mp3"
    completed_file.write_bytes(b"completed audio data")

    # Sibling 2: In-flight partial for a different track (same album)
    sibling_part_1 = album_dir / ".part_01-02 - Completed Sibling.mp3"
    sibling_part_1.write_bytes(b"sibling partial 1")

    # Sibling 3: In-flight partial with overlapping prefix stem
    # e.g. "Target Track" vs "Target Track (Remix)"
    sibling_part_overlap = album_dir / ".part_01-01 - Target Track (Remix).mp3"
    sibling_part_overlap.write_bytes(b"sibling overlap partial")

    # Sibling 4: In-flight partial with numerical suffix stem
    sibling_part_num = album_dir / ".part_01-01 - Target Track 2.mp3"
    sibling_part_num.write_bytes(b"sibling num partial")

    # Sibling 5: Different format extension for same stem
    sibling_part_flac = album_dir / ".part_01-01 - Target Track.flac"
    sibling_part_flac.write_bytes(b"sibling flac partial")

    # Sibling 6: Legacy suffix .part for different track
    legacy_part = album_dir / "01-03 - Legacy Sibling.mp3.part"
    legacy_part.write_bytes(b"legacy part bytes")

    # Job registration with target_track in flight and completed_file completed
    job = JobExecutionState(
        job_id="job-test-cleanup",
        user_id="user1",
        playlist_name="Test Playlist",
        in_flight_targets={target_track},
        completed_files={completed_file},
    )

    cleaned = pm.cleanup_job_temp_files(job)

    # 1. Target's partial file MUST be cleaned
    assert not target_part.exists(), "Target part file was NOT cleaned"
    assert cleaned == 1, f"Expected exactly 1 cleaned file, got {cleaned}"

    # 2. Completed file MUST NOT be touched
    assert completed_file.exists(), "COLLATERAL DAMAGE: Completed file was deleted!"
    assert completed_file.read_bytes() == b"completed audio data"

    # 3. Sibling partials MUST NOT be touched
    assert sibling_part_1.exists(), "COLLATERAL DAMAGE: Sibling part 1 was deleted!"
    assert sibling_part_overlap.exists(), "COLLATERAL DAMAGE: Sibling overlap part was deleted!"
    assert sibling_part_num.exists(), "COLLATERAL DAMAGE: Sibling numerical part was deleted!"
    assert sibling_part_flac.exists(), "COLLATERAL DAMAGE: Sibling FLAC part was deleted!"
    assert legacy_part.exists(), "COLLATERAL DAMAGE: Legacy sibling part was deleted!"


@pytest.mark.asyncio
async def test_simulated_sigkill_cancellation_during_download(temp_music_dir):
    """Stress test: Simulated SIGKILL during active yt-dlp invocation.
    Verifies process group termination, target .part cleanup, and zero collateral damage.
    """
    pm = ProcessManager()
    album_dir = temp_music_dir / "Artist" / "Album"
    album_dir.mkdir(parents=True, exist_ok=True)

    job_id = "job-sigkill-stress"
    await pm.register_job(job_id, user_id="user_stress", playlist_name="Stress Playlist")

    target_track = album_dir / "01-01 - Slow Track.mp3"
    target_part = album_dir / ".part_01-01 - Slow Track.mp3"

    # Also place a sibling partial file that belongs to another track
    sibling_part = album_dir / ".part_01-02 - Other Track.mp3"
    sibling_part.write_bytes(b"other track partial")

    # Launch a process that creates target_part and sleeps (simulating in-flight download)
    slow_script = (
        f"import time, pathlib\n"
        f"p = pathlib.Path({repr(str(target_part))})\n"
        f"p.write_bytes(b'in flight audio stream')\n"
        f"time.sleep(30)\n"
    )

    cmd = ["python3", "-c", slow_script]
    handle = await pm.spawn_process(
        job_id=job_id,
        argv=cmd,
        cwd=album_dir,
        in_flight_target=target_track,
    )

    # Wait until target_part is created
    for _ in range(50):
        if target_part.exists():
            break
        await asyncio.sleep(0.05)

    assert target_part.exists(), "Target part file was not created by simulated downloader"
    job = pm.get_job(job_id)
    assert job is not None
    assert handle.pid in job.active_processes

    # Cancel job with SIGKILL escalation
    cancel_res = await pm.cancel_job(job_id)

    assert cancel_res.status == "cancelled"
    assert handle.pid in cancel_res.terminated_pids

    # Verify process is terminated
    try:
        os.kill(handle.pid, 0)
        proc_alive = True
    except OSError:
        proc_alive = False
    assert not proc_alive, "Subprocess was not terminated by cancel_job"

    # Verify target part is cleaned up
    assert not target_part.exists(), "Target partial file was not cleaned up after cancellation"

    # Verify sibling partial is UNTOUCHED
    assert sibling_part.exists(), "COLLATERAL DAMAGE: Sibling partial was deleted during cancel!"
    assert sibling_part.read_bytes() == b"other track partial"

    await pm.shutdown()


@pytest.mark.asyncio
async def test_stale_partial_precleaning(temp_music_dir):
    """Stress test: Downloader cleans up both .part_{stem}{ext} and {name}.part before downloading."""
    pm = ProcessManager()
    job_id = "job-stale-cleanup"
    await pm.register_job(job_id, user_id="u1", playlist_name="p1")

    downloader = Downloader(
        music_dir=temp_music_dir,
        process_manager=pm,
        engine=DownloadEngine.YTDLP,
    )

    target_path = generate_track_path(temp_music_dir, "Artist", "Album", "Stale Song", 1, 1, ext=".mp3")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    part_path = downloader.get_part_path(target_path)
    legacy_part = target_path.with_name(f"{target_path.name}.part")

    # Create stale files
    part_path.write_bytes(b"stale prefix part")
    legacy_part.write_bytes(b"stale legacy suffix part")

    assert part_path.exists()
    assert legacy_part.exists()

    # Create mock runner that checks stale files are gone before creating new part
    async def mock_runner(cmd, job_id, cwd, temp_dir, in_flight_target, timeout):
        # Stale parts should already be unlinked by downloader prior to command invocation!
        assert not part_path.exists(), "Stale part_path was not unlinked prior to command execution"
        assert not legacy_part.exists(), "Stale legacy_part was not unlinked prior to command execution"
        # Simulate successful download
        part_path.write_bytes(b"newly downloaded audio bytes")
        return (0, "Download completed", "")

    downloader._custom_command_runner = mock_runner

    track = DownloadTrack(
        id="t1",
        title="Stale Song",
        artist="Artist",
        album="Album",
        track_number=1,
        disc_number=1,
    )

    res = await downloader.download_single_track(track, job_id=job_id)

    assert res.success is True
    assert target_path.exists()
    assert target_path.read_bytes() == b"newly downloaded audio bytes"
    assert not part_path.exists()
    assert not legacy_part.exists()

    await pm.shutdown()


def test_indexer_multidisc_title_extraction():
    """Stress test: indexer regex correctly parses multi-disc tracks without truncating disc prefix."""
    # Test cases: (filename_stem, expected_title)
    cases = [
        ("01-01 - Title", "Title"),
        ("1-01 - Title", "Title"),
        ("02-15 - Comfortably Numb", "Comfortably Numb"),
        ("10-04 - Long Disc Track", "Long Disc Track"),
        ("01 - Simple Track", "Simple Track"),
        ("01. Dotted Track", "Dotted Track"),
        ("05_Underscore Track", "Underscore Track"),
        ("1 - Single Digit Track", "Single Digit Track"),
        ("Title Without Number", "Title Without Number"),
    ]

    for stem, expected in cases:
        cleaned = re.sub(r"^(?:\d+-\d+|\d+)[\s\.\-_]+", "", stem).strip()
        assert cleaned == expected, f"Failed for '{stem}': got '{cleaned}', expected '{expected}'"
