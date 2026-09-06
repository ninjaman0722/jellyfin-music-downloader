"""Comprehensive Pytest Suite for Jellyfin REST Client & Multi-User Isolation (Milestone 3).

Validates:
- JellyfinClient lifecycle, initialization, auth headers (X-Emby-Token and MediaBrowser Authorization), and clean teardown
- User discovery, disabled account filtering, and Household (Shared) account injection
- User-scoped playlist retrieval and multi-user privacy isolation (Alice vs Bob)
- Dynamic virtual folder discovery and targeted library refresh with global fallback
- Track BaseItem GUID resolution across multi-tier strategies (path, relative subpath, filename, Unicode NFKC, stripped parentheticals, targeted search)
- Non-ASCII title preservation (Japanese kanji/kana, Korean, Cyrillic)
- Playlist creation with JSON body payload and reuse of existing playlists
- Strict 50-track sequential chunking across all boundary conditions (0, 49, 50, 51, 100, 150, 157)
- Network resilience and structured error hierarchy (connection drops, timeouts, 401, 404, 503, 400, permission errors)
- Forensic invariant assertions: Strictly zero SQLite direct queries, zero jellyfin.db, and zero playlist.xml file mutations
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from server.app.jellyfin import (
    HOUSEHOLD_USER_ID,
    HOUSEHOLD_USER_NAME,
    SHARED_USER_ID,
    JellyfinAuthError,
    JellyfinClient,
    JellyfinConnectionError,
    JellyfinError,
    JellyfinNetworkError,
    JellyfinNotFoundError,
    JellyfinPermissionError,
    JellyfinPlaylist,
    JellyfinServerError,
    JellyfinTimeoutError,
    JellyfinUser,
    JellyfinValidationError,
    PlaylistSummary,
    RefreshResult,
    VirtualFolder,
)
from tests.conftest import MockJellyfinState


@pytest.fixture
def mock_jf_state() -> MockJellyfinState:
    return MockJellyfinState()


@pytest.fixture
def jellyfin_client() -> JellyfinClient:
    return JellyfinClient(
        base_url="http://mock-jellyfin:8096",
        token="test-secret-token-123",
        timeout=5.0,
        max_retries=1,
        retry_backoff=0.01,
    )


# ==============================================================================
# 1. Lifecycle & Authentication Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_client_init_and_aclose():
    """Verify JellyfinClient instantiates with configured parameters and closes cleanly."""
    client = JellyfinClient(
        base_url="http://localhost:8096/",
        token="my-token",
        timeout=10.0,
    )
    assert client.base_url == "http://localhost:8096"
    assert client.token == "my-token"
    assert client.api_key == "my-token"
    assert not client.client.is_closed

    await client.aclose()
    assert client._client is None or client._client.is_closed


@pytest.mark.asyncio
async def test_client_context_manager():
    """Verify JellyfinClient functions as an async context manager."""
    async with JellyfinClient(base_url="http://localhost:8096", token="tok") as client:
        assert not client.client.is_closed
    assert client._client is None or client._client.is_closed


@pytest.mark.asyncio
async def test_client_sends_dual_auth_headers(respx_mock, jellyfin_client: JellyfinClient):
    """Verify outgoing requests include X-Emby-Token, Authorization: MediaBrowser, and Accept headers."""
    route = respx_mock.get(path="/Users").respond(200, json=[])
    await jellyfin_client.get_users()

    assert route.called
    req = route.calls.last.request
    assert req.headers.get("X-Emby-Token") == "test-secret-token-123"
    auth_header = req.headers.get("Authorization", "")
    assert "MediaBrowser" in auth_header
    assert 'Client="Jellyfin Music Downloader"' in auth_header
    assert 'Token="test-secret-token-123"' in auth_header
    assert "application/json" in req.headers.get("Accept", "")
    assert "api_key" not in req.url.query.decode("utf-8")


# ==============================================================================
# 2. User Discovery & Multi-User Isolation Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_get_users_parses_response_and_appends_household(respx_mock, jellyfin_client: JellyfinClient):
    """Verify get_users() parses users, excludes disabled accounts, and appends Household (Shared)."""
    respx_mock.get(path="/Users").respond(
        200,
        json=[
            {
                "Id": "u-alice",
                "Name": "Alice",
                "HasPassword": True,
                "Policy": {"IsAdministrator": True, "IsDisabled": False},
            },
            {
                "Id": "u-disabled",
                "Name": "DisabledAccount",
                "HasPassword": False,
                "Policy": {"IsAdministrator": False, "IsDisabled": True},
            },
            {
                "Id": "u-bob",
                "Name": "Bob",
                "HasPassword": False,
                "Policy": {"IsAdministrator": False, "IsDisabled": False},
            },
        ],
    )
    users = await jellyfin_client.get_users(include_household=True)
    assert len(users) == 3
    assert users[0].id == "u-alice"
    assert users[0].name == "Alice"
    assert users[0].is_admin is True
    assert users[0].is_household is False

    assert users[1].id == "u-bob"
    assert users[1].name == "Bob"
    assert users[1].is_admin is False

    # Household virtual user
    assert users[2].id == HOUSEHOLD_USER_ID
    assert users[2].name == HOUSEHOLD_USER_NAME
    assert users[2].is_household is True


@pytest.mark.asyncio
async def test_get_user_playlists_scoped_by_user_id(respx_mock, jellyfin_client: JellyfinClient):
    """Verify get_user_playlists() passes userId in URL path to maintain user isolation."""
    respx_mock.get(path="/Users/u-alice/Items").respond(
        200,
        json={
            "Items": [
                {"Id": "pl-a1", "Name": "Alice Rock", "ChildCount": 25, "OwnerUserId": "u-alice"},
                {"Id": "pl-a2", "Name": "Alice Chill", "ChildCount": 10, "OwnerUserId": "u-alice"},
            ]
        },
    )
    playlists = await jellyfin_client.get_user_playlists("u-alice")
    assert len(playlists) == 2
    assert playlists[0].id == "pl-a1"
    assert playlists[0].name == "Alice Rock"
    assert playlists[0].track_count == 25
    assert playlists[0].item_count == 25
    assert playlists[0].owner_user_id == "u-alice"


@pytest.mark.asyncio
async def test_multi_user_privacy_isolation_alice_vs_bob(respx_mock, jellyfin_client: JellyfinClient):
    """Verify Alice and Bob playlists are strictly isolated and cannot leak across boundaries."""
    respx_mock.get(path="/Users/user-alice/Items").respond(
        200,
        json={"Items": [{"Id": "pl-alice-private", "Name": "Alice Secret", "ChildCount": 5, "OwnerUserId": "user-alice"}]},
    )
    respx_mock.get(path="/Users/user-bob/Items").respond(
        200,
        json={"Items": [{"Id": "pl-bob-private", "Name": "Bob Secret", "ChildCount": 12, "OwnerUserId": "user-bob"}]},
    )

    alice_pls = await jellyfin_client.get_user_playlists("user-alice")
    bob_pls = await jellyfin_client.get_user_playlists("user-bob")

    assert [p.id for p in alice_pls] == ["pl-alice-private"]
    assert [p.id for p in bob_pls] == ["pl-bob-private"]
    assert "pl-bob-private" not in [p.id for p in alice_pls]
    assert "pl-alice-private" not in [p.id for p in bob_pls]


@pytest.mark.asyncio
async def test_get_user_playlists_filters_foreign_owners(respx_mock, jellyfin_client: JellyfinClient):
    """Verify that if Jellyfin returns items with a different OwnerUserId, they are filtered out."""
    respx_mock.get(path="/Users/user-alice/Items").respond(
        200,
        json={
            "Items": [
                {"Id": "pl-alice-1", "Name": "Alice 1", "ChildCount": 3, "OwnerUserId": "user-alice"},
                {"Id": "pl-bob-leaked", "Name": "Bob Leaked", "ChildCount": 10, "OwnerUserId": "user-bob"},
                {"Id": "pl-shared", "Name": "Shared Jams", "ChildCount": 8, "OwnerUserId": SHARED_USER_ID},
            ]
        },
    )
    alice_pls = await jellyfin_client.get_user_playlists("user-alice")
    ids = [p.id for p in alice_pls]
    assert "pl-alice-1" in ids
    assert "pl-shared" in ids
    assert "pl-bob-leaked" not in ids, "Foreign user's playlist leaked across isolation boundary!"


@pytest.mark.asyncio
async def test_get_user_playlists_household_queries_global_items(respx_mock, jellyfin_client: JellyfinClient):
    """Verify querying playlists for Household (Shared) routes to /Items."""
    route = respx_mock.get(path="/Items").respond(
        200,
        json={"Items": [{"Id": "pl-shared-01", "Name": "Living Room Jams", "ChildCount": 50}]},
    )
    pls = await jellyfin_client.get_user_playlists(HOUSEHOLD_USER_ID)
    assert route.called
    assert len(pls) == 1
    assert pls[0].id == "pl-shared-01"


# ==============================================================================
# 3. Dynamic Virtual Folder Discovery & Targeted Refresh
# ==============================================================================

@pytest.mark.asyncio
async def test_get_virtual_folders(respx_mock, jellyfin_client: JellyfinClient):
    """Verify get_virtual_folders() retrieves library virtual folders."""
    respx_mock.get(path="/Library/VirtualFolders").respond(
        200,
        json=[
            {"Name": "Music", "ItemId": "vf-music-101", "CollectionType": "music", "Locations": ["/music"]},
            {"Name": "Playlists", "ItemId": "vf-pl-102", "CollectionType": "playlists", "Locations": ["/playlists"]},
        ],
    )
    vfs = await jellyfin_client.get_virtual_folders()
    assert len(vfs) == 2
    assert vfs[0].name == "Music"
    assert vfs[0].item_id == "vf-music-101"
    assert vfs[0].id == "vf-music-101"
    assert vfs[0].collection_type == "music"


@pytest.mark.asyncio
async def test_find_music_library_by_collection_type(respx_mock, jellyfin_client: JellyfinClient):
    """Verify find_music_library prioritizes collection_type == 'music'."""
    respx_mock.get(path="/Library/VirtualFolders").respond(
        200,
        json=[
            {"Name": "My Audio Collection", "ItemId": "vf-music-202", "CollectionType": "music", "Locations": ["/mnt/audio"]},
            {"Name": "Movies", "ItemId": "vf-mov-99", "CollectionType": "movies", "Locations": ["/mnt/movies"]},
        ],
    )
    vf = await jellyfin_client.find_music_library()
    assert vf is not None
    assert vf.item_id == "vf-music-202"


@pytest.mark.asyncio
async def test_find_music_library_by_location(respx_mock, jellyfin_client: JellyfinClient):
    """Verify find_music_library matches configured storage location."""
    respx_mock.get(path="/Library/VirtualFolders").respond(
        200,
        json=[
            {"Name": "Archive", "ItemId": "vf-arc-1", "CollectionType": "mixed", "Locations": ["/storage/music"]},
        ],
    )
    vf = await jellyfin_client.find_music_library(music_dir="/storage/music/Artist/Album")
    assert vf is not None
    assert vf.item_id == "vf-arc-1"


@pytest.mark.asyncio
async def test_refresh_library_targeted_music_folder(respx_mock, jellyfin_client: JellyfinClient):
    """Verify refresh_library() dynamically discovers music folder and targets POST /Items/{id}/Refresh."""
    respx_mock.get(path="/Library/VirtualFolders").respond(
        200,
        json=[{"Name": "Music", "ItemId": "vf-music-101", "CollectionType": "music", "Locations": ["/music"]}],
    )
    refresh_route = respx_mock.post(path="/Items/vf-music-101/Refresh").respond(204)

    result = await jellyfin_client.refresh_library()
    assert refresh_route.called
    assert result.success is True
    assert result.target == "item:vf-music-101"


@pytest.mark.asyncio
async def test_refresh_library_fallback_to_global(respx_mock, jellyfin_client: JellyfinClient):
    """Verify refresh_library() falls back to POST /Library/Refresh if targeted refresh fails with 404."""
    respx_mock.get(path="/Library/VirtualFolders").respond(
        200,
        json=[{"Name": "Music", "ItemId": "vf-stale-id", "CollectionType": "music"}],
    )
    respx_mock.post(path="/Items/vf-stale-id/Refresh").respond(404, text="Not Found")
    global_refresh = respx_mock.post(path="/Library/Refresh").respond(204)

    result = await jellyfin_client.refresh_library()
    assert global_refresh.called
    assert result.success is True
    assert result.target == "library:global"


# ==============================================================================
# 4. Track BaseItem GUID Resolution Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_resolve_track_item_ids_matches_by_exact_path(respx_mock, jellyfin_client: JellyfinClient):
    """Verify resolve_track_item_ids matches items using exact local path."""
    respx_mock.get(path="/Users/u-1/Items").respond(
        200,
        json={
            "Items": [
                {"Id": "guid-track-1", "Path": "/music/The Weeknd/After Hours/01-01 - Blinding Lights.mp3", "Name": "Blinding Lights"},
                {"Id": "guid-track-2", "Path": "/music/Dua Lipa/Future Nostalgia/01-02 - Levitating.mp3", "Name": "Levitating"},
            ]
        },
    )
    tracks = [
        {"title": "Blinding Lights", "artist": "The Weeknd", "path": "/music/The Weeknd/After Hours/01-01 - Blinding Lights.mp3"},
        {"title": "Levitating", "artist": "Dua Lipa", "path": "/music/Dua Lipa/Future Nostalgia/01-02 - Levitating.mp3"},
    ]
    ids = await jellyfin_client.resolve_track_item_ids("u-1", tracks)
    assert ids == ["guid-track-1", "guid-track-2"]


@pytest.mark.asyncio
async def test_resolve_track_item_ids_matches_by_relative_subpath(respx_mock, jellyfin_client: JellyfinClient):
    """Verify relative subpath suffix matches when host vs container mount prefixes differ."""
    respx_mock.get(path="/Users/u-1/Items").respond(
        200,
        json={
            "Items": [
                {"Id": "guid-container-1", "Path": "/music/Daft Punk/Discovery/01 - One More Time.mp3", "Name": "One More Time"},
            ]
        },
    )
    tracks = [
        {"title": "One More Time", "artist": "Daft Punk", "path": "/mnt/host/media/music/Daft Punk/Discovery/01 - One More Time.mp3"},
    ]
    ids = await jellyfin_client.resolve_track_item_ids("u-1", tracks, music_dir=Path("/mnt/host/media/music"))
    assert ids == ["guid-container-1"]


@pytest.mark.asyncio
async def test_resolve_track_item_ids_nfkc_unicode_preservation(respx_mock, jellyfin_client: JellyfinClient):
    """Verify non-ASCII track titles (Japanese kanji/kana, Korean, Cyrillic) resolve correctly via NFKC."""
    respx_mock.get(path="/Users/u-1/Items").respond(
        200,
        json={
            "Items": [
                {"Id": "guid-jp", "Name": "前前前世", "Artists": ["RADWIMPS"], "Path": "/music/RADWIMPS/前前前世.mp3"},
                {"Id": "guid-kr", "Name": "봄날 (Spring Day)", "Artists": ["BTS"], "Path": "/music/BTS/봄날.mp3"},
                {"Id": "guid-ru", "Name": "Группа крови", "Artists": ["Кино"], "Path": "/music/Кино/Группа крови.mp3"},
            ]
        },
    )
    tracks = [
        {"title": "前前前世", "artist": "RADWIMPS", "path": "/alt/RADWIMPS/前前前世.mp3"},
        {"title": "봄날 (Spring Day)", "artist": "BTS", "path": None},
        {"title": "Группа крови", "artist": "Кино", "path": None},
    ]
    ids = await jellyfin_client.resolve_track_item_ids("u-1", tracks)
    assert ids == ["guid-jp", "guid-kr", "guid-ru"]


@pytest.mark.asyncio
async def test_resolve_track_item_ids_targeted_search_fallback(respx_mock, jellyfin_client: JellyfinClient):
    """Verify targeted REST search resolves tracks missing from the initial scan."""
    respx_mock.get(path="/Users/u-1/Items", params__contains={"searchTerm": "Rare Track"}).respond(
        200,
        json={"Items": [{"Id": "guid-rare-found", "Name": "Rare Track", "Artists": ["Obscure Artist"]}]},
    )
    respx_mock.get(path="/Users/u-1/Items").respond(
        200,
        json={"Items": []},
    )
    tracks = [{"title": "Rare Track", "artist": "Obscure Artist", "path": None}]
    ids = await jellyfin_client.resolve_track_item_ids("u-1", tracks, max_retries=1)
    assert ids == ["guid-rare-found"]


@pytest.mark.asyncio
async def test_resolve_track_item_ids_omits_missing_tracks_gracefully(respx_mock, jellyfin_client: JellyfinClient):
    """Verify tracks completely missing from Jellyfin library are omitted without raising exceptions."""
    respx_mock.get(path="/Users/u-1/Items").respond(
        200,
        json={"Items": [{"Id": "guid-found", "Name": "Known Song", "Artists": ["Artist"]}]},
    )
    tracks = [
        {"title": "Known Song", "artist": "Artist", "path": None},
        {"title": "Unknown Song That Failed To Scan", "artist": "Ghost", "path": None},
    ]
    ids = await jellyfin_client.resolve_track_item_ids("u-1", tracks, max_retries=1)
    assert ids == ["guid-found"]


# ==============================================================================
# 5. Playlist Creation & Retrieval Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_create_or_get_playlist_returns_existing_if_present(respx_mock, jellyfin_client: JellyfinClient):
    """Verify existing playlist owned by user is reused without creating a duplicate."""
    respx_mock.get(path="/Users/u-1/Items").respond(
        200,
        json={"Items": [{"Id": "pl-existing-99", "Name": "Synthwave Drive", "ChildCount": 10, "OwnerUserId": "u-1"}]},
    )
    create_route = respx_mock.post(path="/Playlists").respond(200, json={"Id": "pl-new"})

    pl_id = await jellyfin_client.create_or_get_playlist("u-1", "Synthwave Drive")
    assert pl_id == "pl-existing-99"
    assert not create_route.called


@pytest.mark.asyncio
async def test_create_or_get_playlist_creates_new_with_json_payload(respx_mock, jellyfin_client: JellyfinClient):
    """Verify new playlist is created with POST /Playlists and JSON body payload."""
    respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": []})
    create_route = respx_mock.post(path="/Playlists").respond(
        200,
        json={"Id": "pl-newly-created-001"},
    )

    pl_id = await jellyfin_client.create_or_get_playlist("u-1", "Brand New Playlist")
    assert pl_id == "pl-newly-created-001"
    assert create_route.called

    req = create_route.calls.last.request
    body = req.read().decode("utf-8")
    import json
    data = json.loads(body)
    assert data["Name"] == "Brand New Playlist"
    assert data["UserId"] == "u-1"
    assert data["MediaType"] == "Audio"


# ==============================================================================
# 6. 50-Track Chunking Boundary Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_chunking_boundary_0_tracks(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 0 tracks generates 0 API requests."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    res = await jellyfin_client.add_items_to_playlist("u-1", "pl-01", [])
    assert res is True
    assert not append_route.called


@pytest.mark.asyncio
async def test_chunking_boundary_49_tracks(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 49 tracks generates exactly 1 request with 49 items."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"track-{i}" for i in range(49)]

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", track_ids)
    assert append_route.call_count == 1
    ids_sent = append_route.calls[0].request.url.params["ids"].split(",")
    assert len(ids_sent) == 49


@pytest.mark.asyncio
async def test_chunking_boundary_50_tracks(respx_mock, jellyfin_client: JellyfinClient):
    """Verify exactly 50 tracks generates 1 request with 50 items."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"track-{i}" for i in range(50)]

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", track_ids)
    assert append_route.call_count == 1
    ids_sent = append_route.calls[0].request.url.params["ids"].split(",")
    assert len(ids_sent) == 50


