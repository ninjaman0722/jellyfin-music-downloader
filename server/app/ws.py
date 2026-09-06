"""WebSocket Connection Manager & Real-Time Event Engine for Jellyfin Music Downloader V2.

Provides:
1. Pydantic models for all event frame schemas adhering to V2 protocol.
2. ConnectionManager handling client registration, targeted/global broadcasting,
   keep-alive heartbeats (30s), slow-consumer pruning, and graceful teardown.
3. /ws/events FastAPI WebSocket endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Set, Union
from uuid import uuid4

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("server.app.ws")

ws_router = APIRouter(tags=["websocket"])


# ==============================================================================
# 1. Event Models & Schemas
# ==============================================================================

def get_utc_timestamp() -> str:
    """Returns current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


class BaseEvent(BaseModel):
    """Base schema for all WebSocket broadcast frames."""
    event: str
    job_id: Optional[str] = None
    timestamp: str = Field(default_factory=get_utc_timestamp)
    data: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


class ConnectedEvent(BaseEvent):
    """Emitted immediately upon client connection acceptance."""
    event: Literal["connected"] = "connected"
    client_id: str
    data: Dict[str, Any] = Field(default_factory=dict)


class JobStartedEvent(BaseEvent):
    """Emitted when an ingestion job transitions from queue to active execution."""
    event: Literal["job_started"] = "job_started"
    job_id: str
    user_id: Optional[str] = None
    playlist_name: str
    total_tracks: int
    to_download: int
    already_present: int


class StageTransitionEvent(BaseEvent):
    """Emitted when pipeline advances between stages (1: Resolve, 2: Fetch, 3: Metadata, 4: Sync)."""
    event: Literal["stage_transition"] = "stage_transition"
    job_id: str
    stage: int
    stage_name: str
    description: str


class ProgressEvent(BaseEvent):
    """Emitted during active downloads.

    Provides dual-compatibility: top-level numeric fields (pct, current, total)
    as well as percentage, current_track, total_tracks for QML/Qt parsers.
    """
    event: Literal["progress"] = "progress"
    job_id: str
    percentage: float
    pct: float
    current_track: int
    current: int
    total_tracks: int
    total: int
    current_title: str
    speed: str = "0.0 MB/s"
    eta_seconds: int = 0
    status: str = "Downloading"


class TrackCompletedEvent(BaseEvent):
    """Emitted when a single track is downloaded, tagged, and finalized."""
    event: Literal["track_completed"] = "track_completed"
    job_id: str
    track_id: str
    track: str
    artist: str
    duration: float
    lyrics_synced: bool = False
    cover_embedded: bool = False
    path: str


class TrackFailedEvent(BaseEvent):
    """Emitted when a track fails after retry attempts."""
    event: Literal["track_failed"] = "track_failed"
    job_id: str
    track_id: str
    track: str
    artist: str
    error: str
    retrying: bool = False
    retry_count: int = 0


class LogEvent(BaseEvent):
    """Emitted in real-time when the server logs records at level >= INFO."""
    event: Literal["log"] = "log"
    level: str
    message: str
    logger_name: str = "daemon"


class JobCompletedEvent(BaseEvent):
    """Emitted when all stages of the ingestion job finish successfully."""
    event: Literal["job_completed"] = "job_completed"
    job_id: str
    playlist_id: Optional[str] = None
    playlist_name: str = ""
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    duration_seconds: float = 0.0


class JobCancelledEvent(BaseEvent):
    """Emitted when targeted cancellation of a job finishes."""
    event: Literal["job_cancelled"] = "job_cancelled"
    job_id: str
    reason: str = "User cancelled"
    cleaned_files: int = 0


class JobErrorEvent(BaseEvent):
    """Emitted when an unrecoverable error terminates a job."""
    event: Literal["job_error"] = "job_error"
    job_id: str
    error: str
    details: Optional[str] = None


class PingEvent(BaseEvent):
    """30-second keepalive ping frame."""
    event: Literal["ping"] = "ping"


# ==============================================================================
# 2. Client Session & Connection Manager
# ==============================================================================

@dataclass
class ClientSession:
    """Metadata for a connected WebSocket client."""
    websocket: WebSocket
    client_id: str
    connected_at: float = field(default_factory=time.time)
    last_pong: float = field(default_factory=time.time)
    subscribed_jobs: Set[str] = field(default_factory=set)


