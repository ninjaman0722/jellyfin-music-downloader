"""Tests for Stage 1 URL Metadata Resolver and Diff Engine (M2 / Tier 1, Tier 2, Tier 4).

Validates:
- Partitioning of resolved playlist tracks into existing vs missing.
- Sub-20ms diff benchmark asserting 200 tracks diff against 10,000 indexed tracks in <20ms (<5ms typical).
- Streaming URL detection (Spotify playlist/album/track, YouTube Music, YouTube).
- Unicode non-ASCII title preservation during pre-flight diffing.
- Parenthetical tolerance (remix, deluxe, live versions).
- Multi-artist collaboration matching during pre-flight diffing.
- Schema compliance with ResolveResponse and ResolveTrack.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List

import pytest

from server.app.indexer import LibraryIndex
from server.app.resolver import (
    MockMetadataExtractor,
    Resolver,
    ResolveResponse,
    ResolveTrack,
    URLType,
    detect_url_type,
)


# -----------------------------------------------------------------------------
# Tier 1 Tests: URL Classification & Track Partitioning
# -----------------------------------------------------------------------------

def test_detect_url_type_classification():
    """Tier 1: Verify correct URL classification across streaming providers."""
    test_cases = [
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", URLType.SPOTIFY_PLAYLIST),
        ("https://open.spotify.com/album/4yP0hdKO0Ptshxwm0V6Dss", URLType.SPOTIFY_ALBUM),
        ("https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b", URLType.SPOTIFY_TRACK),
        ("https://music.youtube.com/playlist?list=RDCLAK5uy_k", URLType.YOUTUBE_PLAYLIST),
        ("https://music.youtube.com/watch?v=kJQP7kiw5Fk", URLType.YOUTUBE_TRACK),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", URLType.YOUTUBE_TRACK),
        ("https://invalid-url.com/stream", URLType.UNKNOWN),
        ("", URLType.UNKNOWN),
    ]

    for url, expected_type in test_cases:
        assert detect_url_type(url) == expected_type, f"Failed to classify URL: {url}"


def test_resolver_partitions_existing_and_missing_tracks(tmp_path: Path):
    """Tier 1: Verify pre-flight diff correctly partitions tracks into existing and missing."""
    index = LibraryIndex()
    p1 = tmp_path / "The Weeknd" / "After Hours" / "01 - Blinding Lights.mp3"
    p2 = tmp_path / "Daft Punk" / "Discovery" / "02 - One More Time.mp3"
    index.add_track(p1, "Blinding Lights", "The Weeknd")
    index.add_track(p2, "One More Time", "Daft Punk")

    query_tracks = [
        ResolveTrack(id="t1", title="Blinding Lights", artist="The Weeknd", album="After Hours"),
        ResolveTrack(id="t2", title="Save Your Tears", artist="The Weeknd", album="After Hours"),
        ResolveTrack(id="t3", title="One More Time", artist="Daft Punk", album="Discovery"),
        ResolveTrack(id="t4", title="Harder, Better, Faster, Stronger", artist="Daft Punk", album="Discovery"),
    ]

    resolver = Resolver(indexer=index)
    result = resolver.diff_tracks(query_tracks, playlist_name="Test Synthwave")

    # Invariants
    assert result.total_tracks == 4
    assert result.existing_tracks == 2
    assert result.missing_tracks == 2
    assert result.playlist_name == "Test Synthwave"

    # Track-level validation
    t1 = next(t for t in result.tracks if t.id == "t1")
    assert t1.exists_locally is True
    assert t1.local_path == str(p1.resolve())

    t2 = next(t for t in result.tracks if t.id == "t2")
    assert t2.exists_locally is False
    assert t2.local_path is None

    t3 = next(t for t in result.tracks if t.id == "t3")
    assert t3.exists_locally is True
    assert t3.local_path == str(p2.resolve())

    t4 = next(t for t in result.tracks if t.id == "t4")
    assert t4.exists_locally is False
    assert t4.local_path is None


# -----------------------------------------------------------------------------
# Tier 2 Tests: Unicode NFKC Non-ASCII & Parenthetical Diffing
# -----------------------------------------------------------------------------

def test_resolver_non_ascii_unicode_diff(tmp_path: Path):
    """Tier 2: Verify non-ASCII Japanese, Korean, and Cyrillic tracks are matched accurately."""
    index = LibraryIndex()
    jp_path = tmp_path / "RADWIMPS" / "01 - 前前前世.mp3"
    kr_path = tmp_path / "BTS" / "02 - 봄날 (Spring Day).mp3"
    ru_path = tmp_path / "Кино" / "01 - Группа крови.mp3"

    index.add_track(jp_path, "前前前世", "RADWIMPS")
    index.add_track(kr_path, "봄날 (Spring Day)", "BTS")
    index.add_track(ru_path, "Группа крови", "Кино")

    query_tracks = [
        ResolveTrack(id="jp1", title="前前前世", artist="RADWIMPS"),
        ResolveTrack(id="kr1", title="봄날", artist="BTS"),  # Stripped parenthetical
        ResolveTrack(id="ru1", title="Группа крови", artist="Кино"),
        ResolveTrack(id="new1", title="アイドル", artist="YOASOBI"),  # Missing
    ]

    resolver = Resolver(indexer=index)
    result = resolver.diff_tracks(query_tracks)

    assert result.total_tracks == 4
    assert result.existing_tracks == 3
    assert result.missing_tracks == 1

    assert next(t for t in result.tracks if t.id == "jp1").exists_locally is True
    assert next(t for t in result.tracks if t.id == "kr1").exists_locally is True
    assert next(t for t in result.tracks if t.id == "ru1").exists_locally is True
    assert next(t for t in result.tracks if t.id == "new1").exists_locally is False


def test_resolver_collaboration_artist_matching(tmp_path: Path):
    """Tier 2: Verify collaboration tracks match even when artist strings differ slightly."""
    index = LibraryIndex()
    local_path = tmp_path / "David Guetta" / "01 - I'm Good (Blue).mp3"
    index.add_track(local_path, "I'm Good (Blue)", "David Guetta, Bebe Rexha")

    resolver = Resolver(indexer=index)

    # Query lists primary artist only
    tracks = [
        ResolveTrack(id="t_collab", title="I'm Good (Blue)", artist="David Guetta"),
    ]
    result = resolver.diff_tracks(tracks)
    assert result.existing_tracks == 1
    assert result.tracks[0].exists_locally is True
    assert result.tracks[0].local_path == str(local_path.resolve())


# -----------------------------------------------------------------------------
# Tier 3 & Tier 4 Tests: Async Resolution & Sub-20ms Diff Benchmark
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_resolver_with_mock_extractor(tmp_path: Path):
    """Tier 3: Verify async resolve pipeline using mock metadata extractor."""
    index = LibraryIndex()
    f1 = tmp_path / "Starboy.mp3"
    index.add_track(f1, "Starboy", "The Weeknd")

    mock_tracks = [
        ResolveTrack(id="t1", title="Starboy", artist="The Weeknd", album="Starboy"),
        ResolveTrack(id="t2", title="Party Monster", artist="The Weeknd", album="Starboy"),
    ]
    extractor = MockMetadataExtractor(tracks=mock_tracks, playlist_name="Starboy Album")
    resolver = Resolver(indexer=index, extractor=extractor)

    response = await resolver.resolve(["https://open.spotify.com/album/mock123"])

    assert isinstance(response, ResolveResponse)
    assert response.playlist_name == "Starboy Album"
    assert response.total_tracks == 2
    assert response.existing_tracks == 1
    assert response.missing_tracks == 1
    assert response.resolve_time_ms >= 0.0


def test_sub_20ms_resolver_benchmark_200_tracks():
    """Tier 4: Benchmark asserting that diffing 200 tracks against 10,000 indexed
    tracks completes in strictly under 20 milliseconds (target < 5ms).
    """
    index = LibraryIndex()

    # Pre-populate index with 10,000 tracks
    for i in range(10_000):
        fake_path = Path(f"/music/Artist_{i % 300}/Album_{i % 100}/{i:02d} - Song_{i}.mp3")
        index.add_track(fake_path, f"Track {i}", f"Artist {i % 300}")

    assert index.total_indexed == 10_000

    # Build 200-track playlist: 185 present, 15 missing
    query_tracks: List[ResolveTrack] = []
    for i in range(185):
        query_tracks.append(
            ResolveTrack(
                id=f"q_{i}",
                title=f"Track {i}",
                artist=f"Artist {i % 300}",
                album="Benchmark Album",
            )
        )
    for i in range(15):
        query_tracks.append(
            ResolveTrack(
                id=f"missing_{i}",
                title=f"Brand New Track {i}",
                artist=f"Brand New Artist {i}",
                album="Benchmark Album",
            )
        )

    assert len(query_tracks) == 200

    resolver = Resolver(indexer=index)

    # Time the diff operation
    start = time.perf_counter()
    result = resolver.diff_tracks(query_tracks, playlist_name="Benchmark Playlist")
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # Strict performance assertions
    assert result.total_tracks == 200
    assert result.existing_tracks == 185
    assert result.missing_tracks == 15
    assert elapsed_ms < 20.0, f"Benchmark exceeded 20ms: took {elapsed_ms:.2f}ms"


@pytest.mark.asyncio
async def test_resolver_mixed_multi_playlist_and_loose_tracks():
    """Verify resolver partitions and identifies multiple distinct playlists and loose tracks in one batch."""
    index = LibraryIndex()
    tracks_a = [
        ResolveTrack(id="a1", title="Song A1", artist="Artist A", album="Album A", source_playlist_name="Chill Vibes"),
        ResolveTrack(id="a2", title="Song A2", artist="Artist A", album="Album A", source_playlist_name="Chill Vibes"),
    ]
    tracks_b = [
        ResolveTrack(id="b1", title="Song B1", artist="Artist B", album="Album B", source_playlist_name="Workout Hits"),
    ]
    loose = [
        ResolveTrack(id="l1", title="Loose Song 1", artist="Artist C", album="Single", source_playlist_name=None),
        ResolveTrack(id="l2", title="Loose Song 2", artist="Artist D", album="Single", source_playlist_name=None),
    ]

    class MultiUrlExtractor:
        async def extract_tracks(self, url: str):
            if "chill" in url:
                return "Chill Vibes", "pl-chill", [t.model_copy() for t in tracks_a]
            elif "workout" in url:
                return "Workout Hits", "pl-workout", [t.model_copy() for t in tracks_b]
            else:
                return None, "pl-loose", [loose[0] if "1" in url else loose[1]]

    resolver = Resolver(indexer=index, extractor=MultiUrlExtractor())
    urls = [
        "https://open.spotify.com/playlist/chill",
        "https://open.spotify.com/playlist/workout",
        "https://open.spotify.com/track/loose1",
        "https://open.spotify.com/track/loose2",
    ]

    res = await resolver.resolve(urls)
    assert res.total_tracks == 5
    assert res.detected_playlists == ["Chill Vibes", "Workout Hits"]
    assert res.loose_tracks_count == 2
    assert sum(1 for t in res.tracks if t.source_playlist_name == "Chill Vibes") == 2
    assert sum(1 for t in res.tracks if t.source_playlist_name == "Workout Hits") == 1
    assert sum(1 for t in res.tracks if t.source_playlist_name is None) == 2


@pytest.mark.asyncio
async def test_spotify_metadata_extractor_parsing(monkeypatch):
    """Verify SpotifyMetadataExtractor extracts tracks and handles single tracks vs playlists."""
    import json
    from unittest.mock import MagicMock
    from server.app.resolver import SpotifyMetadataExtractor

    sample_playlist_json = {
        "props": {
            "pageProps": {
                "state": {
                    "data": {
                        "entity": {
                            "title": "📻 Let's Chill 📻",
                            "trackList": [
                                {"title": "Song 1", "subtitle": "Artist 1", "duration": 200000, "uri": "spotify:track:abc1"},
                                {"title": "Song 2", "subtitle": "Artist 2", "duration": 180000, "uri": "spotify:track:abc2"},
                            ]
                        }
                    }
                }
            }
        }
    }
    sample_html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(sample_playlist_json)}</script></html>'

    mock_resp = MagicMock()
    mock_resp.read.return_value = sample_html.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: mock_resp)

    extractor = SpotifyMetadataExtractor()
    pl_name, pl_id, tracks = await extractor.extract_tracks("https://open.spotify.com/playlist/test123")

    assert pl_name == "📻 Let's Chill 📻"
    assert len(tracks) == 2
    assert tracks[0].title == "Song 1"
    assert tracks[0].artist == "Artist 1"
    assert tracks[0].source_playlist_name == "📻 Let's Chill 📻"
    assert tracks[1].title == "Song 2"
    assert tracks[1].source_playlist_name == "📻 Let's Chill 📻"


@pytest.mark.asyncio
async def test_spotify_artist_discography_mode(monkeypatch):
    """Verify SpotifyMetadataExtractor resolves all albums and deduplicates tracks in discography mode."""
    import json
    from unittest.mock import MagicMock
    from server.app.resolver import SpotifyMetadataExtractor

    artist_html = """
    <html>
      <meta property="og:title" content="Test Artist" />
      <a href="/album/1111111111111111111111">Album 1</a>
      <a href="/album/2222222222222222222222">Album 2</a>
    </html>
    """

    alb1_json = {
        "props": {"pageProps": {"state": {"data": {"entity": {
            "title": "Album 1",
            "trackList": [
                {"title": "Track One", "subtitle": "Test Artist", "duration": 120000, "uri": "spotify:track:t1"},
                {"title": "Shared Single", "subtitle": "Test Artist", "duration": 180000, "uri": "spotify:track:t2"},
            ]
        }}}}}
    }
    alb2_json = {
        "props": {"pageProps": {"state": {"data": {"entity": {
            "title": "Album 2",
            "trackList": [
                {"title": "Shared Single", "subtitle": "Test Artist", "duration": 180000, "uri": "spotify:track:t2"},
                {"title": "Track Two", "subtitle": "Test Artist", "duration": 200000, "uri": "spotify:track:t3"},
            ]
        }}}}}
    }

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        mock = MagicMock()
        if "embed/album/1111111111111111111111" in url:
            mock.read.return_value = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(alb1_json)}</script>'.encode("utf-8")
        elif "embed/album/2222222222222222222222" in url:
            mock.read.return_value = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(alb2_json)}</script>'.encode("utf-8")
        else:
            mock.read.return_value = artist_html.encode("utf-8")
        mock.__enter__.return_value = mock
        return mock

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    extractor = SpotifyMetadataExtractor()
    pl_name, pl_id, tracks = await extractor.extract_tracks(
        "https://open.spotify.com/artist/testartist12345678901",
        artist_mode="discography"
    )

    assert pl_name == "Test Artist (Discography)"
    assert pl_id == "pl-testartist12345678901"
    # Total tracks should be 3 (Track One, Shared Single, Track Two) after deduplication
    assert len(tracks) == 3
    assert tracks[0].title == "Track One"
    assert tracks[0].album == "Album 1"
    assert tracks[1].title == "Shared Single"
    assert tracks[1].album == "Album 1"
    assert tracks[2].title == "Track Two"
    assert tracks[2].album == "Album 2"


@pytest.mark.asyncio
async def test_spotify_artist_top_tracks_mode(monkeypatch):
    """Verify SpotifyMetadataExtractor returns popular tracks in top_tracks mode."""
    import json
    from unittest.mock import MagicMock
    from server.app.resolver import SpotifyMetadataExtractor

    artist_top_json = {
        "props": {"pageProps": {"state": {"data": {"entity": {
            "title": "Test Artist",
            "trackList": [
                {"title": f"Top Hit {i}", "subtitle": "Test Artist", "duration": 180000, "uri": f"spotify:track:hit{i}"}
                for i in range(1, 11)
            ]
        }}}}}
    }

    def fake_urlopen(req, *args, **kwargs):
        mock = MagicMock()
        mock.read.return_value = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(artist_top_json)}</script>'.encode("utf-8")
        mock.__enter__.return_value = mock
        return mock

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    extractor = SpotifyMetadataExtractor()
    pl_name, pl_id, tracks = await extractor.extract_tracks(
        "https://open.spotify.com/artist/testartist12345678901",
        artist_mode="top_tracks"
    )

    assert pl_name == "Test Artist (Top Tracks)"
    assert len(tracks) == 10
    assert tracks[0].title == "Top Hit 1"
    assert tracks[0].source_playlist_name is None


@pytest.mark.asyncio
async def test_resolver_is_playlist_classification():
    """Verify is_playlist flag correctly distinguishes playlists from albums/artists/tracks."""
    index = LibraryIndex()
    resolver = Resolver(indexer=index)

    # 1. Tracks without playlist source (e.g. Album, Artist discography, Loose tracks)
    album_tracks = [
        ResolveTrack(id="t1", title="Waves", artist="Fiji Blue", album="Reasons You Should Hate Me", source_playlist_name=None),
        ResolveTrack(id="t2", title="Day by Day", artist="Fiji Blue", album="Reasons You Should Hate Me", source_playlist_name=None),
    ]
    album_res = resolver.diff_tracks(album_tracks, playlist_name="Reasons You Should Hate Me", is_playlist=False)
    assert album_res.is_playlist is False
    assert album_res.detected_playlists == []
    assert album_res.loose_tracks_count == 2

    # 2. Tracks with playlist source
    pl_tracks = [
        ResolveTrack(id="p1", title="Song 1", artist="Artist 1", source_playlist_name="My Summer Hits"),
    ]
    pl_res = resolver.diff_tracks(pl_tracks, is_playlist=True)
    assert pl_res.is_playlist is True
    assert pl_res.detected_playlists == ["My Summer Hits"]


