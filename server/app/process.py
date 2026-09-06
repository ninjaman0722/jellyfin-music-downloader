"""Safe Subprocess Engine & Targeted Process Group Manager for Jellyfin Music Downloader V2.

Provides:
- Safe subprocess execution via argv lists (shell=False).
- Dedicated process group assignment via preexec_fn=os.setsid.
- Non-blocking stream draining splitting on both \n and \r to prevent pipe buffer deadlocks.
- Isolated process-targeted cancellation via os.killpg with 5s SIGTERM -> SIGKILL cascade.
- Stem-matched temporary partial file cleanup (.part, .tmp) protecting completed tracks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Coroutine, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger("daemon.process")

PART_EXTENSIONS = {".part", ".tmp", ".temp", ".download", ".ytdl"}
PART_PREFIXES = {f".{ext.lstrip('.')}_" for ext in PART_EXTENSIONS} | {f"{ext.lstrip('.')}_" for ext in PART_EXTENSIONS}


def compute_valid_temp_names(target_path_or_name: Union[Path, str]) -> Set[str]:
    """Computes all valid temporary and partial file names for a target media file.

    Handles:
    - Trailing extension partials (e.g. '01 - Track.mp3.part', '01 - Track.tmp')
    - Hidden prefix partials (e.g. '.part_01 - Track.mp3', '.tmp_01 - Track.mp3')
    - Target passed as either base audio file or intermediate partial file
    """
    raw_name = Path(target_path_or_name).name
    clean_name = raw_name

    # Strip trailing part extension if present
    for ext in PART_EXTENSIONS:
        if clean_name.endswith(ext):
            clean_name = clean_name[:-len(ext)]
            break

    # Strip leading part prefix if present
    for pfx in PART_PREFIXES:
        if clean_name.startswith(pfx):
            clean_name = clean_name[len(pfx):]
            break

    clean_target = Path(clean_name)
    stem = clean_target.stem
    target_suffix = clean_target.suffix

    return (
        {f"{clean_name}{ext}" for ext in PART_EXTENSIONS}
        | {f"{raw_name}{ext}" for ext in PART_EXTENSIONS}
        | {f"{stem}{ext}" for ext in PART_EXTENSIONS}
        | {f"{stem}{ext}{target_suffix}" for ext in PART_EXTENSIONS}
        | {f"{pfx}{clean_name}" for pfx in PART_PREFIXES}
        | {f"{pfx}{stem}{target_suffix}" for pfx in PART_PREFIXES}
    )


@dataclass
class ProcessHandle:
    """Represents a single running OS subprocess assigned to a job."""
    job_id: str
    pid: int
    pgid: int
    process: asyncio.subprocess.Process
    cmd: List[str]
    start_time: float = field(default_factory=time.time)
    stdout_task: Optional[asyncio.Task] = None
    stderr_task: Optional[asyncio.Task] = None
    in_flight_target: Optional[Path] = None
    is_terminated: bool = False


@dataclass
class JobExecutionState:
    """Encapsulates all process resources, tracking sets, and synchronization for a job."""
    job_id: str
    user_id: str
    playlist_name: str
    status: str = "queued"  # queued, running, cancelling, cancelled, completed, failed
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    active_processes: Dict[int, ProcessHandle] = field(default_factory=dict)
    in_flight_targets: Set[Path] = field(default_factory=set)
    completed_files: Set[Path] = field(default_factory=set)
    worker_tasks: List[asyncio.Task] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    start_time: float = field(default_factory=time.time)


@dataclass
class CancellationResult:
    """Result of a targeted cancellation operation."""
    job_id: str
    status: str
    cleaned_files: int
    terminated_pids: List[int]
    message: str
    duration_seconds: float


class CommandResult(tuple):
    """Result of a run_command invocation: (returncode, stdout, stderr).
    Compatible with 3-tuple unpacking, int comparison, and attribute access.
    """

    def __new__(cls, returncode: int, stdout: str, stderr: str):
        return super().__new__(cls, (returncode, stdout, stderr))

    @property
    def returncode(self) -> int:
        return self[0]

    @property
    def stdout(self) -> str:
        return self[1]

    @property
    def stderr(self) -> str:
        return self[2]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, int):
            return self[0] == other
        return super().__eq__(other)


async def stream_process_lines(
    reader: asyncio.StreamReader,
    chunk_size: int = 4096,
) -> AsyncGenerator[str, None]:
    """Asynchronously reads lines from an asyncio.StreamReader, splitting on both
    newline (\\n) and carriage return (\\r) delimiters.

    Prevents 64KB pipe buffer deadlocks and avoids ValueError from StreamReader.readline()
    when CLI tools emit continuous in-place progress updates.
    """
    buf = bytearray()
    while True:
        try:
            chunk = await reader.read(chunk_size)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error reading process stream: %s", exc)
            break

        if not chunk:
            # EOF reached
            if buf:
                text = buf.decode("utf-8", errors="replace").strip()
                if text:
                    yield text
            break

        buf.extend(chunk)

        # Parse delimiters \r and \n
        while True:
            idx_n = buf.find(b"\n")
            idx_r = buf.find(b"\r")

            if idx_n == -1 and idx_r == -1:
                # Need more data; protect against unbounded buffer growth
                if len(buf) > 1024 * 1024:  # 1MB emergency safety cap
                    text = buf.decode("utf-8", errors="replace").strip()
                    buf.clear()
                    if text:
                        yield text
                break

            # Find which delimiter comes first
            if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
                line = buf[:idx_n]
                del buf[: idx_n + 1]
            else:
                line = buf[:idx_r]
                # Consume trailing \n if this was \r\n
                if idx_r + 1 < len(buf) and buf[idx_r + 1] == ord(b"\n"):
                    del buf[: idx_r + 2]
                else:
                    del buf[: idx_r + 1]

            text = line.decode("utf-8", errors="replace").strip()
            if text:
                yield text


class ProcessManager:
    """Central process supervisor guaranteeing:
    - Safe subprocess execution via argv lists (shell=False)
    - Independent process group assignment via preexec_fn=os.setsid
    - Non-blocking stream draining
    - Isolated targeted cancellation with zero sibling impact
    """

    def __init__(self, ws_broadcaster=None) -> None:
        self._jobs: Dict[str, JobExecutionState] = {}
        self._lock = asyncio.Lock()
        self.ws_broadcaster = ws_broadcaster

    async def register_job(self, job_id: str, user_id: str, playlist_name: str) -> JobExecutionState:
        async with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"Job {job_id} is already registered")
            job = JobExecutionState(job_id=job_id, user_id=user_id, playlist_name=playlist_name)
            self._jobs[job_id] = job
            return job

    def get_job(self, job_id: str) -> Optional[JobExecutionState]:
        return self._jobs.get(job_id)

    def get_active_jobs(self) -> Dict[str, JobExecutionState]:
        return {jid: job for jid, job in self._jobs.items() if job.status in ("queued", "running")}

    def find_conflict(self, user_id: str, playlist_name: str) -> Optional[str]:
        for job in self.get_active_jobs().values():
            if job.user_id == user_id and job.playlist_name.lower() == playlist_name.lower():
                return job.job_id
        return None

    async def spawn_process(
        self,
        job_id: str,
        argv: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        in_flight_target: Optional[Path] = None,
        on_stdout: Optional[Callable[[str], Coroutine]] = None,
        on_stderr: Optional[Callable[[str], Coroutine]] = None,
    ) -> ProcessHandle:
        """Spawns a subprocess inside an isolated process group.
        argv MUST be a list of individual string arguments (no shell string interpolation).
        """
        job = self.get_job(job_id)
        if not job:
            raise KeyError(f"Job {job_id} not registered")

        if job.cancel_event.is_set():
            raise asyncio.CancelledError(f"Job {job_id} has been cancelled")

        job.status = "running"

        # Subprocess execution contract: shell=False, preexec_fn=os.setsid
        proc = await asyncio.create_subprocess_exec(
            argv[0],
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,  # Creates distinct POSIX session & PGID
            cwd=str(cwd) if cwd else None,
            env=env if env is not None else os.environ.copy(),
        )

        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid

        handle = ProcessHandle(
            job_id=job_id,
            pid=proc.pid,
            pgid=pgid,
            process=proc,
            cmd=argv,
            in_flight_target=in_flight_target,
        )

        async with job.lock:
            if job.cancel_event.is_set() or job.status in ("cancelling", "cancelled"):
                logger.warning(
                    "[%s] Detected cancellation during spawn of PID %d (PGID %d). Terminating immediately.",
                    job_id,
                    proc.pid,
                    pgid,
                )
                if pgid > 1:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError as exc:
                        logger.error("[%s] Permission error signalling PGID %d: %s", job_id, pgid, exc)
                else:
                    try:
                        proc.kill()
                    except (ProcessLookupError, PermissionError):
                        pass

                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass

                handle.is_terminated = True

                if in_flight_target:
                    job.in_flight_targets.add(in_flight_target)
                    self.cleanup_job_temp_files(job)

                exc = asyncio.CancelledError(f"Job {job_id} was cancelled during process spawn")
                exc.pid = proc.pid
                exc.pgid = pgid
                raise exc

            job.active_processes[proc.pid] = handle
            if in_flight_target:
                job.in_flight_targets.add(in_flight_target)

        # Background consumer tasks to prevent pipe deadlocks
        async def _drain_stream(reader: asyncio.StreamReader, callback: Optional[Callable[[str], Coroutine]]):
            async for line in stream_process_lines(reader):
                if callback:
                    try:
                        res = callback(line)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:
                        logger.warning("[%s] Callback error on line '%s': %s", job_id, line[:50], exc)

        if proc.stdout:
            handle.stdout_task = asyncio.create_task(_drain_stream(proc.stdout, on_stdout))
        if proc.stderr:
            handle.stderr_task = asyncio.create_task(_drain_stream(proc.stderr, on_stderr))

        logger.info("[%s] Spawned PID=%d PGID=%d: %s...", job_id, proc.pid, pgid, " ".join(argv[:3]))
        return handle

    async def wait_process(self, handle: ProcessHandle) -> int:
        """Awaits process completion and cleans up streams."""
        try:
            return_code = await handle.process.wait()
            # Wait for stream drains to complete
            drain_tasks = [t for t in (handle.stdout_task, handle.stderr_task) if t]
            if drain_tasks:
                await asyncio.wait(drain_tasks, timeout=2.0)
            return return_code
        finally:
            handle.is_terminated = True
            job = self.get_job(handle.job_id)
            if job:
                async with job.lock:
                    job.active_processes.pop(handle.pid, None)

    @staticmethod
    def _is_shared_music_dir(td_path: Path) -> bool:
        """Determines whether td_path represents a shared music/album directory
        where sweeping partial files would cause collateral damage to sibling downloads.
        """
        if not td_path.exists() or not td_path.is_dir():
            return False

        name_lower = td_path.name.lower()
        path_str_lower = str(td_path).lower()

        # 1. Indicator keywords in directory name or path
        shared_indicators = ("album", "music", "artist", "media", "library", "songs", "jellyfin", "shared")
        if any(ind in name_lower or ind in path_str_lower for ind in shared_indicators):
            return True

        try:
            items = [p for p in td_path.iterdir() if p.is_file()]
        except OSError:
            return False

        # 2. Check for music track numbering patterns on partial files (e.g. "01 - Track A.mp3.part" or ".part_01 - Track A.mp3")
        track_pattern = re.compile(r"^(\.(?:part|tmp|temp|download|ytdl)_)?\d{1,3}[\s.-]+")
        numbered_partials = [
            item.name for item in items
            if (any(item.name.endswith(ext) for ext in PART_EXTENSIONS) or
                any(item.name.startswith(pfx) for pfx in PART_PREFIXES))
            and track_pattern.match(item.name)
        ]
        if len(numbered_partials) >= 2:
            return True

        if len(numbered_partials) >= 1 and len(items) >= 2:
            return True

        return False

    @classmethod
    def _is_isolated_temp_dir(cls, td_path: Path, job_id: Optional[str] = None) -> bool:
        """Returns True ONLY if td_path is explicitly an isolated per-job temporary directory."""
        if not td_path.exists() or not td_path.is_dir():
            return False

        if cls._is_shared_music_dir(td_path):
            return False

        if job_id and (job_id in td_path.name or td_path.name.startswith(f"job_{job_id}")):
            return True

        try:
            system_tmp = Path(tempfile.gettempdir()).resolve()
            resolved_td = td_path.resolve()
            is_in_tmp = system_tmp in resolved_td.parents or resolved_td == system_tmp
        except OSError:
            is_in_tmp = False

        if is_in_tmp and (td_path.name.startswith("tmp") or td_path.name.startswith("temp")):
            return True

        return False

    def _cleanup_temp_artifacts(
        self,
        *args,
        job_id: Optional[str] = None,
        temp_dir: Optional[Union[Path, str]] = None,
        in_flight_target: Optional[Union[Path, str]] = None,
        job: Optional[JobExecutionState] = None,
    ) -> int:
        """Cleans up temporary and partial files associated with a job, target, and/or directory.

        Follows strict sibling isolation:
        - If in_flight_target is provided, cleans only matching temporary files via exact set membership.
        - If job has in_flight_targets, delegates to cleanup_job_temp_files(job).
        - If temp_dir is a directory and no target is specified: do NOT sweep and delete all .part files
          if it is a shared music/album directory. Only delete if temp_dir is explicitly an isolated
          per-job temporary directory.
        """
        if len(args) == 1:
            if isinstance(args[0], (Path, str)) and (Path(args[0]).is_dir() or "/" in str(args[0]) or "\\" in str(args[0])):
                if temp_dir is None:
                    temp_dir = args[0]
            else:
                if job_id is None:
                    job_id = str(args[0])
        elif len(args) == 2:
            first, second = args[0], args[1]
            if isinstance(first, str) and (self.get_job(first) is not None or first.startswith("cmd-") or first.startswith("job_")):
                job_id = first
                temp_dir = second
            elif isinstance(first, Path) or (isinstance(first, str) and Path(first).is_dir()):
                temp_dir = first
                in_flight_target = second
            else:
                job_id = str(first)
                temp_dir = second
        elif len(args) >= 3:
            temp_dir = args[0]
            in_flight_target = args[1]
            job_id = str(args[2]) if args[2] is not None else None
            if len(args) >= 4 and job is None:
                job = args[3]

        if job is None and job_id is not None:
            job = self.get_job(job_id)

        actual_job_id = job_id or (job.job_id if job else "unknown")
        cleaned = 0

        # 1. If job has in_flight_targets, delegate to cleanup_job_temp_files(job)
        if job and job.in_flight_targets:
            cleaned += self.cleanup_job_temp_files(job)

        # 2. If in_flight_target is provided, clean only matching temporary files via exact set membership
        target_to_clean: Optional[Path] = None
        if in_flight_target is not None:
            target_to_clean = Path(in_flight_target)
        elif temp_dir is not None:
            td_candidate = Path(temp_dir)
            if td_candidate.exists() and not td_candidate.is_dir():
                target_to_clean = td_candidate

        if target_to_clean is not None:
            parent = target_to_clean.parent
            if parent.exists() and parent.is_dir():
                valid_temp_names = compute_valid_temp_names(target_to_clean)
                try:
                    for item in parent.iterdir():
                        if not item.is_file():
                            continue
                        if job and item in job.completed_files:
                            continue
                        if item.name in valid_temp_names:
                            try:
                                item.unlink()
                                cleaned += 1
                                logger.info("[%s] Cleaned temp artifact for target %s: %s", actual_job_id, target_to_clean.name, item.name)
                            except OSError as exc:
                                logger.warning("[%s] Failed to unlink temp artifact %s: %s", actual_job_id, item, exc)
                except Exception as exc:
                    logger.warning("[%s] Error scanning %s for target %s: %s", actual_job_id, parent, target_to_clean, exc)

        # 3. If temp_dir is a directory and no target is specified:
        # Do NOT sweep if it is a shared music/album directory. Only delete if explicitly an isolated per-job temp dir.
        if temp_dir is not None and target_to_clean is None and (not job or not job.in_flight_targets):
            td_path = Path(temp_dir)
            if td_path.exists() and td_path.is_dir():
                if self._is_isolated_temp_dir(td_path, actual_job_id):
                    for item in td_path.iterdir():
                        if item.is_file() and any(item.name.endswith(ext) for ext in PART_EXTENSIONS):
                            try:
                                item.unlink()
                                cleaned += 1
                                logger.info("[%s] Cleaned temp artifact in isolated dir %s: %s", actual_job_id, td_path, item.name)
                            except OSError as exc:
                                logger.warning("[%s] Failed to unlink temp artifact %s: %s", actual_job_id, item, exc)
                else:
                    logger.debug(
                        "[%s] Preserving shared music/album directory %s without sweeping partial files",
                        actual_job_id,
                        td_path,
                    )

        return cleaned

    async def run_command(
        self,
        argv_or_job_id: Optional[Union[List[str], str]] = None,
        argv: Optional[List[str]] = None,
        job_id: Optional[str] = None,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        temp_dir: Optional[Path] = None,
        timeout: Optional[float] = None,
        on_stdout: Optional[Callable[[str], Any]] = None,
        on_stderr: Optional[Callable[[str], Any]] = None,
        in_flight_target: Optional[Union[Path, str]] = None,
    ) -> CommandResult:
        """Executes a command via spawn_process, buffers stdout and stderr, enforces timeout,
        and ensures proper cleanup and unregistration.

        Args:
            argv_or_job_id: Command arguments list (shell=False) or job_id string.
            argv: Command arguments list if argv_or_job_id is job_id or when passed as kwarg.
            job_id: Optional job ID if argv_or_job_id is argv list.
            cwd: Optional working directory.
            env: Optional environment variables dictionary.
            temp_dir: Optional directory or file path for temporary partial file tracking and cleanup.
            timeout: Optional maximum execution duration in seconds. Raises TimeoutError if exceeded.
            on_stdout: Optional callback invoked for each stdout line in real-time.
            on_stderr: Optional callback invoked for each stderr line in real-time.
            in_flight_target: Optional target media file being downloaded, for targeted partial cleanup.

        Returns:
            CommandResult tuple of (returncode, stdout, stderr).

        Raises:
            ValueError: If argv is empty.
            TimeoutError: If execution exceeds the specified timeout.
            asyncio.CancelledError: If execution is cancelled.
        """
        if argv is not None:
            actual_argv = list(argv)
            if isinstance(argv_or_job_id, str):
                actual_job_id = argv_or_job_id
            else:
                actual_job_id = job_id
        elif isinstance(argv_or_job_id, str):
            actual_job_id = argv_or_job_id
            actual_argv = argv
        elif isinstance(argv_or_job_id, (list, tuple)):
            actual_argv = list(argv_or_job_id)
            actual_job_id = job_id if job_id is not None else None
        else:
            raise ValueError("First argument must be an argv list or job_id string, or pass argv keyword argument")

        if not actual_argv:
            raise ValueError("argv must be a non-empty list of command arguments")

        is_ephemeral = False
        if actual_job_id is None:
            actual_job_id = f"cmd-{uuid.uuid4().hex[:8]}"
            await self.register_job(actual_job_id, user_id="system", playlist_name="command")
            is_ephemeral = True
        elif self.get_job(actual_job_id) is None:
            await self.register_job(actual_job_id, user_id="system", playlist_name="command")
            is_ephemeral = True

        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        async def _capture_stdout(line: str):
            stdout_lines.append(line)
            if on_stdout:
                try:
                    res = on_stdout(line)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    logger.warning("[%s] on_stdout callback error: %s", actual_job_id, exc)

        async def _capture_stderr(line: str):
            stderr_lines.append(line)
            if on_stderr:
                try:
                    res = on_stderr(line)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    logger.warning("[%s] on_stderr callback error: %s", actual_job_id, exc)

        actual_in_flight_target: Optional[Path] = None
        if in_flight_target is not None:
            actual_in_flight_target = Path(in_flight_target)
        elif temp_dir is not None:
            temp_path = Path(temp_dir)
            if not temp_path.is_dir():
                actual_in_flight_target = temp_path

        handle: Optional[ProcessHandle] = None
        try:
            handle = await self.spawn_process(
                job_id=actual_job_id,
                argv=actual_argv,
                cwd=cwd,
                env=env,
                in_flight_target=actual_in_flight_target,
                on_stdout=_capture_stdout,
                on_stderr=_capture_stderr,
            )

            if timeout is not None:
                if timeout <= 0:
                    raise TimeoutError(f"Command '{actual_argv[0]}' timed out after {timeout} seconds")
                returncode = await asyncio.wait_for(
                    self.wait_process(handle),
                    timeout=timeout,
                )
            else:
                returncode = await self.wait_process(handle)

            stdout_str = "\n".join(stdout_lines)
            stderr_str = "\n".join(stderr_lines)
            return CommandResult(returncode, stdout_str, stderr_str)

        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("[%s] Command %s timed out after %ss", actual_job_id, actual_argv[:3], timeout)
            if handle is not None:
                if handle.pgid > 1:
                    try:
                        os.killpg(handle.pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    try:
                        handle.process.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    await asyncio.wait_for(handle.process.wait(), timeout=1.0)
                except Exception:
                    pass
                for task in (handle.stdout_task, handle.stderr_task):
                    if task and not task.done():
                        task.cancel()

            self._cleanup_temp_artifacts(
                actual_job_id,
                temp_dir=temp_dir,
                in_flight_target=actual_in_flight_target,
            )
            raise TimeoutError(f"Command '{actual_argv[0]}' timed out after {timeout} seconds")

        except asyncio.CancelledError:
            logger.info("[%s] Command %s was cancelled", actual_job_id, actual_argv[:3])
            if handle is not None:
                if handle.pgid > 1:
                    try:
                        os.killpg(handle.pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    try:
                        handle.process.kill()
                    except (ProcessLookupError, PermissionError):
                        pass
                try:
                    await asyncio.wait_for(handle.process.wait(), timeout=1.0)
                except Exception:
                    pass
                for task in (handle.stdout_task, handle.stderr_task):
                    if task and not task.done():
                        task.cancel()

            self._cleanup_temp_artifacts(
                actual_job_id,
                temp_dir=temp_dir,
                in_flight_target=actual_in_flight_target,
            )
            raise

        finally:
            if handle is not None:
                handle.is_terminated = True
                j = self.get_job(actual_job_id)
                if j:
                    async with j.lock:
                        j.active_processes.pop(handle.pid, None)
            if is_ephemeral:
                await self.unregister_job(actual_job_id)

    async def cancel_job(self, job_id: str) -> CancellationResult:
        """Gracefully cancels a specific job by PGID without collateral impact on sibling jobs."""
        start_t = time.time()
        job = self.get_job(job_id)
        if not job:
            raise KeyError(f"Job {job_id} does not exist or already finished")

        async with job.lock:
            if job.status == "cancelled":
                return CancellationResult(
                    job_id=job_id,
                    status="cancelled",
                    cleaned_files=0,
                    terminated_pids=[],
                    message="Job already cancelled",
                    duration_seconds=0.0,
                )
            job.status = "cancelling"
            job.cancel_event.set()

            # 1. Cancel high-level worker tasks
            for task in job.worker_tasks:
                if not task.done():
                    task.cancel()

            handles = list(job.active_processes.values())
            terminated_pids = [h.pid for h in handles]

            # 2. Phase 1: Send SIGTERM exclusively to targeted PGIDs (> 1)
            for handle in handles:
                if handle.pgid > 1:
                    try:
                        os.killpg(handle.pgid, signal.SIGTERM)
                        logger.info("[%s] Sent SIGTERM to PGID %d (PID %d)", job_id, handle.pgid, handle.pid)
                    except ProcessLookupError:
                        pass  # Already terminated
                    except PermissionError as exc:
                        logger.error("[%s] Permission error signalling PGID %d: %s", job_id, handle.pgid, exc)
                else:
                    logger.warning(
                        "[%s] Refusing to signal invalid PGID %d for PID %d in SIGTERM phase",
                        job_id,
                        handle.pgid,
                        handle.pid,
                    )

            # 3. Phase 2: Await graceful exit up to 5.0 seconds
            wait_tasks = [
                asyncio.create_task(handle.process.wait())
                for handle in handles
                if handle.process.returncode is None
            ]
            if wait_tasks:
                done, pending = await asyncio.wait(wait_tasks, timeout=5.0)

                # 4. Phase 3: Fallback SIGKILL for stubborn processes
                if pending:
                    for handle in handles:
                        if handle.process.returncode is None:
                            if handle.pgid > 1:
                                try:
                                    os.killpg(handle.pgid, signal.SIGKILL)
                                    logger.warning("[%s] Sent SIGKILL fallback to PGID %d", job_id, handle.pgid)
                                except ProcessLookupError:
                                    pass
                            else:
                                logger.warning(
                                    "[%s] Refusing to signal invalid PGID %d for PID %d in SIGKILL phase",
                                    job_id,
                                    handle.pgid,
                                    handle.pid,
                                )
                    kill_tasks = [
                        asyncio.create_task(handle.process.wait())
                        for handle in handles
                        if handle.process.returncode is None
                    ]
                    if kill_tasks:
                        await asyncio.wait(kill_tasks, timeout=1.0)

            # 5. Cancel stream reader tasks
            for handle in handles:
                for task in (handle.stdout_task, handle.stderr_task):
                    if task and not task.done():
                        task.cancel()

            # 6. Scoped Partial File Cleanup
            cleaned_count = self.cleanup_job_temp_files(job)

            job.status = "cancelled"
            job.active_processes.clear()

        # 7. Broadcast WebSocket event
        if self.ws_broadcaster:
            try:
                await self.ws_broadcaster.broadcast({
                    "event": "job_cancelled",
                    "job_id": job_id,
                    "reason": "User requested cancellation",
                    "cleaned_files": cleaned_count,
                    "timestamp": time.time(),
                })
            except Exception as exc:
                logger.debug("Failed to broadcast job_cancelled event: %s", exc)

        duration = time.time() - start_t
        logger.info("[%s] Cancellation complete in %.2fs (cleaned %d partial files)", job_id, duration, cleaned_count)

        return CancellationResult(
            job_id=job_id,
            status="cancelled",
            cleaned_files=cleaned_count,
            terminated_pids=terminated_pids,
            message=f"Job cancelled; {len(terminated_pids)} process groups terminated gracefully",
            duration_seconds=round(duration, 3),
        )

    def cleanup_job_temp_files(self, job: JobExecutionState) -> int:
        """Unlinks ONLY .part / .tmp files matching the stems of in-flight targets.
        Never unlinks files in job.completed_files. Never unlinks sibling files.
        """
        cleaned_count = 0
        for target in list(job.in_flight_targets):
            if target in job.completed_files:
                continue
            parent = target.parent
            if not parent.exists():
                continue

            valid_temp_names = compute_valid_temp_names(target)
            try:
                for item in parent.iterdir():
                    if not item.is_file():
                        continue
                    if item in job.completed_files:
                        continue
                    if item.name in valid_temp_names:
                        try:
                            item.unlink()
                            cleaned_count += 1
                            logger.info("[%s] Cleaned partial file: %s", job.job_id, item.name)
                        except OSError as exc:
                            logger.warning("[%s] Failed to unlink partial %s: %s", job.job_id, item, exc)
            except Exception as exc:
                logger.warning("[%s] Error scanning directory %s for cleanup: %s", job.job_id, parent, exc)

        return cleaned_count

    async def unregister_job(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.pop(job_id, None)

    async def shutdown(self) -> None:
        """Shutdown handler cancelling all active jobs gracefully."""
        active_ids = list(self.get_active_jobs().keys())
        for jid in active_ids:
            try:
                await self.cancel_job(jid)
            except Exception as exc:
                logger.warning("Error cancelling job %s during shutdown: %s", jid, exc)
