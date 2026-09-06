"""server/app/resolver.py
Pre-Flight URL Metadata Resolver and Sub-20ms Diff Engine.

Provides:
- URL classification for Spotify and YouTube Music streams.
- Metadata extraction into typed ResolveTrack models.
- High-speed in-memory diffing against LibraryIndex (<20ms benchmark).
- Partitioning into existing vs missing tracks for the Stage 2 downloader queue.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, Tuple

from pydantic import BaseModel, Field

from server.app.indexer import LibraryIndex, artists_match, normalize_key

logger = logging.getLogger("jellyfin_music_daemon.resolver")


class URLType(str, Enum):
    SPOTIFY_PLAYLIST = "spotify_playlist"
    SPOTIFY_ALBUM = "spotify_album"
    SPOTIFY_ARTIST = "spotify_artist"
    SPOTIFY_TRACK = "spotify_track"
    YOUTUBE_PLAYLIST = "youtube_playlist"
    YOUTUBE_TRACK = "youtube_track"
    UNKNOWN = "unknown"


def detect_url_type(url: str) -> URLType:
    """Classify incoming streaming media URL."""
    if not url:
        return URLType.UNKNOWN

    clean_url = url.strip()
    if "open.spotify.com" in clean_url or "spotify:" in clean_url:
        if "playlist" in clean_url:
            return URLType.SPOTIFY_PLAYLIST
        if "album" in clean_url:
            return URLType.SPOTIFY_ALBUM
        if "artist" in clean_url:
            return URLType.SPOTIFY_ARTIST
        if "track" in clean_url:
            return URLType.SPOTIFY_TRACK
        return URLType.SPOTIFY_PLAYLIST

    if "music.youtube.com" in clean_url or "youtube.com" in clean_url or "youtu.be" in clean_url:
        if "list=" in clean_url:
            return URLType.YOUTUBE_PLAYLIST
        return URLType.YOUTUBE_TRACK

    return URLType.UNKNOWN


class ResolveTrack(BaseModel):
    """Metadata for an individual resolved track matching main.py schema."""
    id: str = Field(default_factory=lambda: f"t_{uuid.uuid4().hex[:8]}")
    title: str = "Track"
    artist: str = "Artist"
    album: str = "Album"
    disc_number: int = 1
    track_number: int = 1
    duration_ms: float = 180000.0
    exists_locally: bool = False
    local_path: Optional[str] = None
    source_playlist_name: Optional[str] = None


class ResolveRequest(BaseModel):
    """Request payload for POST /api/resolve."""
    urls: List[str] = Field(..., min_length=1, description="Playlist or track URLs to resolve")
    target_user_id: Optional[str] = None
    artist_mode: str = Field(default="discography", description="Artist resolution mode: 'discography' or 'top_tracks'")


class ResolveResponse(BaseModel):
    """Response payload for POST /api/resolve."""
    playlist_name: str = "Resolved Playlist"
    playlist_id: str = "pl-resolved-01"
    is_playlist: bool = False
    detected_playlists: List[str] = Field(default_factory=list)
    loose_tracks_count: int = 0
    total_tracks: int = 0
    existing_tracks: int = 0
    missing_tracks: int = 0
    resolve_time_ms: float = 0.0
    tracks: List[ResolveTrack] = Field(default_factory=list)


class MetadataExtractor(Protocol):
    """Interface protocol for extracting track metadata from URLs without downloading."""

    async def extract_tracks(self, url: str, artist_mode: str = "discography") -> Tuple[Optional[str], str, List[ResolveTrack]]:
        """Return (playlist_name, playlist_id, tracks)."""
        ...


class YtDlpMetadataExtractor:
    """Metadata extraction engine utilizing yt-dlp flat-playlist extraction."""

    async def extract_tracks(self, url: str, artist_mode: str = "discography") -> Tuple[str, str, List[ResolveTrack]]:
        try:
            import yt_dlp
        except ImportError:
            logger.warning("yt_dlp not installed; returning synthetic fallback track.")
            return self._fallback_track(url)

        ydl_opts = {
            "extract_flat": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }

        loop = asyncio.get_running_loop()

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await loop.run_in_executor(None, _extract)
        except Exception as exc:
            logger.error("yt-dlp metadata extraction failed for '%s': %s", url, exc)
            return self._fallback_track(url)

        if not info:
            return self._fallback_track(url)

        url_type = detect_url_type(url)
        is_playlist = url_type in (URLType.SPOTIFY_PLAYLIST, URLType.YOUTUBE_PLAYLIST)
        raw_title = info.get("title")
        playlist_name = (raw_title or "Streaming Tracks") if is_playlist else None
        source_pl = playlist_name if is_playlist else None
        playlist_id = info.get("id") or f"pl-{uuid.uuid4().hex[:8]}"
        entries = info.get("entries") or [info]

        tracks: List[ResolveTrack] = []
        for idx, entry in enumerate(entries, start=1):
            if not entry:
                continue
            title = entry.get("title") or entry.get("track") or f"Track {idx}"
            artist = entry.get("artist") or entry.get("uploader") or entry.get("channel") or "Unknown Artist"
            album = entry.get("album") or playlist_name or "Single"
            duration = float(entry.get("duration") or 180) * 1000.0

            tracks.append(
                ResolveTrack(
                    id=f"t_{entry.get('id') or idx}",
                    title=title,
                    artist=artist,
                    album=album,
                    disc_number=int(entry.get("disc_number") or 1),
                    track_number=int(entry.get("track_number") or idx),
                    duration_ms=duration,
                    exists_locally=False,
                    local_path=None,
                    source_playlist_name=source_pl,
                )
            )

        return playlist_name or "Streaming Tracks", playlist_id, tracks

    @staticmethod
    def _fallback_track(url: str) -> Tuple[str, str, List[ResolveTrack]]:
        url_type = detect_url_type(url)
        is_pl = url_type in (URLType.SPOTIFY_PLAYLIST, URLType.YOUTUBE_PLAYLIST)
        pl_name = "Imported Tracks" if is_pl else None
        pl_id = f"pl-{uuid.uuid4().hex[:8]}"
        track = ResolveTrack(
            id="t_1",
            title=url.split("/")[-1].split("?")[0] or "Track 1",
            artist="Unknown Artist",
            album=pl_name or "Single",
            disc_number=1,
            track_number=1,
            duration_ms=180000.0,
            exists_locally=False,
            local_path=None,
            source_playlist_name=pl_name,
        )
        return pl_name or "Imported Tracks", pl_id, [track]


class SpotifyMetadataExtractor:
    """Fast, zero-credential metadata extraction engine for Spotify playlists, albums, artists, and tracks.

    Fetches public embed metadata and catalog releases in sub-second times.
    Requires no Spotify API credentials or SpotDL authentication.
    """

    async def _extract_artist_discography(
        self,
        artist_id: str,
        original_url: str,
    ) -> Optional[Tuple[str, str, List[ResolveTrack]]]:
        """Scrape artist discography (all albums, singles, EPs) and return deduplicated tracks."""
        loop = asyncio.get_running_loop()

        def _fetch_artist_page() -> str:
            import urllib.request
            req = urllib.request.Request(
                f"https://open.spotify.com/artist/{artist_id}",
                headers={"User-Agent": "curl/7.88.1"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.read().decode("utf-8", errors="ignore")

        try:
            page_html = await loop.run_in_executor(None, _fetch_artist_page)
        except Exception as exc:
            logger.warning("Failed to fetch Spotify artist page for '%s': %s", artist_id, exc)
            return None

        import json
        import unicodedata

        m_title = re.search(r'<meta\s+(?:property|name)="og:title"\s+content="([^"]+)"', page_html)
        if not m_title:
            m_title = re.search(r"<title>(.*?)(?: \| Spotify)?</title>", page_html)
        raw_artist_name = m_title.group(1).split(" | ")[0].strip() if m_title else "Artist"
        artist_name = unicodedata.normalize("NFKC", str(raw_artist_name)).strip()

        # Separate artist's own releases from "Appears On" third-party compilations
        pos_appears_on = page_html.find("Appears On")
        artist_section = page_html[:pos_appears_on] if pos_appears_on != -1 else page_html

        album_ids = list(dict.fromkeys(re.findall(r"/album/([a-zA-Z0-9]{22})", artist_section)))
        if not album_ids:
            # Fallback to whole page if no albums found before "Appears On"
            album_ids = list(dict.fromkeys(re.findall(r"/album/([a-zA-Z0-9]{22})", page_html)))
        if not album_ids:
            logger.info("No album IDs found on artist page for '%s', falling back to top tracks embed", artist_id)
            return None

        playlist_name = f"{artist_name} (Discography)"
        playlist_id = f"pl-{artist_id}"

        sem = asyncio.Semaphore(8)

        def _fetch_album_embed(aid: str) -> Tuple[Optional[str], List[dict]]:
            import urllib.request
            url = f"https://open.spotify.com/embed/album/{aid}"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.88.1"})
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = resp.read().decode("utf-8", errors="ignore")
                    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', data)
                    if m:
                        j = json.loads(m.group(1))
                        ent = j.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                        alb_title = ent.get("title") or "Single"
                        raw_tracks = ent.get("trackList", [])
                        return alb_title, raw_tracks
            except Exception as e:
                logger.debug("Album embed fetch failed for %s: %s", aid, e)
            return None, []

        async def _album_worker(aid: str):
            async with sem:
                return await loop.run_in_executor(None, lambda: _fetch_album_embed(aid))

        album_results = await asyncio.gather(*(_album_worker(aid) for aid in album_ids))

        seen_keys = set()
        tracks: List[ResolveTrack] = []
        track_idx = 1

        for alb_title, raw_tracks in album_results:
            clean_alb = unicodedata.normalize("NFKC", str(alb_title or playlist_name)).strip()
            for item in raw_tracks:
                t_title = unicodedata.normalize("NFKC", str(item.get("title") or f"Track {track_idx}")).strip()
                t_artist = unicodedata.normalize("NFKC", str(item.get("subtitle") or artist_name)).strip()

                # Filter out tracks by other artists on compilation or multi-artist releases
                if not artists_match(t_artist, artist_name):
                    continue

                dedup_key = (t_title.lower(), t_artist.lower())
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                uri = item.get("uri", "")
                track_id = uri.split(":")[-1] if uri else f"t_{artist_id}_{track_idx}"
                duration = float(item.get("duration") or 180000.0)

                tracks.append(
                    ResolveTrack(
                        id=f"t_{track_id}",
                        title=t_title,
                        artist=t_artist,
                        album=clean_alb,
                        disc_number=1,
                        track_number=track_idx,
                        duration_ms=duration,
                        exists_locally=False,
                        local_path=None,
                        source_playlist_name=None,
                    )
                )
                track_idx += 1

        logger.info(
            "Resolved Spotify artist discography for '%s': %d tracks across %d releases",
            artist_name,
            len(tracks),
            len(album_ids),
        )
        return playlist_name, playlist_id, tracks

    async def extract_tracks(
        self,
        url: str,
        artist_mode: str = "discography",
    ) -> Tuple[Optional[str], str, List[ResolveTrack]]:
        clean_match = re.search(r'(playlist|album|artist|track)[/:]([a-zA-Z0-9]+)', url)
        if not clean_match:
            logger.warning("Could not parse Spotify entity type and ID from '%s'", url)
            return YtDlpMetadataExtractor._fallback_track(url)

        entity_type = clean_match.group(1).lower()
        spotify_id = clean_match.group(2)

        if entity_type == "artist" and artist_mode == "discography":
            discog_res = await self._extract_artist_discography(spotify_id, url)
            if discog_res and discog_res[2]:
                return discog_res

        loop = asyncio.get_running_loop()

        def _fetch_page(sid: str) -> str:
            import urllib.request
            target_embed = f"https://open.spotify.com/embed/{entity_type}/{sid}"
            req = urllib.request.Request(
                target_embed,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.read().decode("utf-8")

        candidate_ids = [spotify_id]
        if "l" in spotify_id:
            candidate_ids.append(spotify_id.replace("l", "I"))
        if "I" in spotify_id:
            candidate_ids.append(spotify_id.replace("I", "l"))

        html = None
        matched_id = spotify_id
        for cid in candidate_ids:
            try:
                page_html = await loop.run_in_executor(None, lambda sid=cid: _fetch_page(sid))
                if '<script id="__NEXT_DATA__"' in page_html and '"status":404' not in page_html and '"status": 404' not in page_html:
                    html = page_html
                    matched_id = cid
                    break
            except Exception as exc:
                logger.debug("Attempt for %s failed: %s", cid, exc)

        if not html:
            logger.warning("Spotify embed data missing or returned 404 for '%s'", url)
            return YtDlpMetadataExtractor._fallback_track(url)

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if not match:
            logger.warning("Spotify embed data missing __NEXT_DATA__ for '%s'", url)
            return YtDlpMetadataExtractor._fallback_track(url)

        try:
            import json
            import unicodedata
            data = json.loads(match.group(1))
            props = data.get("props", {}).get("pageProps", {})
            state_data = props.get("state", {}).get("data", {})
            entity = state_data.get("entity", {})

            raw_title = entity.get("title") or entity.get("name") or "Spotify Media"
            clean_title = unicodedata.normalize("NFKC", str(raw_title)).strip()
            if entity_type == "artist":
                clean_title = f"{clean_title} (Top Tracks)"
            playlist_id = f"pl-{spotify_id}"

            tracks: List[ResolveTrack] = []

            if entity_type == "track":
                # Single loose track
                artists_list = entity.get("artists", [])
                art_str = ", ".join(a.get("name", "") for a in artists_list if a.get("name")) or entity.get("subtitle") or "Unknown Artist"
                clean_artist = unicodedata.normalize("NFKC", str(art_str)).strip()
                duration = float(entity.get("duration") or 180000.0)

                tracks.append(
                    ResolveTrack(
                        id=f"t_{spotify_id}",
                        title=clean_title,
                        artist=clean_artist,
                        album="Single",
                        disc_number=1,
                        track_number=1,
                        duration_ms=duration,
                        exists_locally=False,
                        local_path=None,
                        source_playlist_name=None,
                    )
                )
                return None, playlist_id, tracks

            # Playlist, Album, or Artist
            raw_track_list = entity.get("trackList", [])
            for idx, item in enumerate(raw_track_list, start=1):
                t_title = unicodedata.normalize("NFKC", str(item.get("title") or f"Track {idx}")).strip()
                t_artist = unicodedata.normalize("NFKC", str(item.get("subtitle") or raw_title or "Unknown Artist")).strip()
                t_uri = item.get("uri") or f"t_{spotify_id}_{idx}"
                track_id = t_uri.split(":")[-1]
                t_dur = float(item.get("duration") or 180000.0)

                tracks.append(
                    ResolveTrack(
                        id=f"t_{track_id}",
                        title=t_title,
                        artist=t_artist,
                        album=clean_title,
                        disc_number=1,
                        track_number=idx,
                        duration_ms=t_dur,
                        exists_locally=False,
                        local_path=None,
                        source_playlist_name=clean_title if entity_type == "playlist" else None,
                    )
                )

            logger.info("Successfully resolved Spotify %s '%s' (%d tracks)", entity_type, clean_title, len(tracks))
            return clean_title, playlist_id, tracks

        except Exception as exc:
            logger.error("Failed to parse Spotify embed JSON for '%s': %s", url, exc)
            return YtDlpMetadataExtractor._fallback_track(url)


class CompositeMetadataExtractor:
    """Smart router dispatching Spotify URLs to SpotifyMetadataExtractor and YouTube to YtDlpMetadataExtractor."""

    def __init__(self):
        self.spotify_extractor = SpotifyMetadataExtractor()
        self.ytdlp_extractor = YtDlpMetadataExtractor()

    async def extract_tracks(
        self,
        url: str,
        artist_mode: str = "discography",
    ) -> Tuple[Optional[str], str, List[ResolveTrack]]:
        url_type = detect_url_type(url)
        if url_type in (URLType.SPOTIFY_PLAYLIST, URLType.SPOTIFY_ALBUM, URLType.SPOTIFY_ARTIST, URLType.SPOTIFY_TRACK):
            pl_name, pl_id, tracks = await self.spotify_extractor.extract_tracks(url, artist_mode=artist_mode)
            if pl_name != "Imported Tracks" and tracks:
                return pl_name, pl_id, tracks

        return await self.ytdlp_extractor.extract_tracks(url, artist_mode=artist_mode)


class MockMetadataExtractor:
    """Mock extractor for offline testing and fixture injection."""

    def __init__(self, tracks: Optional[List[ResolveTrack]] = None, playlist_name: str = "Mock Playlist"):
        self.tracks = tracks or []
        self.playlist_name = playlist_name

    async def extract_tracks(
        self,
        url: str,
        artist_mode: str = "discography",
    ) -> Tuple[str, str, List[ResolveTrack]]:
        pl_id = f"pl-mock-{uuid.uuid4().hex[:6]}"
        copied = []
        for t in self.tracks:
            c = t.model_copy()
            if not c.source_playlist_name:
                c.source_playlist_name = self.playlist_name
            copied.append(c)
        return self.playlist_name, pl_id, copied


class Resolver:
    """Pre-flight URL metadata resolution and sub-20ms diff engine."""

    def __init__(
        self,
        indexer: LibraryIndex,
        extractor: Optional[MetadataExtractor] = None,
    ):
        self.indexer = indexer
        self.extractor = extractor or CompositeMetadataExtractor()

    def diff_tracks(
        self,
        tracks: List[ResolveTrack],
        playlist_name: str = "Resolved Playlist",
        playlist_id: str = "pl-01",
        is_playlist: Optional[bool] = None,
    ) -> ResolveResponse:
        """Diff a list of resolved tracks against the in-memory LibraryIndex in <20ms (<5ms typical)."""
        start_time = time.perf_counter()

        existing_count = 0
        missing_count = 0

        for track in tracks:
            match_path = self.indexer.find_match(track.title, track.artist)
            if match_path is not None:
                track.exists_locally = True
                track.local_path = str(match_path)
                existing_count += 1
            else:
                track.exists_locally = False
                track.local_path = None
                missing_count += 1

        elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        detected_playlists = []
        for t in tracks:
            spl = getattr(t, "source_playlist_name", None)
            if spl and spl not in detected_playlists:
                if spl not in ("Streaming Tracks", "Imported Tracks", "Resolved Playlist"):
                    detected_playlists.append(spl)

        loose_tracks_count = sum(1 for t in tracks if not getattr(t, "source_playlist_name", None))
        effective_is_playlist = is_playlist if is_playlist is not None else bool(detected_playlists)

        if len(detected_playlists) > 1:
            effective_summary_name = f"{len(detected_playlists)} Playlists: {', '.join(detected_playlists)}"
        elif len(detected_playlists) == 1:
            effective_summary_name = detected_playlists[0]
        else:
            effective_summary_name = playlist_name or "Resolved Playlist"

        return ResolveResponse(
            playlist_name=effective_summary_name,
            playlist_id=playlist_id,
            is_playlist=effective_is_playlist,
            detected_playlists=detected_playlists,
            loose_tracks_count=loose_tracks_count,
            total_tracks=len(tracks),
            existing_tracks=existing_count,
            missing_tracks=missing_count,
            resolve_time_ms=round(elapsed_ms, 2),
            tracks=tracks,
        )

    async def resolve(
        self,
        urls: List[str],
        target_user_id: Optional[str] = None,
        artist_mode: str = "discography",
    ) -> ResolveResponse:
        """Asynchronously resolve URL metadata and diff against the library index."""
        if not urls:
            raise ValueError("urls list cannot be empty")

        all_tracks: List[ResolveTrack] = []
        master_playlist_name: Optional[str] = None
        master_playlist_id: Optional[str] = None

        for url in urls:
            try:
                pl_name, pl_id, tracks = await self.extractor.extract_tracks(url, artist_mode=artist_mode)
            except TypeError:
                pl_name, pl_id, tracks = await self.extractor.extract_tracks(url)
            if not master_playlist_name:
                master_playlist_name = pl_name
                master_playlist_id = pl_id
            all_tracks.extend(tracks)

        any_playlist_url = any(
            detect_url_type(u) in (URLType.SPOTIFY_PLAYLIST, URLType.YOUTUBE_PLAYLIST)
            for u in urls
        )

        return self.diff_tracks(
            tracks=all_tracks,
            playlist_name=master_playlist_name or "Resolved Playlist",
            playlist_id=master_playlist_id or "pl-01",
            is_playlist=True if any_playlist_url else None,
        )
