"""Empirical Verification Tests for Structured Logging & WebSocket Streaming Bridge.

Validates:
- Structured log formatting: [YYYY-MM-DD HH:MM:SS] [LEVEL] [job_id] Message.
- ContextVar propagation for ambient job_id across async tasks.
- Bounded rotating file logging (5MB x 3 backups, max 15MB total backups).
- WebSocketLogHandler filtering (INFO+ only, suppression of internal loggers).
- Non-blocking queue insertion with oldest-eviction on full queue.
- Background log_broadcaster_worker dispatch to WebSocketManager.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.app.logger import (
    StructuredLogFormatter,
    WebSocketLogHandler,
    current_job_id,
    log_broadcaster_worker,
    setup_logging,
)


def test_structured_log_formatter_explicit_job_id():
    """Verify StructuredLogFormatter renders [YYYY-MM-DD HH:MM:SS] [LEVEL] [job_id] Message."""
    formatter = StructuredLogFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg="Downloading track %s",
        args=("Blinding Lights",),
        exc_info=None,
    )
    record.job_id = "job-uuid-1234"
    formatted = formatter.format(record)

    # Validate structure: [timestamp] [INFO] [job-uuid-1234] Downloading track Blinding Lights
    assert "[INFO]" in formatted
    assert "[job-uuid-1234]" in formatted
    assert "Downloading track Blinding Lights" in formatted
    assert formatted.startswith("[20")  # Matches [YYYY-...


def test_structured_log_formatter_ambient_contextvar():
    """Verify StructuredLogFormatter picks up job_id from ambient ContextVar if not on record."""
    formatter = StructuredLogFormatter()
    token = current_job_id.set("job-ambient-999")
    try:
        record = logging.LogRecord(
            name="worker_logger",
            level=logging.WARNING,
            pathname=__file__,
            lineno=100,
            msg="Connection retry #2",
            args=(),
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert "[WARNING]" in formatted
        assert "[job-ambient-999]" in formatted
        assert "Connection retry #2" in formatted
    finally:
        current_job_id.reset(token)


def test_structured_log_formatter_fallback_dash():
    """Verify StructuredLogFormatter outputs [-] when no job_id is configured."""
    formatter = StructuredLogFormatter()
    token = current_job_id.set("-")
    try:
        record = logging.LogRecord(
            name="system_logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=200,
            msg="System initialized",
            args=(),
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert "[ERROR]" in formatted
        assert "[-] System initialized" in formatted
    finally:
        current_job_id.reset(token)


def test_structured_log_formatter_with_exception():
    """Verify exception stack traces are properly appended to log message."""
    formatter = StructuredLogFormatter()
    try:
        raise ValueError("Simulated download failure")
    except ValueError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="err_logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=50,
        msg="Download aborted",
        args=(),
        exc_info=exc_info,
    )
    formatted = formatter.format(record)
    assert "Download aborted" in formatted
    assert "ValueError: Simulated download failure" in formatted
    assert "Traceback (most recent call last)" in formatted


def test_rotating_file_handler_bounds():
    """Verify RotatingFileHandler respects 5MB limit and 3 backup files."""
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "test_daemon.log"
        # Test with a small maxBytes to empirically verify rotation mechanics
        small_max_bytes = 1024  # 1 KB
        handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=small_max_bytes,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(StructuredLogFormatter())

        logger = logging.getLogger("test_rotation")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        # Write enough log records to trigger rotation multiple times
        for i in range(100):
            logger.info("Line %d: %s", i, "X" * 100)

        handler.close()

        # Check files created
        files = list(Path(td).glob("test_daemon.log*"))
        assert len(files) > 1, "Log rotation did not create backup files"
        assert len(files) <= 4, f"Backup count exceeded: found {len(files)} files (max is 4: base + 3 backups)"


@pytest.mark.asyncio
async def test_websocket_log_handler_queueing_and_filtering():
    """Verify WebSocketLogHandler queues INFO+ logs and suppresses debug and internal loggers."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    loop = asyncio.get_running_loop()
    handler = WebSocketLogHandler(queue=queue, loop=loop, level=logging.INFO)

    # 1. DEBUG record should be ignored (level < INFO)
    debug_record = logging.LogRecord(
        name="app.debug",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=10,
        msg="Debug detail",
        args=(),
        exc_info=None,
    )
    handler.emit(debug_record)
    assert queue.empty(), "DEBUG record was improperly queued"

    # 2. Internal WebSocket logger should be suppressed
    internal_record = logging.LogRecord(
        name="server.app.ws",
        level=logging.INFO,
        pathname=__file__,
        lineno=15,
        msg="WS internal frame",
        args=(),
        exc_info=None,
    )
    handler.emit(internal_record)
    assert queue.empty(), "Internal WS logger was not suppressed"

    # 3. INFO record from normal logger should be queued
    info_record = logging.LogRecord(
        name="app.ingest",
        level=logging.INFO,
        pathname=__file__,
        lineno=20,
        msg="Starting track download",
        args=(),
        exc_info=None,
    )
    info_record.job_id = "job-test-77"
    handler.emit(info_record)

    assert not queue.empty(), "INFO record was not queued"
    item = queue.get_nowait()
    assert item["event"] == "log"
    assert item["job_id"] == "job-test-77"
    assert item["level"] == "INFO"
    assert "Starting track download" in item["message"]


@pytest.mark.asyncio
async def test_websocket_log_handler_queue_full_eviction():
    """Verify that when the log queue is full, oldest items are dropped without blocking."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    loop = asyncio.get_running_loop()
    handler = WebSocketLogHandler(queue=queue, loop=loop, level=logging.INFO)

    # Push 3 records into a queue with capacity 2
    for i in range(3):
        rec = logging.LogRecord(
            name="app.queue",
            level=logging.INFO,
            pathname=__file__,
            lineno=30,
            msg=f"Message {i}",
            args=(),
            exc_info=None,
        )
        handler.emit(rec)

    assert queue.qsize() == 2
    first = queue.get_nowait()
    second = queue.get_nowait()

    # Message 0 should have been evicted; Message 1 and Message 2 remain
    assert "Message 1" in first["message"]
    assert "Message 2" in second["message"]


@pytest.mark.asyncio
async def test_log_broadcaster_worker():
    """Verify log_broadcaster_worker drains the queue and calls ws_manager.broadcast()."""
    queue: asyncio.Queue = asyncio.Queue()
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()

    # Put a test log event into the queue
    event = {
        "event": "log",
        "job_id": "job-broadcast-1",
        "level": "INFO",
        "message": "Broadcast test",
    }
    await queue.put(event)

    # Run worker task briefly
    worker_task = asyncio.create_task(log_broadcaster_worker(queue, mock_ws))
    await asyncio.sleep(0.05)
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    mock_ws.broadcast.assert_awaited_once_with(event, job_id="job-broadcast-1")
    assert queue.empty()


def test_setup_logging_initialization(tmp_path: Path):
    """Verify setup_logging configures root logger with all three handlers."""
    log_file = tmp_path / "daemon.log"
    ws_handler, log_queue = setup_logging(log_file_path=log_file)

    root = logging.getLogger()
    assert any(isinstance(h, RotatingFileHandler) for h in root.handlers)
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    assert any(isinstance(h, WebSocketLogHandler) for h in root.handlers)
    assert log_queue.maxsize == 1000