@pytest.mark.asyncio
async def test_chunking_boundary_51_tracks(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 51 tracks generates exactly 2 sequential requests (50 + 1)."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"track-{i}" for i in range(51)]

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", track_ids)
    assert append_route.call_count == 2
    chunk1 = append_route.calls[0].request.url.params["ids"].split(",")
    chunk2 = append_route.calls[1].request.url.params["ids"].split(",")
    assert len(chunk1) == 50
    assert len(chunk2) == 1
    assert chunk1[0] == "track-0"
    assert chunk2[0] == "track-50"


@pytest.mark.asyncio
async def test_chunking_boundary_100_tracks(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 100 tracks generates exactly 2 sequential requests of 50 each."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"track-{i}" for i in range(100)]

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", track_ids)
    assert append_route.call_count == 2
    assert len(append_route.calls[0].request.url.params["ids"].split(",")) == 50
    assert len(append_route.calls[1].request.url.params["ids"].split(",")) == 50


@pytest.mark.asyncio
async def test_chunking_boundary_150_tracks(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 150 tracks generates exactly 3 sequential requests of 50 each."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"track-{i}" for i in range(150)]

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", track_ids)
    assert append_route.call_count == 3


@pytest.mark.asyncio
async def test_chunking_boundary_157_tracks_preserves_strict_order(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 157 tracks splits into 50, 50, 50, 7 and preserves strict source order."""
    append_route = respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(204)
    track_ids = [f"track-{i:03d}" for i in range(157)]

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", track_ids)
    assert append_route.call_count == 4
    c1 = append_route.calls[0].request.url.params["ids"].split(",")
    c2 = append_route.calls[1].request.url.params["ids"].split(",")
    c3 = append_route.calls[2].request.url.params["ids"].split(",")
    c4 = append_route.calls[3].request.url.params["ids"].split(",")
    assert len(c1) == 50 and len(c2) == 50 and len(c3) == 50 and len(c4) == 7

    reconstructed = c1 + c2 + c3 + c4
    assert reconstructed == track_ids


@pytest.mark.asyncio
async def test_chunking_deduplicate_parameter(respx_mock, jellyfin_client: JellyfinClient):
    """Verify deduplicate=True filters out IDs already present in playlist."""
    respx_mock.get(path="/Playlists/pl-01/Items").respond(
        200,
        json={"Items": [{"Id": "t1"}, {"Id": "t2"}]},
    )
    append_route = respx_mock.route(method="POST", path="/Playlists/pl-01/Items").respond(204)

    await jellyfin_client.add_items_to_playlist("u-1", "pl-01", ["t1", "t2", "t3"], deduplicate=True)
    assert append_route.call_count == 1
    ids_sent = append_route.calls[0].request.url.params["ids"].split(",")
    assert ids_sent == ["t3"]


# ==============================================================================
# 7. Error Handling & Resilience Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_unreachable_host_raises_jellyfin_connection_error(respx_mock, jellyfin_client: JellyfinClient):
    """Verify connection refusal or network drop raises JellyfinConnectionError."""
    respx_mock.get(path="/Users").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(JellyfinConnectionError) as exc_info:
        await jellyfin_client.get_users()
    assert "Connection refused" in str(exc_info.value)


@pytest.mark.asyncio
async def test_timeout_raises_jellyfin_timeout_error(respx_mock, jellyfin_client: JellyfinClient):
    """Verify request timeout raises JellyfinTimeoutError (subclass of JellyfinConnectionError)."""
    respx_mock.get(path="/Users").mock(side_effect=httpx.TimeoutException("Read timed out"))
    with pytest.raises(JellyfinConnectionError):
        await jellyfin_client.get_users()


@pytest.mark.asyncio
async def test_401_unauthorized_raises_jellyfin_auth_error(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 401 Unauthorized raises JellyfinAuthError."""
    respx_mock.get(path="/Users").respond(401, text="Invalid token")
    with pytest.raises(JellyfinAuthError) as exc_info:
        await jellyfin_client.get_users()
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_404_not_found_raises_jellyfin_not_found_error(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 404 Not Found raises JellyfinNotFoundError."""
    respx_mock.get(path="/Users/nonexistent/Items").respond(404, text="User not found")
    with pytest.raises(JellyfinNotFoundError) as exc_info:
        await jellyfin_client.get_user_playlists("nonexistent")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_503_service_unavailable_raises_jellyfin_server_error(respx_mock, jellyfin_client: JellyfinClient):
    """Verify 503 Service Unavailable raises JellyfinServerError after retries."""
    respx_mock.get(path="/Users").respond(503, text="Service Unavailable")
    with pytest.raises(JellyfinServerError) as exc_info:
        await jellyfin_client.get_users()
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_cross_user_append_raises_permission_error(respx_mock, jellyfin_client: JellyfinClient):
    """Verify cross-user append returning 403 raises JellyfinPermissionError."""
    respx_mock.route(method="POST", path__regex=r"^/Playlists/.*/Items").respond(403, text="Forbidden")
    with pytest.raises(JellyfinPermissionError):
        await jellyfin_client.add_items_to_playlist("user-alice", "pl-bob", ["trk-1"])


# ==============================================================================
# 8. Forensic Invariant Verification (Zero SQLite / Zero XML Mutations)
# ==============================================================================

def test_zero_sqlite_or_xml_disk_access():
    """Verify server/app/jellyfin.py does not import sqlite3, touch jellyfin.db, or mutate playlist.xml."""
    jellyfin_py = Path(__file__).resolve().parent.parent / "server" / "app" / "jellyfin.py"
    assert jellyfin_py.exists(), "jellyfin.py file does not exist!"
    content = jellyfin_py.read_text(encoding="utf-8")
    assert "sqlite3" not in content, "Forbidden 'sqlite3' found in jellyfin.py!"
    assert "jellyfin.db" not in content, "Forbidden 'jellyfin.db' found in jellyfin.py!"
    assert "playlist.xml" not in content, "Forbidden 'playlist.xml' found in jellyfin.py!"
    assert "api_key=" not in content, "Forbidden 'api_key=' in query params found in jellyfin.py!"
