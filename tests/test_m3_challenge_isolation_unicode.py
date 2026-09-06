"""Adversarial Challenge Test Suite: Multi-User Privacy Isolation & Unicode Resolution.

Authored by M3 Challenger 2 (challenger_m3_2).

Validates:
1. Multi-User Privacy Isolation Boundaries:
   - Alice and Bob with identical playlist names ("Favorites", "Workout").
   - Zero cross-user access, appending, or mutation.
   - Cross-user append rejection raising JellyfinPermissionError.
   - Contaminated API response filtering against foreign OwnerUserIds.
   - Independent playlist creation when playlist name collisions exist across users.
2. Non-ASCII Unicode Track BaseItem ID Resolution:
   - Japanese (前前前世, 夜に駆ける), Korean (봄날), Cyrillic (Группа крови, Спокойная ночь), and accented Latin (Déjà Vu, Águas de Março).
   - Relative subpath matching across volume mount discrepancies (host /mnt/media/music vs container /music).
   - Subpath matching fallback without music_dir (parts[-2:], parts[-3:]).
   - Unicode NFKC normalization (fullwidth/halfwidth, decomposed NFD vs precomposed NFC).
   - Asynchronous scanner retry polling delay handling delayed library indexing.
   - Strict source playlist sequence order preservation across multilingual tracks and missing track omissions.
"""

from __future__ import annotations

import asyncio
import unicodedata
from pathlib import Path
from typing import Any, Dict, List
import pytest
import respx
import httpx

from server.app.jellyfin import (
    HOUSEHOLD_USER_ID,
    SHARED_USER_ID,
    JellyfinAuthError,
    JellyfinClient,
    JellyfinPermissionError,
    JellyfinValidationError,
    PlaylistSummary,
)


@pytest.fixture
def jf_client() -> JellyfinClient:
    return JellyfinClient(
        base_url="http://mock-jellyfin:8096",
        token="challenger-token-abc",
        timeout=5.0,
        max_retries=2,
        retry_backoff=0.01,
    )


# ==============================================================================
# SECTION 1: Multi-User Privacy Isolation Stress Tests
# ==============================================================================

