"""server/app/downloader.py - Concurrent Media Downloader & Pipeline Stage 2

Architecture & Features:
1. Concurrent Worker Pool: Bounded async worker queue (4-6 workers) downloading only missing tracks.
2. Deterministic Path Generator: Computes {music_dir}/{artist}/{album}/{disc:02d}-{track:02d} - {title}.mp3
   with full filesystem sanitization, Unicode NFKC script preservation, forbidden char stripping, and byte length limits.
3. Safe .part Download Flow: Subprocess downloads to deterministic .part file in the target directory, verifies
   integrity, and atomically renames to the final audio file via os.replace.
4. Non-Destructive Invariant: Never unlinks valid audio tracks regardless of size (preserves intros/skits <350KB).
   Cleans up only specific in-flight partial files on failure or cancellation.
5. Integration: Integrates seamlessly with ProcessManager (safe argv execution, PID group cancellation,
   temp file scoping) and ConnectionManager (real-time WebSocket progress and status events).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("server.app.downloader")

# ==============================================================================
# 1. Constants & Sanitization Regexes
# ==============================================================================

ILLEGAL_CHARS_PATTERN = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f-\x9f]')

RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def sanitize_path_component(name: str, fallback: str = "Unknown", max_bytes: int = 200) -> str:
    """Sanitizes a single directory or filename component for cross-platform filesystems.

    Rules:
    - Normalizes using Unicode NFKC (preserves Japanese, Korean, Cyrillic, accented Latin).
    - Replaces path separators (/ and \\) with underscores.
    - Replaces colons (:) with ' - '.
    - Replaces illegal/dangerous filesystem characters (* ? " < > | control chars) with underscores.
    - Strips leading and trailing whitespace and dots.
    - Safely resolves DOS reserved names (CON, NUL, AUX, etc.) by appending an underscore.
    - Truncates byte length to max_bytes without cutting multi-byte UTF-8 character boundaries.
    - Returns fallback if component collapses to empty string.
    """
    if not name or not str(name).strip():
        return fallback

    # 1. Unicode NFKC normalization
    s = unicodedata.normalize("NFKC", str(name)).strip()

    # 2. Replace separators and illegal characters
    s = s.replace(":", " - ")
    s = s.replace("/", "_").replace("\\", "_")
    s = ILLEGAL_CHARS_PATTERN.sub("_", s)

    # 3. Collapse multiple whitespace and consecutive underscores
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip(" .")

    # 4. Check reserved DOS device names
    if s.upper() in RESERVED_NAMES:
        s = f"{s}_"

    # 5. Safe UTF-8 byte truncation
    encoded = s.encode("utf-8")
    if len(encoded) > max_bytes:
        s = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .")

    return s if s else fallback


def generate_track_path(
    music_dir: Union[Path, str],
    artist: str,
    album: str,
    title: str,
    track_number: int = 1,
    disc_number: int = 1,
    ext: str = ".mp3",
) -> Path:
    """Generates deterministic target audio file path adhering to:
    {music_dir}/{artist}/{album}/{disc:02d}-{track:02d} - {title}{ext}
    """
    base_dir = Path(music_dir).expanduser().resolve()
    clean_artist = sanitize_path_component(artist, fallback="Unknown Artist")
    clean_album = sanitize_path_component(album, fallback="Unknown Album")
    clean_title = sanitize_path_component(title, fallback="Unknown Track")

    clean_ext = ext.strip()
    if not clean_ext.startswith("."):
        clean_ext = f".{clean_ext}"

    disc_num = max(1, int(disc_number)) if disc_number is not None else 1
    track_num = max(1, int(track_number)) if track_number is not None else 1

    filename = f"{disc_num:02d}-{track_num:02d} - {clean_title}{clean_ext}"
    return base_dir / clean_artist / clean_album / filename


def generate_part_path(target_path: Path, ext: Optional[str] = None) -> Path:
    """Generates the hidden partial download path for a target audio file.
    Preserves the format extension for yt-dlp/ffmpeg and hides the in-flight file from Jellyfin.
    Example: /music/Artist/Album/01-01 - Title.mp3 -> /music/Artist/Album/.part_01-01 - Title.mp3
    """
    clean_ext = ext.strip() if ext else target_path.suffix
    if not clean_ext.startswith("."):
        clean_ext = f".{clean_ext}"
    return target_path.with_name(f".part_{target_path.stem}{clean_ext}")


# ==============================================================================
# 2. Data Models
# ==============================================================================

@dataclass
class DownloadTrack:
    """Represents a track to be ingested."""
    id: str
    title: str
    artist: str
    album: str
    disc_number: int = 1
    track_number: int = 1
    duration_ms: float = 0.0
    url: Optional[str] = None


@dataclass
class TrackResult:
    """Result of a track download operation."""
    track_id: str
    title: str
    artist: str
    album: str
    path: Optional[Path] = None
    success: bool = False
    error: Optional[str] = None
    duration_seconds: float = 0.0
    file_size_bytes: int = 0
    was_skipped: bool = False
    cover_embedded: bool = False


class DownloadEngine(str, Enum):
    SPOTDL = "spotdl"
    YTDLP = "yt-dlp"
    AUTO = "auto"


# ==============================================================================
# 3. Downloader Engine Implementation
# ==============================================================================

class Downloader:
    """Concurrent Media Downloader managing Stage 2 missing track fetching."""

    def __init__(
        self,
        music_dir: Union[Path, str],
        process_manager: Any,  # ProcessManager
        ws_broadcaster: Optional[Any] = None,  # ConnectionManager
        concurrency: int = 4,
        bitrate: str = "320k",
        format_ext: str = ".mp3",
        sponsorblock: bool = True,
        per_track_timeout: float = 180.0,
        max_retries: int = 2,
        engine: DownloadEngine = DownloadEngine.AUTO,
        command_runner: Optional[Callable[..., Coroutine]] = None,
    ):
        self.music_dir = Path(music_dir).expanduser().resolve()
        self.process_manager = process_manager
        self.ws_broadcaster = ws_broadcaster
        self.concurrency = max(1, min(16, concurrency))
        self.bitrate = bitrate
        self.format_ext = format_ext if format_ext.startswith(".") else f".{format_ext}"
        self.sponsorblock = sponsorblock
        self.per_track_timeout = per_track_timeout
        self.max_retries = max_retries
        self.engine = engine
        self._custom_command_runner = command_runner

    def get_part_path(self, target_path: Path) -> Path:
        """Returns the hidden partial download path for a target audio file."""
        return generate_part_path(target_path, self.format_ext)

    def build_download_command(
        self,
        track: DownloadTrack,
        part_path: Path,
    ) -> List[str]:
        """Builds an optimized argv list (shell=False) for yt-dlp."""
        engine = self.engine
        if engine == DownloadEngine.AUTO:
            if shutil.which("yt-dlp") is not None:
                engine = DownloadEngine.YTDLP
            elif shutil.which("spotdl") is not None:
                engine = DownloadEngine.SPOTDL
            else:
                engine = DownloadEngine.YTDLP

        if engine == DownloadEngine.SPOTDL:
            query = track.url if track.url else f"{track.artist} - {track.title}"
            spotdl_output = f"{part_path.with_suffix('')}.{{output-ext}}"
            cmd = [
                "spotdl",
                "download",
                query,
                "--output", spotdl_output,
                "--bitrate", self.bitrate,
                "--max-retries", str(self.max_retries),
                "--audio", "youtube-music", "youtube",
            ]
            if self.sponsorblock:
                cmd.append("--sponsor-block")
            return cmd

        # High-performance yt-dlp zero-copy configuration
        target_query = track.url if track.url else f"ytsearch1:{track.artist} - {track.title} audio"
        fmt = self.format_ext.lstrip(".")
        cmd = [
            "yt-dlp",
            target_query,
            "-f", "bestaudio[ext=m4a]/bestaudio" if fmt == "m4a" else "bestaudio",
            "-x",
            "--audio-format", fmt,
            "--audio-quality", self.bitrate,
            "--embed-thumbnail",
            "--concurrent-fragments", "4",
            "--no-playlist",
            "--no-part",
            "--no-keep-video",
            "--no-cache-dir",
            "-o", str(part_path),
        ]
        if self.sponsorblock:
            cmd.extend(["--sponsorblock-remove", "all"])
        return cmd

    async def _execute_command(
        self,
        cmd: List[str],
        job_id: str,
        cwd: Path,
        part_path: Path,
        target_path: Path,
    ) -> Tuple[int, str, str]:
        """Executes download command through custom runner or ProcessManager."""
        if self._custom_command_runner:
            return await self._custom_command_runner(
                cmd,
                job_id=job_id,
                cwd=cwd,
                temp_dir=part_path,
                in_flight_target=target_path,
                timeout=self.per_track_timeout,
            )

        return await self.process_manager.run_command(
            cmd,
            argv=cmd,
            job_id=job_id,
            cwd=cwd,
            temp_dir=part_path,
            in_flight_target=target_path,
            timeout=self.per_track_timeout,
        )

    async def download_single_track(
        self,
        track: DownloadTrack,
        job_id: str,
    ) -> TrackResult:
        """Executes safe .part download and atomic rename for a single track."""
        target_path = generate_track_path(
            music_dir=self.music_dir,
            artist=track.artist,
            album=track.album,
            title=track.title,
            track_number=track.track_number,
            disc_number=track.disc_number,
            ext=self.format_ext,
        )

        # 1. Idempotency Check: preserve existing valid audio file
        if target_path.exists() and target_path.is_file() and target_path.stat().st_size > 0:
            logger.info("[%s] Track already exists locally: %s", job_id, target_path.name)
            return TrackResult(
                track_id=track.id,
                title=track.title,
                artist=track.artist,
                album=track.album,
                path=target_path,
                success=True,
                was_skipped=True,
                duration_seconds=track.duration_ms / 1000.0,
                file_size_bytes=target_path.stat().st_size,
            )

        target_dir = target_path.parent
        target_dir.mkdir(parents=True, exist_ok=True)

        part_path = self.get_part_path(target_path)

        # 2. Cleanup any stale partial from a previous aborted attempt (both new prefix, non-dotted prefix, and legacy suffix)
        legacy_part = target_path.with_name(f"{target_path.name}.part")
        nodot_part = part_path.with_name(part_path.name.lstrip("."))
        for stale in (part_path, nodot_part, legacy_part):
            if stale.exists() and stale.is_file():
                try:
                    stale.unlink()
                except OSError as e:
                    logger.warning("[%s] Could not unlink stale part %s: %s", job_id, stale, e)

        # 3. Register in-flight target with ProcessManager job
        job = self.process_manager.get_job(job_id) if hasattr(self.process_manager, "get_job") else None
        if job:
            async with job.lock:
                job.in_flight_targets.add(target_path)

        # 4. Build command and execute with retry loop
        cmd = self.build_download_command(track, part_path)
        last_error = ""

        for attempt in range(1, self.max_retries + 2):
            # Check cancellation between retries
            if job and job.cancel_event.is_set():
                break

            try:
                t0 = time.time()
                rc, stdout, stderr = await self._execute_command(
                    cmd=cmd,
                    job_id=job_id,
                    cwd=target_dir,
                    part_path=part_path,
                    target_path=target_path,
                )

                if rc == 0:
                    # 5. Integrity Verification: check that .part exists and is non-empty regular file
                    # SpotDL strips leading dots from filenames (.part_ -> part_), so check both variants
                    actual_part = part_path
                    if not actual_part.exists() or not actual_part.is_file():
                        alt = part_path.with_name(part_path.name.lstrip("."))
                        if alt.exists() and alt.is_file():
                            actual_part = alt

                    if not actual_part.exists() or not actual_part.is_file() or actual_part.stat().st_size == 0:
                        last_error = f"Command succeeded but partial file {part_path.name} was empty or missing, or not a regular file"
                        logger.warning("[%s] %s (attempt %d/%d)", job_id, last_error, attempt, self.max_retries + 1)
                        continue

                    # 6. Atomic Rename to final path
                    os.replace(actual_part, target_path)

                    # 7. Non-destructive protection: register completed file
                    if job:
                        async with job.lock:
                            job.completed_files.add(target_path)
                            job.in_flight_targets.discard(target_path)

                    file_size = target_path.stat().st_size
                    duration_s = track.duration_ms / 1000.0 if track.duration_ms else (time.time() - t0)
                    logger.info("[%s] Successfully downloaded and finalized: %s (%d bytes)", job_id, target_path.name, file_size)

                    is_cover_embedded = False
                    try:
                        import mutagen
                        from mutagen.mp4 import MP4
                        from mutagen.flac import FLAC
                        mf = mutagen.File(str(target_path))
                        if mf:
                            cov_bytes = None
                            if isinstance(mf, MP4) and "covr" in mf and mf["covr"]:
                                is_cover_embedded = True
                                cov_bytes = bytes(mf["covr"][0])
                            elif hasattr(mf, "tags") and mf.tags:
                                apics = [v for k, v in mf.tags.items() if k.startswith("APIC")]
                                if apics:
                                    is_cover_embedded = True
                                    cov_bytes = apics[0].data
                            elif isinstance(mf, FLAC) and mf.pictures:
                                is_cover_embedded = True
                                cov_bytes = mf.pictures[0].data

                            if cov_bytes:
                                cov_file = target_path.parent / "cover.jpg"
                                if not cov_file.exists():
                                    cov_file.write_bytes(cov_bytes)
                    except Exception as cov_err:
                        logger.debug("[%s] Cover detection error: %s", job_id, cov_err)

                    return TrackResult(
                        track_id=track.id,
                        title=track.title,
                        artist=track.artist,
                        album=track.album,
                        path=target_path,
                        success=True,
                        duration_seconds=duration_s,
                        file_size_bytes=file_size,
                        cover_embedded=is_cover_embedded,
                    )
                else:
                    err_raw = (stderr.strip() or stdout.strip())
                    err_last_line = err_raw.splitlines()[-1] if err_raw else f"Exit code {rc}"
                    last_error = err_last_line
                    if attempt <= self.max_retries:
                        logger.info(
                            "[%s] Track '%s' retry %d/%d (transient: %s)",
                            job_id,
                            track.title,
                            attempt,
                            self.max_retries + 1,
                            err_last_line,
                        )
                    else:
                        logger.warning(
                            "[%s] Download failed for '%s' after %d attempts: %s",
                            job_id,
                            track.title,
                            attempt,
                            err_last_line,
                        )

            except asyncio.CancelledError:
                logger.info("[%s] Download cancelled for track '%s'", job_id, track.title)
                if part_path.exists():
                    part_path.unlink(missing_ok=True)
                if job:
                    async with job.lock:
                        job.in_flight_targets.discard(target_path)
                raise

            except Exception as exc:
                last_error = str(exc)
                logger.warning("[%s] Exception on attempt %d for '%s': %s", job_id, attempt, track.title, exc)

        # Cleanup on permanent failure
        if part_path.exists():
            try:
                part_path.unlink()
            except OSError:
                pass

        if job:
            async with job.lock:
                job.in_flight_targets.discard(target_path)

        return TrackResult(
            track_id=track.id,
            title=track.title,
            artist=track.artist,
            album=track.album,
            path=None,
            success=False,
            error=last_error or "Exceeded max download retries",
        )

    async def download_missing_tracks(
        self,
        missing_tracks: List[DownloadTrack],
        job_id: str,
        total_tracks_count: Optional[int] = None,
        already_present_count: int = 0,
    ) -> List[TrackResult]:
        """Executes concurrent worker pool downloading only missing tracks."""
        total_tracks = total_tracks_count if total_tracks_count is not None else (already_present_count + len(missing_tracks))
        if not missing_tracks:
            logger.info("[%s] No missing tracks to download; stage complete", job_id)
            return []

        queue: asyncio.Queue[DownloadTrack] = asyncio.Queue()
        for t in missing_tracks:
            queue.put_nowait(t)

        results: List[TrackResult] = []
        results_lock = asyncio.Lock()
        completed_count = already_present_count
        failed_count = 0
        start_time = time.time()
        total_downloaded_bytes = 0

        job = self.process_manager.get_job(job_id) if hasattr(self.process_manager, "get_job") else None

        # Emit StageTransitionEvent if broadcaster is available
        if self.ws_broadcaster:
            try:
                from server.app.ws import StageTransitionEvent
                await self.ws_broadcaster.broadcast(
                    StageTransitionEvent(
                        job_id=job_id,
                        stage=2,
                        stage_name="Downloading Tracks",
                        description=f"Fetching {len(missing_tracks)} tracks with {self.concurrency} workers",
                    ),
                    job_id=job_id,
                )
            except Exception as e:
                logger.warning("[%s] Failed to broadcast stage transition: %s", job_id, e)

        async def _worker(worker_id: int):
            nonlocal completed_count, failed_count, total_downloaded_bytes
            while True:
                try:
                    track = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if job and job.cancel_event.is_set():
                    queue.task_done()
                    break

                try:
                    result = await self.download_single_track(track, job_id)
                except asyncio.CancelledError:
                    queue.task_done()
                    break

                async with results_lock:
                    results.append(result)
                    if result.success:
                        completed_count += 1
                        if result.file_size_bytes:
                            total_downloaded_bytes += result.file_size_bytes
                        elif result.path and result.path.exists():
                            total_downloaded_bytes += result.path.stat().st_size
                    else:
                        failed_count += 1

                    elapsed = max(time.time() - start_time, 0.5)
                    # Real-time download speed calculation
                    if total_downloaded_bytes > 0:
                        speed_mbps = (total_downloaded_bytes / (1024 * 1024)) / elapsed
                        speed_str = f"{speed_mbps:.1f} MB/s"
                    else:
                        speed_str = "0.0 MB/s"

                    # Dynamic ETA calculation
                    tracks_done_in_session = (completed_count - already_present_count) + failed_count
                    remaining_tracks = max(len(missing_tracks) - tracks_done_in_session, 0)
                    if tracks_done_in_session > 0 and remaining_tracks > 0:
                        avg_sec_per_track = elapsed / tracks_done_in_session
                        eta_seconds = int(remaining_tracks * avg_sec_per_track)
                    else:
                        eta_seconds = 0

                    # Real-time WebSocket event emission
                    if self.ws_broadcaster:
                        try:
                            from server.app.ws import ProgressEvent, TrackCompletedEvent, TrackFailedEvent
                            pct = round((completed_count / total_tracks) * 100.0, 1) if total_tracks > 0 else 100.0
                            await self.ws_broadcaster.broadcast(
                                ProgressEvent(
                                    job_id=job_id,
                                    percentage=pct,
                                    pct=pct,
                                    current_track=completed_count,
                                    current=completed_count,
                                    total_tracks=total_tracks,
                                    total=total_tracks,
                                    current_title=track.title,
                                    speed=speed_str,
                                    eta_seconds=eta_seconds,
                                    status=f"Downloaded {completed_count}/{total_tracks}",
                                ),
                                job_id=job_id,
                            )

                            if result.success:
                                await self.ws_broadcaster.broadcast(
                                    TrackCompletedEvent(
                                        job_id=job_id,
                                        track_id=track.id,
                                        track=track.title,
                                        artist=track.artist,
                                        duration=result.duration_seconds,
                                        lyrics_synced=False,
                                        cover_embedded=result.cover_embedded,
                                        path=str(result.path) if result.path else "",
                                    ),
                                    job_id=job_id,
                                )
                            else:
                                await self.ws_broadcaster.broadcast(
                                    TrackFailedEvent(
                                        job_id=job_id,
                                        track_id=track.id,
                                        track=track.title,
                                        artist=track.artist,
                                        error=result.error or "Download failed",
                                        retrying=False,
                                        retry_count=self.max_retries,
                                    ),
                                    job_id=job_id,
                                )
                        except Exception as e:
                            logger.warning("[%s] Error broadcasting track progress: %s", job_id, e)

                queue.task_done()

        num_workers = min(self.concurrency, len(missing_tracks))
        worker_tasks = [asyncio.create_task(_worker(i)) for i in range(num_workers)]

        if job:
            async with job.lock:
                job.worker_tasks.extend(worker_tasks)

        try:
            await asyncio.gather(*worker_tasks)
        finally:
            if job:
                async with job.lock:
                    for t in worker_tasks:
                        if t in job.worker_tasks:
                            job.worker_tasks.remove(t)

        return results

    async def download_tracks(
        self,
        missing_tracks: List[DownloadTrack],
        job_id: str,
        queue_size: Optional[int] = None,
    ) -> AsyncGenerator[TrackResult, None]:
        """Asynchronous generator yielding TrackResults as they complete per PROJECT.md § 121."""
        if queue_size:
            self.concurrency = max(1, min(16, queue_size))

        result_queue: asyncio.Queue[Optional[TrackResult]] = asyncio.Queue()
        track_queue: asyncio.Queue[DownloadTrack] = asyncio.Queue()

        for t in missing_tracks:
            track_queue.put_nowait(t)

        job = self.process_manager.get_job(job_id) if hasattr(self.process_manager, "get_job") else None

        async def _worker():
            while True:
                try:
                    track = track_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if job and job.cancel_event.is_set():
                    track_queue.task_done()
                    break

                try:
                    res = await self.download_single_track(track, job_id)
                    await result_queue.put(res)
                except asyncio.CancelledError:
                    track_queue.task_done()
                    break
                except Exception as exc:
                    await result_queue.put(TrackResult(
                        track_id=track.id,
                        title=track.title,
                        artist=track.artist,
                        album=track.album,
                        success=False,
                        error=str(exc),
                    ))
                finally:
                    track_queue.task_done()

        num_workers = min(self.concurrency, len(missing_tracks)) if missing_tracks else 0
        if num_workers == 0:
            return

        worker_tasks = [asyncio.create_task(_worker()) for _ in range(num_workers)]

        async def _waiter():
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            await result_queue.put(None)  # Sentinel

        waiter_task = asyncio.create_task(_waiter())

        while True:
            item = await result_queue.get()
            if item is None:
                break
            yield item

        await waiter_task
