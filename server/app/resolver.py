"""server/app/resolver.py
Pre-Flight URL Metadata Resolver and Sub-20ms Diff Engine.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
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
    url: Optional[str] = None


class ResolveRequest(BaseModel):
    urls: List[str] = Field(..., min_length=1, description="Playlist or track URLs to resolve")
    target_user_id: Optional[str] = None
    artist_mode: str = Field(default="discography", description="Artist resolution mode: 'discography' or 'top_tracks'")


class ResolveResponse(BaseModel):
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
    async def extract_tracks(self, url: str, artist_mode: str = "discography") -> Tuple[Optional[str], str, List[ResolveTrack]]:
        ...


def clean_yt_track_metadata(
    raw_title: str,
    raw_artist: Optional[str] = None,
    uploader: Optional[str] = None,
) -> Tuple[str, str]:
    """Cleans YouTube track artist and title.
    - Strips ' - Topic' from artists/channels.
    - Parses 'Artist - Title' from raw_title if artist is missing or generic.
    - Strips common video artifacts from title ((Official Video), [Visualizer], etc.).
    """
    artist = (raw_artist or uploader or "Unknown Artist").strip()
    artist = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE).strip()
    title = (raw_title or "").strip()

    if " - " in title:
        parts = title.split(" - ", 1)
        cand_artist = re.sub(r"\s*-\s*Topic$", "", parts[0], flags=re.IGNORECASE).strip()
        cand_title = parts[1].strip()
        if cand_artist and len(cand_artist) <= 60:
            if (
                not raw_artist
                or raw_artist.lower() in ("unknown artist", "unknown", "various artists")
                or raw_artist == uploader
                or cand_artist.lower() == artist.lower()
            ):
                artist = cand_artist
                title = cand_title
            elif cand_artist.lower() in artist.lower():
                title = cand_title

    # Strip common video artifacts from title
    patterns = [
        r"\s*[\(\[](?:Official\s+)?(?:Music\s+)?Video[\)\]]",
        r"\s*[\(\[](?:Official\s+)?Audio[\)\]]",
        r"\s*[\(\[]Lyric\s+Video[\)\]]",
        r"\s*[\(\[]Visualizer[\)\]]",
        r"\s*[\(\[]FREE\s+DOWNLOAD[\)\]]",
        r"\s*[\(\[](?:Official\s+)?HD\s+Video[\)\]]",
        r"\s*[\(\[]HD[\)\]]",
        r"\s*[\(\[]4K[\)\]]",
        r"\s*[\(\[]Official[\)\]]",
    ]
    for pat in patterns:
        title = re.sub(pat, "", title, flags=re.IGNORECASE)

    title = re.sub(r"\s+", " ", title).strip()
    return artist, title


class YtDlpMetadataExtractor:
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

        is_official_album = False
        if "list=OLAK5uy_" in url or info.get("_type") == "album":
            is_official_album = True

        tracks: List[ResolveTrack] = []
        for idx, entry in enumerate(entries, start=1):
            if not entry:
                continue
            raw_title = entry.get("title") or entry.get("track") or f"Track {idx}"
            raw_artist = entry.get("artist")
            uploader = entry.get("uploader") or entry.get("channel")
            artist, title = clean_yt_track_metadata(raw_title, raw_artist, uploader)

            raw_album = entry.get("album")
            if raw_album and str(raw_album).strip():
                album = str(raw_album).strip()
            elif is_official_album:
                album = playlist_name or "Album"
            else:
                album = "Single"

            duration = float(entry.get("duration") or 180) * 1000.0
            track_url = entry.get("url") or entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={entry.get('id')}" if entry.get("id") else None)

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
                    url=track_url,
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
            album="Single",
            disc_number=1,
            track_number=1,
            duration_ms=180000.0,
            exists_locally=False,
            local_path=None,
            source_playlist_name=pl_name,
            url=url if not is_pl else None,
        )
        return pl_name or "Imported Tracks", pl_id, [track]


class SpotifyMetadataExtractor:
    @staticmethod
    def _init_spotdl_client() -> bool:
        """Ensures spotdl virtualenv is available on sys.path and initializes SpotifyClient."""
        import glob
        for p in glob.glob("/opt/spotdl/lib/python*/site-packages"):
            if p not in sys.path:
                sys.path.insert(0, p)
        try:
            from spotdl.utils.spotify import SpotifyClient
            try:
                client_id = os.environ.get("SPOTIFY_CLIENT_ID", "5f573c9620494bae87890c0f08a60293")
                client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "212476d9b0f3472eaa762d90b19b0ba8")
                SpotifyClient.init(client_id, client_secret)
            except Exception:
                pass
            return True
        except ImportError:
            return False

    def _extract_playlist_via_spotdl(
        self,
        url: str,
        spotify_id: str,
    ) -> Optional[Tuple[str, str, List[ResolveTrack]]]:
        """Extracts complete Spotify playlist using SpotDL's internal client (bypassing 100-track embed cap)."""
        if not self._init_spotdl_client():
            return None

        try:
            from spotdl.types.playlist import Playlist
            metadata, songs = Playlist.get_metadata(url)
            if not songs:
                return None

            raw_title = metadata.get("name") or "Spotify Playlist"
            clean_title = unicodedata.normalize("NFKC", str(raw_title)).strip()
            playlist_id = f"pl-{spotify_id}"

            tracks: List[ResolveTrack] = []
            for idx, s in enumerate(songs, start=1):
                t_title = unicodedata.normalize("NFKC", str(s.name or f"Track {idx}")).strip()
                if isinstance(s.artists, list) and s.artists:
                    art_str = ", ".join(s.artists)
                else:
                    art_str = s.artist or "Unknown Artist"
                t_artist = unicodedata.normalize("NFKC", str(art_str)).strip()
                alb_name = s.album_name or clean_title
                t_album = unicodedata.normalize("NFKC", str(alb_name)).strip()
                t_dur = float(s.duration * 1000.0) if s.duration else 180000.0

                tracks.append(
                    ResolveTrack(
                        id=f"t_{s.song_id}",
                        title=t_title,
                        artist=t_artist,
                        album=t_album,
                        disc_number=s.disc_number or 1,
                        track_number=s.track_number or idx,
                        duration_ms=t_dur,
                        exists_locally=False,
                        local_path=None,
                        source_playlist_name=clean_title,
                        url=s.url,
                    )
                )

            return clean_title, playlist_id, tracks
        except Exception as exc:
            logger.warning("SpotDL playlist extraction failed: %s", exc)
            return None

    def _fetch_remaining_playlist_tracks(
        self,
        playlist_id: str,
        playlist_name: str,
        start_offset: int = 100,
    ) -> List[ResolveTrack]:
        # 1. Prefer SpotDL internal playlist extraction if available
        try:
            if self._init_spotdl_client():
                from spotdl.types.playlist import Playlist
                pl_url = f"https://open.spotify.com/playlist/{playlist_id}"
                _, all_songs = Playlist.get_metadata(pl_url)
                if all_songs and len(all_songs) > start_offset:
                    extra_tracks = []
                    for idx, s in enumerate(all_songs[start_offset:], start=start_offset + 1):
                        art_str = ", ".join(s.artists) if isinstance(s.artists, list) and s.artists else (s.artist or playlist_name)
                        extra_tracks.append(
                            ResolveTrack(
                                id=f"t_{s.song_id}",
                                title=unicodedata.normalize("NFKC", str(s.name)).strip(),
                                artist=unicodedata.normalize("NFKC", str(art_str)).strip(),
                                album=unicodedata.normalize("NFKC", str(s.album_name or playlist_name)).strip(),
                                disc_number=s.disc_number or 1,
                                track_number=s.track_number or idx,
                                duration_ms=float(s.duration * 1000.0) if s.duration else 180000.0,
                                exists_locally=False,
                                local_path=None,
                                source_playlist_name=playlist_name,
                                url=s.url,
                            )
                        )
                    return extra_tracks
        except Exception as exc:
            logger.warning("SpotDL remaining tracks pagination failed: %s", exc)

        client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
        client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
        refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()

        token = None

        # 1. Prefer official credentials if configured
        if client_id and client_secret:
            try:
                auth_bytes = f"{client_id}:{client_secret}".encode("utf-8")
                b64_auth = base64.b64encode(auth_bytes).decode("utf-8")
                payload = (
                    urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode("utf-8")
                    if refresh_token else b"grant_type=client_credentials"
                )
                t_req = urllib.request.Request(
                    "https://accounts.spotify.com/api/token",
                    data=payload,
                    headers={"Authorization": f"Basic {b64_auth}", "Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(t_req, timeout=10) as resp:
                    token = json.loads(resp.read().decode("utf-8")).get("access_token")
            except Exception as exc:
                logger.warning("Official Spotify auth failed, falling back to anonymous token: %s", exc)

        # 2. Fallback: Acquire anonymous web player token
        if not token:
            try:
                anon_req = urllib.request.Request(
                    "https://open.spotify.com/get_access_token?reason=transport&productType=web_player",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
                )
                with urllib.request.urlopen(anon_req, timeout=10) as resp:
                    token = json.loads(resp.read().decode("utf-8")).get("accessToken")
                    if token:
                        logger.info("Acquired anonymous Spotify Web Player token for pagination")
            except Exception as exc:
                logger.error("Failed to acquire anonymous Spotify token: %s", exc)
                return []

        if not token:
            logger.error("No Spotify token available; pagination aborted")
            return []

        # 3. Paginate remaining tracks via Web API
        extra_tracks: List[ResolveTrack] = []
        offset = start_offset
        track_idx = start_offset + 1
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
        }

        while offset < 5000:
            api_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?offset={offset}&limit=100"
            req = urllib.request.Request(api_url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=12) as resp:
                    page_data = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as http_err:
                if http_err.code == 429:
                    retry_after = int(http_err.headers.get("Retry-After", 2))
                    logger.warning("Spotify 429 hit; sleeping %ds...", retry_after)
                    time.sleep(retry_after)
                    continue
                logger.warning("Spotify API error %d at offset %d: %s", http_err.code, offset, http_err)
                break
            except Exception as exc:
                logger.warning("Failed to fetch Spotify tracks at offset %d: %s", offset, exc)
                break

            items = page_data.get("items", [])
            if not items:
                break

            for item in items:
                t = item.get("track") or item.get("item")
                if not t or not isinstance(t, dict):
                    continue

                t_id = t.get("id") or f"{playlist_id}_{track_idx}"
                t_title = unicodedata.normalize("NFKC", str(t.get("name") or f"Track {track_idx}")).strip()

                artists_list = t.get("artists") or []
                art_names = [a.get("name") for a in artists_list if isinstance(a, dict) and a.get("name")]
                t_artist = ", ".join(art_names) if art_names else "Unknown Artist"
                t_artist = unicodedata.normalize("NFKC", str(t_artist)).strip()

                t_dur = float(t.get("duration_ms") or 180000.0)
                disc_num = int(t.get("disc_number") or 1)

                alb_obj = t.get("album")
                if isinstance(alb_obj, dict) and alb_obj.get("name"):
                    t_album = unicodedata.normalize("NFKC", str(alb_obj.get("name"))).strip()
                else:
                    t_album = "Single"

                track_url = f"https://open.spotify.com/track/{t_id}" if t_id else None
                extra_tracks.append(
                    ResolveTrack(
                        id=f"t_{t_id}",
                        title=t_title,
                        artist=t_artist,
                        album=t_album,
                        disc_number=disc_num,
                        track_number=track_idx,
                        duration_ms=t_dur,
                        exists_locally=False,
                        local_path=None,
                        source_playlist_name=playlist_name,
                        url=track_url,
                    )
                )
                track_idx += 1

            if not page_data.get("next"):
                break
            offset += len(items)

        return extra_tracks


    async def _extract_artist_discography(
        self,
        artist_id: str,
        original_url: str,
    ) -> Optional[Tuple[str, str, List[ResolveTrack]]]:
        loop = asyncio.get_running_loop()

        def _fetch_artist_page() -> str:
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

        m_title = re.search(r'<meta\s+(?:property|name)="og:title"\s+content="([^"]+)"', page_html)
        if not m_title:
            m_title = re.search(r"<title>(.*?)(?: \| Spotify)?</title>", page_html)
        raw_artist_name = m_title.group(1).split(" | ")[0].strip() if m_title else "Artist"
        artist_name = unicodedata.normalize("NFKC", str(raw_artist_name)).strip()

        pos_appears_on = page_html.find("Appears On")
        artist_section = page_html[:pos_appears_on] if pos_appears_on != -1 else page_html

        album_ids = list(dict.fromkeys(re.findall(r"/album/([a-zA-Z0-9]{22})", artist_section)))
        if not album_ids:
            album_ids = list(dict.fromkeys(re.findall(r"/album/([a-zA-Z0-9]{22})", page_html)))
        if not album_ids:
            return None

        playlist_name = f"{artist_name} (Discography)"
        playlist_id = f"pl-{artist_id}"

        sem = asyncio.Semaphore(8)

        def _fetch_album_embed(aid: str) -> Tuple[Optional[str], List[dict]]:
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

                if not artists_match(t_artist, artist_name):
                    continue

                dedup_key = (t_title.lower(), t_artist.lower())
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                uri = item.get("uri", "")
                track_id = uri.split(":")[-1] if uri else f"t_{artist_id}_{track_idx}"
                duration = float(item.get("duration") or 180000.0)

                track_url = f"https://open.spotify.com/track/{track_id}" if track_id and not track_id.startswith("t_") else None
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
                        url=track_url,
                    )
                )
                track_idx += 1

        return playlist_name, playlist_id, tracks

    async def extract_tracks(
        self,
        url: str,
        artist_mode: str = "discography",
    ) -> Tuple[Optional[str], str, List[ResolveTrack]]:
        clean_match = re.search(r'(playlist|album|artist|track)[/:]([a-zA-Z0-9]+)', url)
        if not clean_match:
            return YtDlpMetadataExtractor._fallback_track(url)

        entity_type = clean_match.group(1).lower()
        spotify_id = clean_match.group(2)

        if entity_type == "artist" and artist_mode == "discography":
            discog_res = await self._extract_artist_discography(spotify_id, url)
            if discog_res and discog_res[2]:
                return discog_res

        loop = asyncio.get_running_loop()

        # Try SpotDL native playlist extraction first for complete, uncapped metadata
        if entity_type == "playlist":
            try:
                spotdl_res = await loop.run_in_executor(
                    None,
                    lambda: self._extract_playlist_via_spotdl(url, spotify_id),
                )
                if spotdl_res and spotdl_res[2]:
                    logger.info("Successfully extracted %d tracks from Spotify playlist '%s' via SpotDL", len(spotdl_res[2]), spotdl_res[0])
                    return spotdl_res
            except Exception as exc:
                logger.warning("SpotDL playlist extraction failed for '%s', falling back to embed parser: %s", url, exc)

        def _fetch_page(sid: str) -> str:
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
            return YtDlpMetadataExtractor._fallback_track(url)

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if not match:
            return YtDlpMetadataExtractor._fallback_track(url)

        try:
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
                        url=f"https://open.spotify.com/track/{spotify_id}",
                    )
                )
                return None, playlist_id, tracks

            raw_track_list = entity.get("trackList", [])
            for idx, item in enumerate(raw_track_list, start=1):
                t_title = unicodedata.normalize("NFKC", str(item.get("title") or f"Track {idx}")).strip()
                t_artist = unicodedata.normalize("NFKC", str(item.get("subtitle") or raw_title or "Unknown Artist")).strip()
                t_uri = item.get("uri") or f"t_{spotify_id}_{idx}"
                track_id = t_uri.split(":")[-1]
                t_dur = float(item.get("duration") or 180000.0)

                track_url = f"https://open.spotify.com/track/{track_id}" if track_id and not track_id.startswith("t_") else None
                tracks.append(
                    ResolveTrack(
                        id=f"t_{track_id}",
                        title=t_title,
                        artist=t_artist,
                        album=clean_title if entity_type == "album" else "Single",
                        disc_number=1,
                        track_number=idx,
                        duration_ms=t_dur,
                        exists_locally=False,
                        local_path=None,
                        source_playlist_name=clean_title if entity_type == "playlist" else None,
                        url=track_url,
                    )
                )

            if entity_type == "playlist" and len(raw_track_list) >= 100:
                logger.info("Spotify playlist '%s' has 100+ tracks; fetching remaining pages...", clean_title)
                try:
                    extra = await loop.run_in_executor(
                        None,
                        lambda: self._fetch_remaining_playlist_tracks(
                            playlist_id=matched_id,
                            playlist_name=clean_title,
                            start_offset=len(tracks),
                        ),
                    )
                    tracks.extend(extra)
                    logger.info("Fetched %d additional tracks (total: %d tracks)", len(extra), len(tracks))
                except Exception as exc:
                    logger.warning("Failed to paginate remaining playlist tracks: %s", exc)

            return clean_title, playlist_id, tracks

        except Exception as exc:
            logger.error("Failed to parse Spotify embed JSON for '%s': %s", url, exc)
            return YtDlpMetadataExtractor._fallback_track(url)


class CompositeMetadataExtractor:
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
    def __init__(self, tracks: Optional[List[ResolveTrack]] = None, playlist_name: str = "Mock Playlist"):
        self.tracks = tracks or []
        self.playlist_name = playlist_name

    async def extract_tracks(
        self,
        url: str,
        artist_mode: str = "discography",
    ) -> Tuple[str, str, List[ResolveTrack]]:
        pl_id = f"pl-mock-{uuid.uuid4().hex[:6]}"
        copied = [t.model_copy() for t in self.tracks]
        for c in copied:
            if not c.source_playlist_name:
                c.source_playlist_name = self.playlist_name
        return self.playlist_name, pl_id, copied


class Resolver:
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
