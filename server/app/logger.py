"""Bounded Rotating File Logging & WebSocket Forwarding Bridge for Jellyfin Music Downloader V2.

Provides:
1. Structured log formatting: [YYYY-MM-DD HH:MM:SS] [LEVEL] [job_id] Message
2. Bounded disk logging via RotatingFileHandler (5MB x 3 backups = max 15MB backups).
3. Thread-safe, non-blocking WebSocketLogHandler streaming real-time logs over /ws/events.
4. ContextVar tracking for ambient job_id propagation across asyncio tasks.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Context variable providing ambient job_id context for all logs in an async coroutine chain
current_job_id: ContextVar[str] = ContextVar("current_job_id", default="-")


# ==============================================================================
# 1. Structured Log Formatter
# ==============================================================================

class StructuredLogFormatter(logging.Formatter):
    """Enforces the mandatory log record structure:
    [YYYY-MM-DD HH:MM:SS] [LEVEL] [job_id] Message

    - Handles missing job_id attributes gracefully via ContextVar fallback.
    - Appends traceback/exception info cleanly if present.
    """

    def __init__(self) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        # Format timestamp: YYYY-MM-DD HH:MM:SS
        record_time = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")

        # Resolve job_id: check record attribute first, then ambient contextvar
        job_id = getattr(record, "job_id", None)
        if not job_id:
            job_id = current_job_id.get("-")

        # Format base message
        message = record.getMessage()

        # Append formatted exception if present
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                message = f"{message}\n{record.exc_text}"

        # Append stack info if present
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"

        return f"[{record_time}] [{record.levelname}] [{job_id}] {message}"


# ==============================================================================
# 2. WebSocket Real-Time Log Handler
# ==============================================================================

class WebSocketLogHandler(logging.Handler):
    """Captures log records at level >= INFO and queues them for WebSocket streaming.

    Hardened Architecture:
    - Zero blocking: synchronous emit() places records into an in-memory asyncio.Queue.
    - Thread-safe: uses call_soon_threadsafe when called from background threads or threadpools.
    - Re-entrancy guarded: ignores internal logging from the WebSocket module to prevent infinite loops.
    - Memory bounded: bounded queue (default 1,000 items) drops oldest logs under heavy saturation.
    """

    def __init__(
        self,
        queue: asyncio.Queue[Dict[str, Any]],
        loop: Optional[asyncio.AbstractEventLoop] = None,
        level: int = logging.INFO,
    ) -> None:
        super().__init__(level=level)
        self.queue = queue
        self.loop = loop
        self._in_emit = False

    def emit(self, record: logging.LogRecord) -> None:
        # Re-entrancy guard
        if self._in_emit:
            return

        # Level check
        if record.levelno < self.level:
            return

        # Suppress internal feedback loops and excessive ASGI access noise
        if record.name.startswith(("server.app.ws", "websockets", "uvicorn.access")):
            return


        self._in_emit = True
        try:
            job_id = getattr(record, "job_id", None)
            if not job_id:
                ctx_id = current_job_id.get("-")
                job_id = ctx_id if ctx_id != "-" else None

            log_event = {
                "event": "log",
                "job_id": job_id,
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "logger_name": record.name,
                "data": {
                    "level": record.levelname,
                    "message": record.getMessage(),
                    "logger_name": record.name,
                    "job_id": job_id,
                },
            }

            # Enqueue into asyncio.Queue
            target_loop = self.loop
            if not target_loop:
                try:
                    target_loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

            if target_loop and target_loop.is_running():
                current_running_loop = None
                try:
                    current_running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    pass

                if current_running_loop is target_loop:
                    self._enqueue_nonblocking(log_event)
                else:
                    target_loop.call_soon_threadsafe(self._enqueue_nonblocking, log_event)
            else:
                self._enqueue_nonblocking(log_event)
        except Exception:
            self.handleError(record)
        finally:
            self._in_emit = False

    def _enqueue_nonblocking(self, event_data: Dict[str, Any]) -> None:
        """Pushes into queue without blocking; evicts oldest item if full."""
        try:
            self.queue.put_nowait(event_data)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event_data)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass


# ==============================================================================
# 3. Log Broadcaster Worker Task
# ==============================================================================

async def log_broadcaster_worker(queue: asyncio.Queue[Dict[str, Any]], ws_manager: Any) -> None:
    """Background worker that continuously drains the log queue and broadcasts
    log events over WebSocket connections.
    """
    while True:
        try:
            event = await queue.get()
            try:
                await ws_manager.broadcast(event, job_id=event.get("job_id"))
            except Exception:
                pass
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            # Fallback error write to stderr to avoid logging recursion
            sys.stderr.write(f"Error in log broadcaster worker: {exc}\n")


# ==============================================================================
# 4. Logger Setup & Initialization
# ==============================================================================

def setup_logging(
    log_file_path: Path = Path("server/daemon.log"),
    level: int = logging.INFO,
    loop: Optional[asyncio.AbstractEventLoop] = None,
) -> Tuple[WebSocketLogHandler, asyncio.Queue[Dict[str, Any]]]:
    """Configures root logging with:
    1. RotatingFileHandler (5MB x 3 backups, max 15MB backups)
    2. StreamHandler (stdout)
    3. WebSocketLogHandler (streaming over /ws/events)

    Returns:
        (ws_handler, log_queue) for use in application lifespan.
    """
    log_file = Path(log_file_path).resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = StructuredLogFormatter()

    # 1. Bounded Rotating File Handler (5MB per file, max 3 backups = max 15MB total backups)
    file_handler = RotatingFileHandler(
        filename=str(log_file),
        maxBytes=5 * 1024 * 1024,  # 5,242,880 bytes
        backupCount=3,              # keeps daemon.log, daemon.log.1, daemon.log.2, daemon.log.3
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    # 2. Console Handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # 3. WebSocket Log Handler & Queue
    log_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1000)
    ws_handler = WebSocketLogHandler(queue=log_queue, loop=loop, level=level)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing instances of our handlers to avoid duplicates
    handlers_to_keep = [h for h in root_logger.handlers if not isinstance(h, (RotatingFileHandler, WebSocketLogHandler))]
    root_logger.handlers = handlers_to_keep
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(ws_handler)

    # Suppress verbose third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return ws_handler, log_queue