class TestMultiUserPrivacyIsolation:
    """Stress tests verifying strict user boundary enforcement on playlists."""

    @pytest.mark.asyncio
    async def test_identical_playlist_names_alice_and_bob_isolated_retrieval(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify Alice and Bob with identical playlist names ('Favorites', 'Workout')
        receive only their own playlists with zero cross-user leakage."""
        alice_items = [
            {"Id": "pl-alice-fav-100", "Name": "Favorites", "ChildCount": 15, "OwnerUserId": "user-alice"},
            {"Id": "pl-alice-work-101", "Name": "Workout", "ChildCount": 42, "OwnerUserId": "user-alice"},
        ]
        bob_items = [
            {"Id": "pl-bob-fav-200", "Name": "Favorites", "ChildCount": 8, "OwnerUserId": "user-bob"},
            {"Id": "pl-bob-work-201", "Name": "Workout", "ChildCount": 30, "OwnerUserId": "user-bob"},
        ]

        respx_mock.get(path="/Users/user-alice/Items").respond(200, json={"Items": alice_items})
        respx_mock.get(path="/Users/user-bob/Items").respond(200, json={"Items": bob_items})

        alice_playlists = await jf_client.get_user_playlists("user-alice")
        bob_playlists = await jf_client.get_user_playlists("user-bob")

        # Verify Alice's playlists
        alice_ids = [p.id for p in alice_playlists]
        assert alice_ids == ["pl-alice-fav-100", "pl-alice-work-101"]
        assert all(p.owner_user_id == "user-alice" for p in alice_playlists)
        assert "pl-bob-fav-200" not in alice_ids
        assert "pl-bob-work-201" not in alice_ids

        # Verify Bob's playlists
        bob_ids = [p.id for p in bob_playlists]
        assert bob_ids == ["pl-bob-fav-200", "pl-bob-work-201"]
        assert all(p.owner_user_id == "user-bob" for p in bob_playlists)
        assert "pl-alice-fav-100" not in bob_ids
        assert "pl-alice-work-101" not in bob_ids

    @pytest.mark.asyncio
    async def test_create_or_get_playlist_reuses_user_scoped_playlist_only(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify create_or_get_playlist matches existing playlist for the requesting user
        and never intercepts or returns the playlist of another user with identical name."""
        alice_items = [
            {"Id": "pl-alice-fav-100", "Name": "Favorites", "ChildCount": 15, "OwnerUserId": "user-alice"},
            {"Id": "pl-alice-work-101", "Name": "Workout", "ChildCount": 42, "OwnerUserId": "user-alice"},
        ]
        bob_items = [
            {"Id": "pl-bob-fav-200", "Name": "Favorites", "ChildCount": 8, "OwnerUserId": "user-bob"},
            {"Id": "pl-bob-work-201", "Name": "Workout", "ChildCount": 30, "OwnerUserId": "user-bob"},
        ]

        respx_mock.get(path="/Users/user-alice/Items").respond(200, json={"Items": alice_items})
        respx_mock.get(path="/Users/user-bob/Items").respond(200, json={"Items": bob_items})
        create_route = respx_mock.post(path="/Playlists").respond(200, json={"Id": "pl-unexpected-new"})

        # Alice requests "Favorites" -> must return Alice's ID without creating new
        alice_fav_id = await jf_client.create_or_get_playlist("user-alice", "Favorites")
        assert alice_fav_id == "pl-alice-fav-100"
        assert alice_fav_id != "pl-bob-fav-200"

        # Bob requests "Favorites" -> must return Bob's ID without creating new
        bob_fav_id = await jf_client.create_or_get_playlist("user-bob", "Favorites")
        assert bob_fav_id == "pl-bob-fav-200"
        assert bob_fav_id != "pl-alice-fav-100"

        # Alice requests "Workout"
        alice_work_id = await jf_client.create_or_get_playlist("user-alice", "Workout")
        assert alice_work_id == "pl-alice-work-101"

        # Bob requests "Workout"
        bob_work_id = await jf_client.create_or_get_playlist("user-bob", "Workout")
        assert bob_work_id == "pl-bob-work-201"

        # Verify no POST /Playlists was called because both already owned the playlists
        assert not create_route.called

    @pytest.mark.asyncio
    async def test_create_or_get_playlist_creates_independent_when_other_user_owns_name(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify that when Bob already owns 'Chill Jams', but Alice does not,
        requesting 'Chill Jams' for Alice creates a fresh playlist for Alice
        rather than returning Bob's existing playlist."""
        # Alice has no playlists
        respx_mock.get(path="/Users/user-alice/Items").respond(200, json={"Items": []})
        # Bob already owns "Chill Jams"
        respx_mock.get(path="/Users/user-bob/Items").respond(
            200,
            json={"Items": [{"Id": "pl-bob-chill-555", "Name": "Chill Jams", "OwnerUserId": "user-bob"}]},
        )
        create_route = respx_mock.post(path="/Playlists").respond(
            200, json={"Id": "pl-alice-chill-999"}
        )

        alice_chill_id = await jf_client.create_or_get_playlist("user-alice", "Chill Jams")
        assert alice_chill_id == "pl-alice-chill-999"
        assert alice_chill_id != "pl-bob-chill-555"

        # Verify create request was scoped to user-alice
        assert create_route.called
        import json
        req_payload = create_route.calls.last.request.read().decode("utf-8")
        data = json.loads(req_payload)
        assert data["UserId"] == "user-alice"
        assert data["Name"] == "Chill Jams" 

    @pytest.mark.asyncio
    async def test_cross_user_append_rejected_with_permission_error_on_403(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify attempting to append items to Bob's playlist using Alice's user context
        raises JellyfinPermissionError when server denies with HTTP 403."""
        append_route = respx_mock.post(path="/Playlists/pl-bob-private/Items").respond(
            403, text="User does not have permission to modify this playlist."
        )

        with pytest.raises(JellyfinPermissionError) as exc_info:
            await jf_client.add_items_to_playlist(
                user_id="user-alice",
                playlist_id="pl-bob-private",
                item_ids=["track-001", "track-002"],
            )

        assert "Cross-user append forbidden" in str(exc_info.value)
        assert append_route.called
        assert append_route.calls.last.request.url.params["userId"] == "user-alice"

    @pytest.mark.asyncio
    async def test_cross_user_append_rejected_with_permission_error_on_401(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify cross-user append returning HTTP 401 raises JellyfinPermissionError."""
        respx_mock.post(path="/Playlists/pl-bob-private/Items").respond(401, text="Unauthorized token")

        with pytest.raises(JellyfinPermissionError):
            await jf_client.add_items_to_playlist(
                user_id="user-alice",
                playlist_id="pl-bob-private",
                item_ids=["track-001"],
            )

    @pytest.mark.asyncio
    async def test_cross_user_append_flexible_signature_rejection(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify alternative signature (playlist_id, item_ids, user_id) also raises JellyfinPermissionError."""
        respx_mock.post(path="/Playlists/pl-bob-private/Items").respond(403, text="Forbidden")

        with pytest.raises(JellyfinPermissionError):
            # Calling as (playlist_id, item_ids, user_id)
            await jf_client.add_items_to_playlist(
                "pl-bob-private",  # arg 1: playlist_id
                ["track-001"],     # arg 2: item_ids
                "user-alice",      # arg 3: user_id
            )

    @pytest.mark.asyncio
    async def test_contaminated_api_response_filters_foreign_owner_strictly(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Stress test: If Jellyfin server has a bug or returns playlists belonging
        to other users under /Users/{userId}/Items, client MUST strictly filter out
        any playlist where OwnerUserId != user_id and OwnerUserId != SHARED_USER_ID."""
        contaminated_items = [
            {"Id": "pl-alice-legit", "Name": "Alice Rock", "ChildCount": 10, "OwnerUserId": "user-alice"},
            {"Id": "pl-bob-leak", "Name": "Bob Secret", "ChildCount": 20, "OwnerUserId": "user-bob"},
            {"Id": "pl-charlie-leak", "Name": "Charlie Jazz", "ChildCount": 5, "OwnerUserId": "user-charlie"},
            {"Id": "pl-shared-legit", "Name": "Household Hits", "ChildCount": 50, "OwnerUserId": SHARED_USER_ID},
            {"Id": "pl-no-owner", "Name": "Default Owner", "ChildCount": 2, "OwnerUserId": None},
        ]
        respx_mock.get(path="/Users/user-alice/Items").respond(200, json={"Items": contaminated_items})

        playlists = await jf_client.get_user_playlists("user-alice")
        ids = [p.id for p in playlists]

        assert "pl-alice-legit" in ids
        assert "pl-shared-legit" in ids
        assert "pl-no-owner" in ids
        assert "pl-bob-leak" not in ids, "CRITICAL: Foreign playlist for user-bob was not filtered!"
        assert "pl-charlie-leak" not in ids, "CRITICAL: Foreign playlist for user-charlie was not filtered!"


# ==============================================================================
# SECTION 2: Non-ASCII Track BaseItem ID Resolution Stress Tests
# ==============================================================================

class TestNonAsciiTrackResolution:
    """Stress tests verifying BaseItem ID resolution for Japanese, Korean, Cyrillic,
    and Accented Latin tracks across volume mount discrepancies and scanner delays."""

    @pytest.mark.asyncio
    async def test_resolve_japanese_tracks_kanji_and_kana(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify Japanese tracks ('前前前世' by RADWIMPS and '夜に駆ける' by YOASOBI)
        resolve correctly via resolve_track_item_ids()."""
        jf_items = [
            {
                "Id": "guid-radwimps-zenzen",
                "Name": "前前前世",
                "Artists": ["RADWIMPS"],
                "Path": "/music/RADWIMPS/Your Name/01 - 前前前世.mp3",
            },
            {
                "Id": "guid-yoasobi-yoru",
                "Name": "夜に駆ける",
                "Artists": ["YOASOBI"],
                "Path": "/music/YOASOBI/The Book/01 - 夜に駆ける.flac",
            },
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        tracks = [
            {"title": "前前前世", "artist": "RADWIMPS", "path": "/mnt/media/music/RADWIMPS/Your Name/01 - 前前前世.mp3"},
            {"title": "夜に駆ける", "artist": "YOASOBI", "path": "/mnt/media/music/YOASOBI/The Book/01 - 夜に駆ける.flac"},
        ]

        resolved_ids = await jf_client.resolve_track_item_ids(
            "u-1", tracks, music_dir=Path("/mnt/media/music")
        )
        assert resolved_ids == ["guid-radwimps-zenzen", "guid-yoasobi-yoru"]

    @pytest.mark.asyncio
    async def test_resolve_korean_hangul_track(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify Korean Hangul track ('봄날' by BTS) resolves correctly."""
        jf_items = [
            {
                "Id": "guid-bts-spring",
                "Name": "봄날",
                "Artists": ["BTS"],
                "Path": "/music/BTS/You Never Walk Alone/02 - 봄날.mp3",
            },
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        tracks = [
            {"title": "봄날", "artist": "BTS", "path": "/mnt/media/music/BTS/You Never Walk Alone/02 - 봄날.mp3"},
        ]

        resolved_ids = await jf_client.resolve_track_item_ids(
            "u-1", tracks, music_dir=Path("/mnt/media/music")
        )
        assert resolved_ids == ["guid-bts-spring"]

    @pytest.mark.asyncio
    async def test_resolve_cyrillic_tracks(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify Cyrillic tracks ('Группа крови' and 'Спокойная ночь' by Кино) resolve correctly."""
        jf_items = [
            {
                "Id": "guid-kino-blood",
                "Name": "Группа крови",
                "Artists": ["Кино"],
                "Path": "/music/Кино/Группа крови/01 - Группа крови.mp3",
            },
            {
                "Id": "guid-kino-night",
                "Name": "Спокойная ночь",
                "Artists": ["Кино"],
                "Path": "/music/Кино/Группа крови/04 - Спокойная ночь.mp3",
            },
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        tracks = [
            {"title": "Группа крови", "artist": "Кино", "path": "/media/Кино/Группа крови/01 - Группа крови.mp3"},
            {"title": "Спокойная ночь", "artist": "Кино", "path": "/media/Кино/Группа крови/04 - Спокойная ночь.mp3"},
        ]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", tracks)
        assert resolved_ids == ["guid-kino-blood", "guid-kino-night"]

    @pytest.mark.asyncio
    async def test_resolve_accented_latin_tracks(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify accented Latin tracks ('Déjà Vu' by Beyoncé and 'Águas de Março' by Jobim) resolve correctly."""
        jf_items = [
            {
                "Id": "guid-beyonce-deja",
                "Name": "Déjà Vu",
                "Artists": ["Beyoncé"],
                "Path": "/music/Beyoncé/B'Day/01 - Déjà Vu.mp3",
            },
            {
                "Id": "guid-jobim-aguas",
                "Name": "Águas de Março",
                "Artists": ["Antônio Carlos Jobim, Elis Regina"],
                "Path": "/music/Jobim/Elis & Tom/01 - Águas de Março.mp3",
            },
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        tracks = [
            {"title": "Déjà Vu", "artist": "Beyoncé", "path": "/storage/Beyoncé/B'Day/01 - Déjà Vu.mp3"},
            {"title": "Águas de Março", "artist": "Antônio Carlos Jobim", "path": None},
        ]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", tracks)
        assert resolved_ids == ["guid-beyonce-deja", "guid-jobim-aguas"]

    @pytest.mark.asyncio
    async def test_relative_subpath_matching_across_mount_discrepancies(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify relative subpath matching succeeds across volume mount discrepancies
        (host /mnt/media/music vs container /music) with different prefix depths."""
        jf_items = [
            {
                "Id": "guid-subpath-mount-1",
                "Name": "前前前世",
                "Artists": ["RADWIMPS"],
                "Path": "/music/RADWIMPS/Your Name/01-01 - 前前前世.mp3",
            },
            {
                "Id": "guid-subpath-mount-2",
                "Name": "夜に駆ける",
                "Artists": ["YOASOBI"],
                "Path": "/var/lib/jellyfin/media/YOASOBI/The Book/01-01 - 夜に駆ける.flac",
            },
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        # Discrepancy 1: host uses /mnt/media/music (depth 3) vs container /music (depth 1)
        # Discrepancy 2: host uses /home/kendon/audio/music vs container /var/lib/jellyfin/media
        tracks = [
            {
                "title": "前前前世",
                "artist": "RADWIMPS",
                "path": "/mnt/media/music/RADWIMPS/Your Name/01-01 - 前前前世.mp3",
            },
            {
                "title": "夜に駆ける",
                "artist": "YOASOBI",
                "path": "/home/kendon/audio/music/YOASOBI/The Book/01-01 - 夜に駆ける.flac",
            },
        ]

        resolved_ids = await jf_client.resolve_track_item_ids(
            "u-1", tracks, music_dir=Path("/mnt/media/music")
        )
        assert resolved_ids == ["guid-subpath-mount-1", "guid-subpath-mount-2"]

    @pytest.mark.asyncio
    async def test_relative_subpath_matching_without_music_dir(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify relative subpath matching succeeds even when music_dir is None,
        relying on parts[-2:] (Album/File) and parts[-3:] (Artist/Album/File)."""
        jf_items = [
            {
                "Id": "guid-no-musicdir-match",
                "Name": "Группа крови",
                "Artists": ["Кино"],
                "Path": "/container/library/Кино/Группа крови/01 - Группа крови.mp3",
            }
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        tracks = [
            {
                "title": "Группа крови",
                "artist": "Кино",
                "path": "/completely/different/host/mount/Кино/Группа крови/01 - Группа крови.mp3",
            }
        ]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", tracks, music_dir=None)
        assert resolved_ids == ["guid-no-musicdir-match"]

    @pytest.mark.asyncio
    async def test_relative_subpath_windows_backslash_normalization(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify Windows backslash paths from SMB mounts or Windows Jellyfin servers
        are normalized to POSIX forward slashes and match successfully."""
        jf_items = [
            {
                "Id": "guid-smb-win-path",
                "Name": "봄날",
                "Artists": ["BTS"],
                "Path": r"\\SERVER\Music\BTS\You Never Walk Alone\02 - 봄날.mp3",
            }
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        tracks = [
            {
                "title": "봄날",
                "artist": "BTS",
                "path": "/mnt/media/music/BTS/You Never Walk Alone/02 - 봄날.mp3",
            }
        ]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", tracks)
        assert resolved_ids == ["guid-smb-win-path"]

    @pytest.mark.asyncio
    async def test_unicode_nfkc_decomposed_vs_precomposed_normalization(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify decomposed Unicode (NFD) in input resolves against precomposed (NFC) in Jellyfin."""
        # Precomposed NFC in Jellyfin
        nfc_accented = "Déjà Vu"
        assert unicodedata.is_normalized("NFC", nfc_accented)

        jf_items = [
            {
                "Id": "guid-nfc-deja",
                "Name": nfc_accented,
                "Artists": ["Beyoncé"],
                "Path": None,
            }
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        # Decomposed NFD in query track
        nfd_accented = unicodedata.normalize("NFD", "Déjà Vu")
        assert not unicodedata.is_normalized("NFC", nfd_accented)

        tracks = [{"title": nfd_accented, "artist": "Beyoncé", "path": None}]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", tracks)
        assert resolved_ids == ["guid-nfc-deja"]

    @pytest.mark.asyncio
    async def test_unicode_nfkc_fullwidth_character_normalization(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify fullwidth Latin characters (ＹＯＡＳＯＢＩ) normalize to standard ASCII via NFKC."""
        jf_items = [
            {
                "Id": "guid-yoasobi-fullwidth",
                "Name": "夜に駆ける",
                "Artists": ["YOASOBI"],
                "Path": None,
            }
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        # Query has fullwidth characters in artist: ＹＯＡＳＯＢＩ
        fullwidth_artist = "ＹＯＡＳＯＢＩ"
        tracks = [{"title": "夜に駆ける", "artist": fullwidth_artist, "path": None}]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", tracks)
        assert resolved_ids == ["guid-yoasobi-fullwidth"]

    @pytest.mark.asyncio
    async def test_scanner_retry_polling_asynchronous_delay(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify scanner retry polling handles delayed item indexing when
        library refresh is asynchronous: attempt 1 returns 0 items, attempt 2 returns item."""
        target_item = {
            "Id": "guid-async-scanned-track",
            "Name": "夜に駆ける",
            "Artists": ["YOASOBI"],
            "Path": "/music/YOASOBI/The Book/01 - 夜に駆ける.flac",
        }

        # Attempt 1 returns empty list (scanner not finished)
        # Attempt 2 returns populated list (scanner finished)
        route = respx_mock.get(path="/Users/u-1/Items")
        route.side_effect = [
            httpx.Response(200, json={"Items": []}),
            httpx.Response(200, json={"Items": [target_item]}),
        ]

        tracks = [
            {"title": "夜に駆ける", "artist": "YOASOBI", "path": "/mnt/media/music/YOASOBI/The Book/01 - 夜に駆ける.flac"}
        ]

        resolved_ids = await jf_client.resolve_track_item_ids(
            "u-1", tracks, max_retries=3, retry_delay=0.01
        )

        assert resolved_ids == ["guid-async-scanned-track"]
        assert route.call_count == 2

    @pytest.mark.asyncio
    async def test_strict_playlist_sequence_order_preservation_multilingual(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify resolve_track_item_ids preserves strict input sequence order
        across a mixed multilingual playlist while gracefully omitting missing tracks."""
        jf_items = [
            {"Id": "guid-0-jp1", "Name": "前前前世", "Artists": ["RADWIMPS"]},
            {"Id": "guid-1-cy1", "Name": "Группа крови", "Artists": ["Кино"]},
            {"Id": "guid-3-kr1", "Name": "봄날", "Artists": ["BTS"]},
            {"Id": "guid-4-al1", "Name": "Déjà Vu", "Artists": ["Beyoncé"]},
            {"Id": "guid-6-jp2", "Name": "夜に駆ける", "Artists": ["YOASOBI"]},
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})
        # Empty fallback for missing items
        respx_mock.get(path="/Users/u-1/Items", params__contains={"searchTerm": "Missing"}).respond(
            200, json={"Items": []}
        )

        input_tracks = [
            {"title": "前前前世", "artist": "RADWIMPS", "path": None},
            {"title": "Группа крови", "artist": "Кино", "path": None},
            {"title": "Missing Track 1", "artist": "Phantom", "path": None},
            {"title": "봄날", "artist": "BTS", "path": None},
            {"title": "Déjà Vu", "artist": "Beyoncé", "path": None},
            {"title": "Missing Track 2", "artist": "Ghost", "path": None},
            {"title": "夜に駆ける", "artist": "YOASOBI", "path": None},
        ]

        resolved_ids = await jf_client.resolve_track_item_ids("u-1", input_tracks, max_retries=1)

        # Output must contain exactly the 5 existing tracks in their original relative sequence
        expected = ["guid-0-jp1", "guid-1-cy1", "guid-3-kr1", "guid-4-al1", "guid-6-jp2"]
        assert resolved_ids == expected
        assert len(resolved_ids) == 5

    @pytest.mark.asyncio
    async def test_parenthetical_with_path_rescues_asymmetric_titles(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify that when Jellyfin library metadata contains parentheticals
        (e.g. '봄날 (Spring Day)') but query track title is stripped ('봄날'),
        having the local path (/music/BTS/You Never Walk Alone/02 - 봄날.mp3)
        enables Tier 1 / Tier 2 to successfully resolve the track."""
        jf_items = [
            {
                "Id": "guid-bts-spring-rescued",
                "Name": "봄날 (Spring Day)",
                "Artists": ["BTS"],
                "Path": "/music/BTS/You Never Walk Alone/02 - 봄날.mp3",
            }
        ]
        respx_mock.get(path="/Users/u-1/Items").respond(200, json={"Items": jf_items})

        # Query has title without parenthetical, but path is provided
        tracks = [
            {
                "title": "봄날",
                "artist": "BTS",
                "path": "/mnt/media/music/BTS/You Never Walk Alone/02 - 봄날.mp3",
            }
        ]

        resolved_ids = await jf_client.resolve_track_item_ids(
            "u-1", tracks, music_dir=Path("/mnt/media/music")
        )
        assert resolved_ids == ["guid-bts-spring-rescued"]

    @pytest.mark.asyncio
    async def test_api_users_endpoint_preserves_privacy_isolation_with_duplicate_playlist_names(
        self, respx_mock: respx.MockRouter
    ):
        """Integration test: Verify /api/users proxy endpoint preserves multi-user
        isolation when Alice and Bob both have 'Favorites' playlists."""
        from server.app.main import app
        from httpx import ASGITransport, AsyncClient

        # Setup mock Jellyfin responses
        respx_mock.get(url__regex=r".*/Users$").respond(
            200,
            json=[
                {"Id": "u-alice-guid", "Name": "Alice", "HasPassword": True, "Policy": {"IsDisabled": False}},
                {"Id": "u-bob-guid", "Name": "Bob", "HasPassword": False, "Policy": {"IsDisabled": False}},
            ],
        )
        respx_mock.get(url__regex=r".*/Users/u-alice-guid/Items.*").respond(
            200,
            json={"Items": [{"Id": "pl-alice-fav", "Name": "Favorites", "ChildCount": 15, "OwnerUserId": "u-alice-guid"}]},
        )
        respx_mock.get(url__regex=r".*/Users/u-bob-guid/Items.*").respond(
            200,
            json={"Items": [{"Id": "pl-bob-fav", "Name": "Favorites", "ChildCount": 8, "OwnerUserId": "u-bob-guid"}]},
        )

        orig_jf = getattr(app.state, "jellyfin", None)
        app.state.jellyfin = JellyfinClient(base_url="http://mock-jellyfin:8096", token="tok")
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.get("/api/users", headers={"X-Emby-Token": "tok"})
        finally:
            app.state.jellyfin = orig_jf

        assert resp.status_code == 200
        data = resp.json()
        users_map = {u["id"]: u for u in data["users"]}

        assert "u-alice-guid" in users_map
        assert "u-bob-guid" in users_map

        alice_entry = users_map["u-alice-guid"]
        bob_entry = users_map["u-bob-guid"]

        assert len(alice_entry["playlists"]) == 1
        assert alice_entry["playlists"][0]["id"] == "pl-alice-fav"
        assert alice_entry["playlists"][0]["name"] == "Favorites"

        assert len(bob_entry["playlists"]) == 1
        assert bob_entry["playlists"][0]["id"] == "pl-bob-fav"
        assert bob_entry["playlists"][0]["name"] == "Favorites"

    @pytest.mark.asyncio
    async def test_get_user_playlists_empty_or_none_user_id_returns_empty_list(
        self, jf_client: JellyfinClient
    ):
        """Verify get_user_playlists with empty or None user_id returns empty list without API call."""
        assert await jf_client.get_user_playlists("") == []
        assert await jf_client.get_user_playlists(None) == []

    @pytest.mark.asyncio
    async def test_add_items_to_playlist_clamping_to_max_50(
        self, respx_mock: respx.MockRouter, jf_client: JellyfinClient
    ):
        """Verify specifying an invalid chunk_size (e.g. 100 or -5) is clamped to MAX_CHUNK_SIZE (50)."""
        append_route = respx_mock.post(path="/Playlists/pl-01/Items").respond(204)
        items = [f"t-{i}" for i in range(75)]

        # Request with chunk_size=100 -> clamped to 50
        await jf_client.add_items_to_playlist("u-1", "pl-01", items, chunk_size=100)
        assert append_route.call_count == 2
        chunk1 = append_route.calls[0].request.url.params["ids"].split(",")
        chunk2 = append_route.calls[1].request.url.params["ids"].split(",")
        assert len(chunk1) == 50
        assert len(chunk2) == 25