class ConnectionManager:
    """Thread-safe and async-safe WebSocket Connection Manager.

    Features:
    - Connection registration and lifecycle tracking.
    - Global and job-filtered broadcast dispatch.
    - Automatic dead-socket pruning.
    - 30-second ping/pong keepalive.
    - Graceful connection draining on daemon shutdown.
    """

    def __init__(self) -> None:
        self._connections: Dict[WebSocket, ClientSession] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_id: Optional[str] = None) -> ClientSession:
        """Accepts an incoming WebSocket connection and registers the session."""
        await websocket.accept()
        cid = client_id.strip() if client_id and client_id.strip() else f"client-{uuid4().hex[:8]}"
        session = ClientSession(websocket=websocket, client_id=cid)

        async with self._lock:
            self._connections[websocket] = session
            count = len(self._connections)

        logger.info("WebSocket client connected: id=%s (Total active: %d)", cid, count)
        return session

    async def disconnect(self, websocket: WebSocket) -> None:
        """Removes a WebSocket connection and cleans up session state."""
        async with self._lock:
            session = self._connections.pop(websocket, None)
            count = len(self._connections)

        if session:
            duration = round(time.time() - session.connected_at, 1)
            logger.info(
                "WebSocket client disconnected: id=%s (Connected for %ss, Total active: %d)",
                session.client_id,
                duration,
                count,
            )

    def subscribe_job(self, websocket: WebSocket, job_id: str) -> None:
        """Subscribes a client session to events for a specific job_id."""
        session = self._connections.get(websocket)
        if session and job_id:
            session.subscribed_jobs.add(job_id)

    def unsubscribe_job(self, websocket: WebSocket, job_id: str) -> None:
        """Unsubscribes a client session from events for a specific job_id."""
        session = self._connections.get(websocket)
        if session and job_id:
            session.subscribed_jobs.discard(job_id)

    def count(self) -> int:
        """Returns the number of active WebSocket connections."""
        return len(self._connections)

    async def broadcast(self, event: Union[BaseEvent, dict], job_id: Optional[str] = None) -> None:
        """Broadcasts an event frame to all connected clients (or job subscribers).

        - Serializes event payload to JSON string once.
        - Pushes concurrently across all matching sockets via asyncio.gather.
        - Catches closed sockets and marks them for pruning without raising to caller.
        """
        if not self._connections:
            return

        # 1. Serialize to JSON text and resolve event_job_id
        if isinstance(event, BaseEvent):
            payload_str = event.model_dump_json()
            event_job_id = event.job_id or job_id
        elif isinstance(event, dict):
            if "timestamp" not in event:
                event["timestamp"] = get_utc_timestamp()
            payload_str = json.dumps(event)
            event_job_id = event.get("job_id") or job_id
        else:
            payload_str = str(event)
            event_job_id = job_id

        # 2. Select target sessions
        async with self._lock:
            sessions = list(self._connections.values())

        targets: List[WebSocket] = []
        for s in sessions:
            if not s.subscribed_jobs:
                targets.append(s.websocket)
            elif event_job_id and event_job_id in s.subscribed_jobs:
                targets.append(s.websocket)

        if not targets:
            return

        # 3. Concurrent dispatch with exception suppression
        async def _safe_send(ws: WebSocket) -> Optional[WebSocket]:
            try:
                await ws.send_text(payload_str)
                return None
            except (WebSocketDisconnect, RuntimeError, ConnectionResetError):
                return ws
            except Exception as exc:
                logger.debug("Send failure on websocket: %s", exc)
                return ws

        results = await asyncio.gather(*(_safe_send(ws) for ws in targets), return_exceptions=True)

        # 4. Prune dead sockets
        dead_sockets = [res for res in results if res is not None and not isinstance(res, BaseException)]
        if dead_sockets:
            for ws in dead_sockets:
                await self.disconnect(ws)


    async def send_heartbeat(self) -> None:
        """Sends a 30-second ping frame to all connected clients."""
        if self.count() == 0:
            return

        ping = PingEvent(data={"server_time": time.time()})
        await self.broadcast(ping)

    async def close_all(self, code: int = 1001, reason: str = "Server shutting down") -> None:
        """Gracefully closes all active WebSocket connections during daemon shutdown."""
        async with self._lock:
            sockets = list(self._connections.keys())
            self._connections.clear()

        for ws in sockets:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass
        logger.info("Closed %d active WebSocket connections", len(sockets))


# Alias for backward compatibility
WebSocketManager = ConnectionManager

# Global connection manager singleton
ws_manager = ConnectionManager()


# ==============================================================================
# 3. Background Heartbeat & Keep-Alive Task
# ==============================================================================

async def heartbeat_worker(manager: ConnectionManager, interval: float = 30.0) -> None:
    """Periodically sends ping frames every interval (default 30s) to maintain
    TCP connection state through NAT gateways, reverse proxies, and firewalls.
    """
    while True:
        try:
            await asyncio.sleep(interval)
            await manager.send_heartbeat()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error in WebSocket heartbeat worker: %s", exc)


# ==============================================================================
# 4. FastAPI WebSocket Endpoint (/ws/events)
# ==============================================================================

@ws_router.websocket("/ws/events")
async def websocket_events_endpoint(
    websocket: WebSocket,
    client_id: Optional[str] = Query(None),
) -> None:
    """Persistent WebSocket endpoint for real-time daemon events.

    Query Params:
        client_id (str, optional): Unique client identifier. Auto-generated if omitted.
    """
    session = await ws_manager.connect(websocket, client_id)
    try:
        # Emit initial connected handshake event
        welcome_event = ConnectedEvent(
            client_id=session.client_id,
            data={
                "message": "Connected to Jellyfin Music Downloader V2 Event Stream",
                "active_clients": ws_manager.count(),
                "ping_interval_seconds": 30,
            },
        )
        await websocket.send_text(welcome_event.model_dump_json())

        # Process incoming client control frames (pong, subscribe, unsubscribe)
        while True:
            raw_msg = await websocket.receive_text()
            if raw_msg == "ping":
                await websocket.send_text("pong")
                continue

            try:
                msg = json.loads(raw_msg)
                action = msg.get("action") or msg.get("event")

                if action == "pong":
                    session.last_pong = time.time()
                elif action == "ping":
                    await websocket.send_text(json.dumps({"event": "pong", "timestamp": get_utc_timestamp()}))
                elif action == "subscribe":
                    target_job = msg.get("job_id")
                    if target_job:
                        ws_manager.subscribe_job(websocket, str(target_job))
                elif action == "unsubscribe":
                    target_job = msg.get("job_id")
                    if target_job:
                        ws_manager.unsubscribe_job(websocket, str(target_job))
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("WebSocket session error for %s: %s", session.client_id, exc)
    finally:
        await ws_manager.disconnect(websocket)
