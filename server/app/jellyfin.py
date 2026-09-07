"""server/app/jellyfin.py - Official Jellyfin REST API Client & Multi-User Isolation Engine.

Guarantees:
1. Pure REST client architecture (zero raw database connections or queries).
2. Zero filesystem markup tampering (all playlist updates via REST API).
3. Strict Multi-User Privacy Isolation (OwnerUserId enforcement).
4. Multi-Tier Track Resolution (relative subpath, filename, Unicode NFKC title/artist).
5. Strict Sequential 50-Item Chunked Appending (preserving source sequence).
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

import httpx
from pydantic import BaseModel, Field

from server.app.indexer import (
    artists_match,
    normalize_key,
    strip_parentheticals,
)

logger = logging.getLogger("jellyfin_music_daemon.jellyfin")

HOUSEHOLD_USER_ID = "00000000000000000000000000000000"
HOUSEHOLD_USER_NAME = "Household (Shared)"
SHARED_USER_ID = HOUSEHOLD_USER_ID
MAX_CHUNK_SIZE = 50


# ==============================================================================
# 1. Custom Exceptions
# ==============================================================================

class JellyfinError(Exception):
    """Base exception for all Jellyfin API client interactions."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_text = response_text

    def __str__(self) -> str:
        if self.status_code:
            return f"[{self.status_code}] {self.message}"
        return self.message


class JellyfinConnectionError(JellyfinError):
    """Network connection failure, DNS resolution failure, or server unreachable."""
    pass


class JellyfinTimeoutError(JellyfinConnectionError):
    """HTTP request timed out before receiving a response."""
    pass


class JellyfinNetworkError(JellyfinConnectionError):
    """Alias for connection and network errors."""
    pass


class JellyfinAuthError(JellyfinError):
    """HTTP 401 Unauthorized or 403 Forbidden: Invalid or missing API token."""
    pass


class JellyfinNotFoundError(JellyfinError):
    """HTTP 404 Not Found: Requested user, playlist, or library item does not exist."""
    pass


class JellyfinServerError(JellyfinError):
    """HTTP 500, 502, 503, 504: Jellyfin server error or service unavailable."""
    pass


class JellyfinValidationError(JellyfinError):
    """HTTP 400 Bad Request or unexpected payload structure."""
    pass


class JellyfinPermissionError(JellyfinError):
    """Raised when cross-user mutation or access violation is attempted."""
    pass


# ==============================================================================
# 2. Data Models
# ==============================================================================

class JellyfinUser(BaseModel):
    """Validated Jellyfin user model."""
    id: str = Field(..., description="Jellyfin User GUID")
    name: str = Field(..., description="Display username")
    has_password: bool = Field(default=False, description="Whether user account has a password set")
    is_admin: bool = Field(default=False, description="Whether user possesses administrator privileges")
    is_disabled: bool = Field(default=False, description="Whether account is currently disabled")
    is_household: bool = Field(default=False, description="True if this is the virtual Household shared account")


class PlaylistSummary(BaseModel):
    """Jellyfin audio playlist summary with dual track_count and item_count support."""
    id: str = Field(..., description="Jellyfin Playlist GUID")
    name: str = Field(..., description="Playlist display name")
    track_count: int = Field(default=0, description="Total number of tracks in playlist")
    item_count: int = Field(default=0, description="Alias for track_count")
    user_id: Optional[str] = Field(default=None, description="Queried user context")
    owner_user_id: Optional[str] = Field(default=None, description="Actual owner GUID of the playlist")

    def model_post_init(self, __context: Any) -> None:
        if self.item_count == 0 and self.track_count > 0:
            self.item_count = self.track_count
        elif self.track_count == 0 and self.item_count > 0:
            self.track_count = self.item_count


# Alias JellyfinPlaylist to PlaylistSummary for complete contract compatibility
JellyfinPlaylist = PlaylistSummary


class VirtualFolder(BaseModel):
    """Jellyfin Virtual Folder / Library collection."""
    name: str = Field(default="", description="Virtual folder name (e.g. Music)")
    item_id: str = Field(default="", description="Collection BaseItem GUID used for targeted refresh")
    collection_type: Optional[str] = Field(default=None, description="Collection type (music, movies, playlists)")
    locations: List[str] = Field(default_factory=list, description="Host storage paths bound to folder")

    def __init__(self, **data: Any):
        # Support both 'id' and 'item_id' during instantiation
        if "id" in data and "item_id" not in data:
            data["item_id"] = data["id"]
        super().__init__(**data)

    @property
    def id(self) -> str:
        """Alias for item_id."""
        return self.item_id


class RefreshResult(BaseModel):
    """Result of triggering a library scan."""
    success: bool = Field(..., description="Whether refresh request was accepted by Jellyfin")
    target: str = Field(..., description="Identifier of refreshed target ('item:<guid>' or 'library:global')")
    message: str = Field(..., description="Human-readable status summary")
    status_code: int = Field(default=204, description="HTTP status code returned by Jellyfin")


# ==============================================================================
# 3. JellyfinClient Implementation
# ==============================================================================

class JellyfinClient:
    """Production-grade asynchronous Jellyfin REST API client.

    Guarantees:
    - HTTP connection pooling via httpx.AsyncClient (max 20 connections, 10 keepalive).
    - Multi-header authentication (X-Emby-Token and MediaBrowser Authorization).
    - Non-blocking dynamic virtual folder discovery and targeted library refresh.
    - User discovery and strictly scoped user playlist queries.
    - Resilient retry mechanism with exponential backoff on transient errors.
    - Multi-tier track item ID resolution preserving source playlist sequence.
    - Sequential 50-item chunking preventing URI length overflow.
    - Strict multi-user isolation by OwnerUserId.
    - Official Jellyfin REST endpoints exclusively.
    """

    CLIENT_NAME = "Jellyfin Music Downloader"
    CLIENT_VERSION = "2.0.0"
    DEVICE_NAME = "Omarchy Linux"
    DEFAULT_TIMEOUT = 15.0
    CONNECT_TIMEOUT = 5.0

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8096",
        token: str = "",
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        device_id: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Accept token or api_key
        effective_token = token if token else (api_key or "")
        self._token = effective_token.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.device_id = device_id or self._detect_device_id()
        self._custom_client = client is not None
        self._client: Optional[httpx.AsyncClient] = client
        self.limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=30.0,
        )

    @property
    def token(self) -> str:
        return self._token

    @token.setter
    def token(self, val: str) -> None:
        self._token = val.strip()

    @property
    def api_key(self) -> str:
        """Alias for self.token."""
        return self._token

    @property
    def client(self) -> httpx.AsyncClient:
        """Access underlying httpx.AsyncClient instance."""
        return self._get_client()

    def _detect_device_id(self) -> str:
        """Determines persistent device ID from system configuration or fallback."""
        machine_id_path = Path("/etc/machine-id")
        if machine_id_path.is_file():
            try:
                mid = machine_id_path.read_text(encoding="utf-8").strip()
                if mid:
                    return mid[:32]
            except Exception:
                pass
        return "omarchy-jellyfin-music-app"

    def _get_headers(self, custom_token: Optional[str] = None) -> Dict[str, str]:
        """Constructs standardized Jellyfin REST headers."""
        active_token = (custom_token or self._token).strip()
        auth_header = (
            f'MediaBrowser Client="{self.CLIENT_NAME}", '
            f'Device="{self.DEVICE_NAME}", '
            f'DeviceId="{self.device_id}", '
            f'Version="{self.CLIENT_VERSION}"'
        )
        if active_token:
            auth_header += f', Token="{active_token}"'

        headers = {
            "Accept": "application/json",
            "User-Agent": f"{self.CLIENT_NAME}/{self.CLIENT_VERSION}",
            "Authorization": auth_header,
        }
        if active_token:
            headers["X-Emby-Token"] = active_token
        return headers

    def _get_client(self) -> httpx.AsyncClient:
        """Retrieves or instantiates the managed httpx.AsyncClient."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                limits=self.limits,
                timeout=httpx.Timeout(self.timeout, connect=self.CONNECT_TIMEOUT),
                headers=self._get_headers(),
            )
        return self._client

    async def aclose(self) -> None:
        """Closes the underlying HTTP client session if internally managed."""
        if self._client and not self._client.is_closed and not self._custom_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "JellyfinClient":
        self._get_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        custom_token: Optional[str] = None,
    ) -> httpx.Response:
        """Issues an HTTP request with exponential backoff retry on transient errors."""
        headers = self._get_headers(custom_token)
        client = self._get_client()

        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.request(
                    method=method,
                    url=path,
                    params=params,
                    json=json_body,
                    headers=headers,
                )

                # Successful status codes
                if resp.status_code in (200, 201, 204):
                    return resp

                # Fail-fast status codes
                if resp.status_code in (401, 403):
                    raise JellyfinAuthError(
                        f"Authentication failed ({resp.status_code}) on {method} {path}. Verify API token.",
                        status_code=resp.status_code,
                        response_text=resp.text,
                    )
                if resp.status_code == 404:
                    raise JellyfinNotFoundError(
                        f"Resource not found (404) on {method} {path}.",
                        status_code=404,
                        response_text=resp.text,
                    )
                if resp.status_code == 400:
                    raise JellyfinValidationError(
                        f"Bad request (400) on {method} {path}: {resp.text}",
                        status_code=400,
                        response_text=resp.text,
                    )

                # Transient server errors: retry with exponential backoff
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        delay = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            "Jellyfin transient error HTTP %d on %s %s. Retrying in %.2fs (%d/%d)...",
                            resp.status_code, method, path, delay, attempt + 1, self.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise JellyfinServerError(
                        f"Jellyfin server error ({resp.status_code}) on {method} {path} after {self.max_retries} retries: {resp.text}",
                        status_code=resp.status_code,
                        response_text=resp.text,
                    )

                resp.raise_for_status()
                return resp

            except (httpx.ConnectError, httpx.NetworkError) as exc:
                if attempt < self.max_retries:
                    delay = self.retry_backoff * (2 ** attempt)
                    logger.warning(
                        "Jellyfin connection error (%s) on %s %s. Retrying in %.2fs (%d/%d)...",
                        exc, method, path, delay, attempt + 1, self.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise JellyfinConnectionError(
                    f"Unable to connect to Jellyfin at {self.base_url}: {exc}",
                ) from exc

            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    delay = self.retry_backoff * (2 ** attempt)
                    logger.warning(
                        "Jellyfin request timeout on %s %s. Retrying in %.2fs (%d/%d)...",
                        method, path, delay, attempt + 1, self.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise JellyfinTimeoutError(
                    f"Request timed out on {method} {path} after {self.timeout}s: {exc}",
                ) from exc

        raise JellyfinServerError(f"Request {method} {path} failed exhausted retries.")

    # ==========================================================================
    # User Discovery & Scoped Playlists
    # ==========================================================================

    async def get_users(self, include_household: bool = True) -> List[JellyfinUser]:
        """Queries GET /Users, returning active Jellyfin users with permissions."""
        resp = await self._request("GET", "/Users")
        raw_users = resp.json()
        if not isinstance(raw_users, list):
            raise JellyfinValidationError("Invalid response from /Users: expected list", response_text=resp.text)

        users: List[JellyfinUser] = []
        for u in raw_users:
            policy = u.get("Policy", {})
            is_disabled = policy.get("IsDisabled", False)
            if is_disabled:
                continue

            users.append(JellyfinUser(
                id=u["Id"],
                name=u.get("Name", "Unnamed"),
                has_password=u.get("HasPassword", False),
                is_admin=policy.get("IsAdministrator", False),
                is_disabled=False,
                is_household=False,
            ))

        if include_household:
            users.append(JellyfinUser(
                id=HOUSEHOLD_USER_ID,
                name=HOUSEHOLD_USER_NAME,
                has_password=False,
                is_admin=False,
                is_disabled=False,
                is_household=True,
            ))

        return users

    async def get_user_playlists(self, user_id: str) -> List[PlaylistSummary]:
        """Queries GET /Users/{user_id}/Items?includeItemTypes=Playlist scoped strictly by user_id."""
        if not user_id:
            return []

        if user_id == HOUSEHOLD_USER_ID:
            path = "/Items"
        else:
            path = f"/Users/{user_id}/Items"

        params = {
            "includeItemTypes": "Playlist",
            "recursive": "true",
            "sortBy": "SortName",
            "sortOrder": "Ascending",
            "fields": "ChildCount,OwnerUserId,OpenAccess",
        }

        resp = await self._request("GET", path, params=params)
        data = resp.json()
        items = data.get("Items", [])

        # Household (Shared) only includes playlists with no individual user restrictions
        if user_id in (HOUSEHOLD_USER_ID, SHARED_USER_ID):
            async def _has_users(pid: str) -> bool:
                try:
                    r = await self._request("GET", f"/Playlists/{pid}/Users")
                    return len(r.json()) > 0
                except Exception:
                    return False

            checks = await asyncio.gather(*[_has_users(it["Id"]) for it in items])
            items = [it for it, has_u in zip(items, checks) if not has_u]

        playlists: List[PlaylistSummary] = []
        for item in items:
            owner = item.get("OwnerUserId")
            if owner and owner != user_id and owner not in (HOUSEHOLD_USER_ID, SHARED_USER_ID):
                continue

            count = item.get("ChildCount", 0)
            playlists.append(PlaylistSummary(
                id=item["Id"],
                name=item.get("Name", "Untitled"),
                track_count=count,
                item_count=count,
                user_id=user_id,
                owner_user_id=owner or user_id,
            ))

        return playlists

    # ==========================================================================
    # Virtual Folders & Library Refresh
    # ==========================================================================

    async def get_virtual_folders(self) -> List[VirtualFolder]:
        """Queries GET /Library/VirtualFolders to locate library roots and ItemIds."""
        resp = await self._request("GET", "/Library/VirtualFolders")
        data = resp.json()
        if not isinstance(data, list):
            raise JellyfinValidationError(
                "Invalid response from /Library/VirtualFolders: expected list",
                response_text=resp.text,
            )

        folders: List[VirtualFolder] = []
        for vf in data:
            folders.append(VirtualFolder(
                name=vf.get("Name", ""),
                item_id=vf.get("ItemId", ""),
                collection_type=vf.get("CollectionType"),
                locations=vf.get("Locations", []) or [],
            ))
        return folders

    async def find_music_library(
        self,
        music_dir: Optional[Union[Path, str]] = None,
    ) -> Optional[VirtualFolder]:
        """Discovers the music virtual folder by collection type, location path, or name."""
        folders = await self.get_virtual_folders()
        if not folders:
            return None

        # 1. Match by official CollectionType == "music"
        for f in folders:
            if f.collection_type and f.collection_type.lower() == "music":
                return f

        # 2. Match by configured music_dir matching one of the locations
        if music_dir:
            clean_music_dir = str(Path(music_dir).resolve()).rstrip("/")
            for f in folders:
                for loc in f.locations:
                    clean_loc = str(Path(loc).resolve()).rstrip("/")
                    if clean_music_dir == clean_loc or clean_music_dir.startswith(clean_loc):
                        return f

        # 3. Fallback: match by folder name
        for f in folders:
            if f.name.lower() in ("music", "songs", "tracks"):
                return f

        return None

    async def refresh_library(
        self,
        item_id: Optional[str] = None,
        folder_id: Optional[str] = None,
        music_dir: Optional[Union[Path, str]] = None,
        fallback_global: bool = True,
    ) -> RefreshResult:
        """Triggers a non-blocking targeted library refresh on Jellyfin.

        If item_id or folder_id is given, triggers POST /Items/{item_id}/Refresh.
        If omitted, discovers the music folder dynamically via find_music_library().
        Falls back to POST /Library/Refresh if targeted refresh fails or folder not found.
        """
        target_id = item_id or folder_id

        if not target_id:
            try:
                music_folder = await self.find_music_library(music_dir)
                if music_folder and music_folder.item_id:
                    target_id = music_folder.item_id
            except Exception as e:
                logger.warning("Dynamic music library discovery failed (%s); proceeding with fallback", e)

        if target_id:
            try:
                path = f"/Items/{target_id}/Refresh"
                params = {
                    "metadataRefreshMode": "Default",
                    "imageRefreshMode": "Default",
                    "replaceAllMetadata": "false",
                    "replaceAllImages": "false",
                }
                resp = await self._request("POST", path, params=params)
                logger.info("Triggered targeted Jellyfin library refresh for item %s", target_id)
                return RefreshResult(
                    success=True,
                    target=f"item:{target_id}",
                    message="Targeted library refresh triggered successfully",
                    status_code=resp.status_code,
                )
            except (JellyfinNotFoundError, JellyfinValidationError) as exc:
                logger.warning("Targeted refresh on %s failed (%s). Falling back to global refresh", target_id, exc)
                if not fallback_global:
                    raise

        if fallback_global:
            resp = await self._request("POST", "/Library/Refresh")
            logger.info("Triggered global Jellyfin library refresh (/Library/Refresh)")
            return RefreshResult(
                success=True,
                target="library:global",
                message="Global library refresh triggered successfully",
                status_code=resp.status_code,
            )

        raise JellyfinNotFoundError("No music virtual folder found and fallback_global is False")

    # ==========================================================================
    # Playlist Management & Scoped Creation
    # ==========================================================================

    async def create_playlist(
        self,
        name: str,
        user_id: str,
        item_ids: Optional[List[str]] = None,
    ) -> str:
        """Creates an audio playlist scoped to user_id via POST /Playlists with JSON body."""
        clean_name = name.strip() or "Downloads"
        payload = {
            "Name": clean_name,
            "Ids": item_ids or [],
            "UserId": user_id,
            "MediaType": "Audio",
            "IsPublic": False,
        }
        params = {
            "name": clean_name,
            "userId": user_id,
            "mediaType": "Audio",
        }
        resp = await self._request("POST", "/Playlists", params=params, json_body=payload)
        data = resp.json()
        playlist_id = data.get("Id")
        if not playlist_id:
            raise JellyfinValidationError(
                "Playlist creation succeeded but response lacked 'Id'",
                response_text=resp.text,
            )

        if user_id:
            try:
                await self._request(
                    "POST",
                    f"/Playlists/{playlist_id}/Users/{user_id}",
                    json_body={"UserId": user_id, "CanEdit": True},
                )
            except Exception as exc:
                logger.debug("Optional user restriction endpoint returned: %s", exc)

        return playlist_id

    async def create_or_get_playlist(
        self,
        user_id: str,
        playlist_name: str,
    ) -> str:
        """Finds existing playlist owned by user_id or creates a fresh one via POST /Playlists.

        Guarantees:
        - Scopes search strictly to user_id (User A cannot match or hijack User B's playlist).
        - Creates playlist with JSON body payload containing Name, UserId, and MediaType='Audio'.
        """
        clean_name = playlist_name.strip() or "Downloads"

        # 1. Check existing playlists owned by this user
        existing_playlists = await self.get_user_playlists(user_id)
        for pl in existing_playlists:
            if pl.name.strip().lower() == clean_name.lower():
                logger.info("Found existing playlist '%s' (%s) for user %s", clean_name, pl.id, user_id)
                return pl.id

        # 2. Not found; create fresh playlist
        return await self.create_playlist(name=clean_name, user_id=user_id)

    async def get_playlist_items(self, user_id: str, playlist_id: str) -> List[str]:
        """Fetch item GUIDs currently contained in playlist_id."""
        url = f"/Playlists/{playlist_id}/Items"
        params = {"userId": user_id}
        try:
            resp = await self._request("GET", url, params=params)
            data = resp.json()
            return [item["Id"] for item in data.get("Items", []) if "Id" in item]
        except Exception as e:
            logger.debug("Failed to query existing playlist items for %s: %s", playlist_id, e)
            return []

    # ==========================================================================
    # 50-Track Sequential Chunked Appending
    # ==========================================================================

    async def add_items_to_playlist(
        self,
        user_id: str,
        playlist_id: str,
        item_ids: List[str],
        chunk_size: int = MAX_CHUNK_SIZE,
        deduplicate: bool = False,
    ) -> bool:
        """Appends tracks to playlist in strict sequential chunks of at most 50 items.

        Supports flexible argument ordering:
        - (user_id, playlist_id, item_ids, ...)
        - (playlist_id, item_ids, user_id, ...)
        """
        # Flexible argument normalization
        if isinstance(user_id, str) and isinstance(playlist_id, list) and isinstance(item_ids, str):
            # Signature was called as (playlist_id, item_ids, user_id)
            actual_pl_id = user_id
            actual_items = playlist_id
            actual_user_id = item_ids
        elif isinstance(item_ids, list):
            actual_user_id = user_id
            actual_pl_id = playlist_id
            actual_items = item_ids
        else:
            raise ValueError("add_items_to_playlist: invalid argument types for playlist appending")

        if not actual_items:
            return True

        if chunk_size > MAX_CHUNK_SIZE or chunk_size <= 0:
            logger.warning("add_items_to_playlist: invalid chunk_size %d; resetting to 50", chunk_size)
            chunk_size = MAX_CHUNK_SIZE

        target_ids = list(actual_items)

        if deduplicate:
            existing_ids = set(await self.get_playlist_items(actual_user_id, actual_pl_id))
            target_ids = [cid for cid in target_ids if cid not in existing_ids]
            if not target_ids:
                logger.info("All %d tracks already exist in playlist %s; skipping append.", len(actual_items), actual_pl_id)
                return True

        total = len(target_ids)
        num_chunks = (total + chunk_size - 1) // chunk_size
        logger.info(
            "Appending %d items to playlist %s in %d chunks (user: %s)...",
            total, actual_pl_id, num_chunks, actual_user_id,
        )

        append_path = f"/Playlists/{actual_pl_id}/Items"

        for chunk_idx, i in enumerate(range(0, total, chunk_size), start=1):
            chunk = target_ids[i:i + chunk_size]
            ids_param = ",".join(chunk)
            params = {
                "ids": ids_param,
                "userId": actual_user_id,
            }

            chunk_success = False
            for attempt in range(1, 4):
                try:
                    resp = await self._request("POST", append_path, params=params)
                    if resp.status_code in (200, 204):
                        chunk_success = True
                        break
                except (JellyfinAuthError, JellyfinPermissionError):
                    raise JellyfinPermissionError(f"Cross-user append forbidden on playlist {actual_pl_id}")
                except Exception as exc:
                    logger.warning(
                        "Chunk %d/%d attempt %d error on playlist %s: %s",
                        chunk_idx, num_chunks, attempt, actual_pl_id, exc,
                    )
                    if attempt < 3:
                        await asyncio.sleep(0.5 * attempt)

            if not chunk_success:
                logger.error(
                    "Failed to append chunk %d/%d (%d tracks) to playlist %s",
                    chunk_idx, num_chunks, len(chunk), actual_pl_id,
                )
                return False

        logger.info("Successfully appended all %d tracks to playlist %s.", total, actual_pl_id)
        return True

    # ==========================================================================
    # Multi-Tier Track Item ID Discovery
    # ==========================================================================

    async def resolve_track_item_ids(
        self,
        user_id: str,
        tracks: List[Dict[str, Any]],
        music_dir: Optional[Path] = None,
        max_retries: int = 3,
        retry_delay: float = 1.5,
        music_folder_id: Optional[str] = None,
    ) -> List[str]:
        """Discovers Jellyfin Audio Item GUIDs for downloaded or pre-existing tracks.

        Strategy:
        1. Query GET /Users/{user_id}/Items?includeItemTypes=Audio&recursive=true&fields=Path,Artists,AlbumArtist,Album.
        2. Execute multi-tier matching:
           - Tier 1: Relative subpath suffix matching.
           - Tier 2: Filename + directory/artist verification.
           - Tier 3: Unicode NFKC normalized title + artist match.
           - Tier 4: Stripped parenthetical title match.
           - Tier 5: Targeted single-track REST search fallback.
        3. Retry polling loop to handle background library scan completion.
        4. Preserves exact input playlist sequence order.
        """
        if not tracks:
            return []

        resolved_ids: List[str] = []
        unresolved_indices: Set[int] = set(range(len(tracks)))
        track_to_id_map: Dict[int, str] = {}

        for attempt in range(1, max_retries + 1):
            path = "/Items" if user_id == HOUSEHOLD_USER_ID else f"/Users/{user_id}/Items"
            params = {
                "includeItemTypes": "Audio",
                "recursive": "true",
                "fields": "Path,Artists,AlbumArtist,Album",
            }
            if music_folder_id:
                params["parentId"] = music_folder_id

            try:
                resp = await self._request("GET", path, params=params)
                data = resp.json()
                jf_items = data.get("Items", [])
            except Exception as exc:
                logger.warning("Failed to fetch audio items from Jellyfin (attempt %d/%d): %s", attempt, max_retries, exc)
                jf_items = []

            # Attempt matching for unresolved tracks
            for idx in list(unresolved_indices):
                track = tracks[idx]
                matched_id = self._match_single_track(track, jf_items, music_dir)
                if matched_id:
                    track_to_id_map[idx] = matched_id
                    unresolved_indices.remove(idx)

            if not unresolved_indices:
                break

            if attempt < max_retries:
                logger.debug(
                    "Track resolution attempt %d/%d: %d/%d tracks matched; waiting %.1fs for scanner...",
                    attempt, max_retries, len(tracks) - len(unresolved_indices), len(tracks), retry_delay,
                )
                await asyncio.sleep(retry_delay)

        # Fallback: Targeted REST search for remaining unmatched tracks
        if unresolved_indices:
            search_path = "/Items" if user_id == HOUSEHOLD_USER_ID else f"/Users/{user_id}/Items"
            for idx in list(unresolved_indices):
                track = tracks[idx]
                title = track.get("title", "")
                if not title:
                    continue
                try:
                    search_resp = await self._request(
                        "GET",
                        search_path,
                        params={
                            "includeItemTypes": "Audio",
                            "recursive": "true",
                            "searchTerm": title,
                            "fields": "Path,Artists,AlbumArtist,Album",
                            "limit": 10,
                        },
                    )
                    if search_resp.status_code == 200:
                        candidates = search_resp.json().get("Items", [])
                        matched_id = self._match_single_track(track, candidates, music_dir)
                        if matched_id:
                            track_to_id_map[idx] = matched_id
                            unresolved_indices.remove(idx)
                except Exception as search_err:
                    logger.debug("Targeted search failed for track '%s': %s", title, search_err)

        # Assemble final ordered ID list preserving source sequence
        for idx in range(len(tracks)):
            if idx in track_to_id_map:
                resolved_ids.append(track_to_id_map[idx])
            else:
                track = tracks[idx]
                logger.warning(
                    "Could not resolve Jellyfin Item ID for track [%d]: %s - %s",
                    idx, track.get("artist"), track.get("title"),
                )

        logger.info("Resolved %d / %d Jellyfin track Item IDs.", len(resolved_ids), len(tracks))
        return resolved_ids

    def _match_single_track(
        self,
        track: Dict[str, Any],
        jf_items: List[Dict[str, Any]],
        music_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Apply multi-tier matching logic to locate Jellyfin Item GUID."""
        track_path_str = str(track.get("path") or track.get("local_path") or "")
        track_title = str(track.get("title") or "")
        track_artist = str(track.get("artist") or "")

        norm_track_title = normalize_key(track_title)
        stripped_track_title = normalize_key(strip_parentheticals(track_title))

        # Determine relative subpath candidates if path is provided
        subpath_candidates: List[str] = []
        filename: Optional[str] = None
        if track_path_str:
            p = Path(track_path_str)
            filename = p.name
            # Exact path check
            for item in jf_items:
                jf_path = item.get("Path") or ""
                if jf_path and jf_path.replace("\\", "/") == track_path_str.replace("\\", "/"):
                    return item["Id"]

            if music_dir:
                try:
                    rel = p.relative_to(music_dir).as_posix()
                    subpath_candidates.append(rel)
                except ValueError:
                    pass
            parts = p.parts
            if len(parts) >= 2:
                subpath_candidates.append(posixpath.join(*parts[-2:]))
            if len(parts) >= 3:
                subpath_candidates.append(posixpath.join(*parts[-3:]))

        # Tier 1: Relative Subpath Suffix Match
        if subpath_candidates:
            for item in jf_items:
                jf_path = (item.get("Path") or "").replace("\\", "/")
                if not jf_path:
                    continue
                for sub in subpath_candidates:
                    if jf_path.endswith(sub):
                        return item["Id"]

        # Tier 2: Filename Match with Directory / Artist Verification
        if filename:
            for item in jf_items:
                jf_path = (item.get("Path") or "").replace("\\", "/")
                if posixpath.basename(jf_path) == filename:
                    cand_artist = item.get("AlbumArtist") or (item.get("Artists", [""])[0] if item.get("Artists") else "")
                    if not track_artist or artists_match(cand_artist, track_artist):
                        return item["Id"]

        # Tier 3: Unicode NFKC Normalized Title + Artist Match
        if norm_track_title:
            for item in jf_items:
                jf_title = normalize_key(item.get("Name", ""))
                if jf_title == norm_track_title:
                    cand_artists = item.get("Artists") or []
                    album_artist = item.get("AlbumArtist") or ""
                    all_cand_str = ", ".join(cand_artists) if cand_artists else album_artist
                    if not track_artist or artists_match(all_cand_str, track_artist):
                        return item["Id"]

        # Tier 4: Stripped Parentheticals Title Match
        if stripped_track_title and stripped_track_title != norm_track_title:
            for item in jf_items:
                jf_title_stripped = normalize_key(strip_parentheticals(item.get("Name", "")))
                if jf_title_stripped == stripped_track_title:
                    cand_artists = item.get("Artists") or []
                    album_artist = item.get("AlbumArtist") or ""
                    all_cand_str = ", ".join(cand_artists) if cand_artists else album_artist
                    if not track_artist or artists_match(all_cand_str, track_artist):
                        return item["Id"]

        return None
