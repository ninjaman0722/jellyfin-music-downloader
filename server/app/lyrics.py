"""server/app/lyrics.py - Async LRCLIB Lyrics Client for Jellyfin Music Downloader V2.

Features:
- Connection pooling with httpx.Limits(max_connections=10, max_keepalive_connections=5).
- Exponential backoff retries for transient 429 and 5xx errors.
- Primary lookup via /api/get, falling back to /api/search.
- Strict +-3.0s audio duration verification:
    * If abs(lrc_duration - audio_duration) <= 3.0s: accept synced lyrics.
    * If abs(lrc_duration - audio_duration) > 3.0s: reject synced lyrics, fall back to plain lyrics.
- Helpers for LRC timestamp stripping and SYLT line parsing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("server.app.lyrics")


# ==============================================================================
# 1. Data Models & Helper Functions
# ==============================================================================

@dataclass
class LyricsResult:
    """Structured result returned by the LRCLIB client."""
    synced_lyrics: Optional[str] = None
    plain_lyrics: Optional[str] = None
    duration: Optional[float] = None
    duration_diff: Optional[float] = None
    is_synced: bool = False
    source: str = "lrclib"
    track_name: Optional[str] = None
    artist_name: Optional[str] = None
    album_name: Optional[str] = None

    def has_lyrics(self) -> bool:
        """Returns True if either synced or plain lyrics are present."""
        return bool(self.synced_lyrics or self.plain_lyrics)

    def best_lyrics(self) -> Optional[str]:
        """Returns synced lyrics if available, otherwise plain lyrics."""
        return self.synced_lyrics or self.plain_lyrics


def strip_lrc_timestamps(synced_lrc: Optional[str]) -> str:
    """Strips [mm:ss.xx] timestamp tags to derive clean plain text lyrics."""
    if not synced_lrc:
        return ""
    lines: List[str] = []
    # Pattern matching [mm:ss], [mm:ss.xx], [mm:ss.xxx]
    tag_pattern = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
    for raw_line in synced_lrc.splitlines():
        # Ignore ID tags like [ti:Title], [ar:Artist]
        if re.match(r"^\[[a-zA-Z]+:.*\]$", raw_line.strip()):
            continue
        clean_line = tag_pattern.sub("", raw_line).strip()
        if clean_line:
            lines.append(clean_line)
    return "\n".join(lines)


def parse_lrc_lines(synced_lrc: Optional[str]) -> List[Tuple[str, int]]:
    """Parses synced LRC text into [(lyric_text, millisecond_timestamp), ...]
    for ID3v2.4 SYLT frame embedding.
    """
    if not synced_lrc:
        return []
    entries: List[Tuple[str, int]] = []
    pattern = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\](.*)")
    for line in synced_lrc.splitlines():
        m = pattern.match(line.strip())
        if m:
            minutes = int(m.group(1))
            seconds = float(m.group(2))
            ms = int((minutes * 60 + seconds) * 1000)
            text = m.group(3).strip()
            if text:
                entries.append((text, ms))
    return entries


def clean_query_title(title: str) -> str:
    """Removes common track suffixes like (feat. ...), [Remastered] for fallback searches."""
    s = re.sub(r"\s*[\(\[](?:feat|ft|featuring)\.?\s+[^\)\]]+[\)\]]", "", title, flags=re.IGNORECASE)
    s = re.sub(r"\s*[\(\[](?:remastered|remaster|radio edit|deluxe|bonus track|live)[^\)\]]*[\)\]]", "", s, flags=re.IGNORECASE)
    return s.strip()


# ==============================================================================
# 2. LRCLIB Async Client
# ==============================================================================

class LRCLIBClient:
    """High-performance asynchronous client for LRCLIB."""

    DEFAULT_BASE_URL: str = "https://lrclib.net"
    DEFAULT_USER_AGENT: str = "JellyfinMusicDownloader/2.0 (https://github.com/omarchy/jellyfin-music-app)"
    MAX_DURATION_DIFF_SECONDS: float = 3.0

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        max_connections: int = 10,
        max_keepalive_connections: int = 5,
        max_retries: int = 3,
        retry_backoff: float = 0.25,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._custom_client = client
        self._client: Optional[httpx.AsyncClient] = client
        self.limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        )

    def _get_client(self) -> httpx.AsyncClient:
        """Retrieves or instantiates the managed httpx.AsyncClient."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                limits=self.limits,
                timeout=httpx.Timeout(self.timeout, connect=5.0),
                headers={"User-Agent": self.DEFAULT_USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        """Closes the underlying HTTP client if internally managed."""
        if self._client and not self._client.is_closed and self._client != self._custom_client:
            await self._client.aclose()

    async def __aenter__(self) -> "LRCLIBClient":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.aclose()

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        params: Dict[str, Any],
    ) -> Optional[httpx.Response]:
        """Executes an HTTP request with exponential backoff on transient errors."""
        client = self._get_client()
        for attempt in range(self.max_retries + 1):
            try:
                resp = await client.request(method, path, params=params)
                # Success or authoritative Not Found
                if resp.status_code in (200, 404):
                    return resp
                # Rate limit (429) or transient server errors (5xx)
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        delay = self.retry_backoff * (2 ** attempt)
                        logger.warning(
                            "LRCLIB transient error HTTP %d on %s. Retrying in %.2fs (attempt %d/%d)...",
                            resp.status_code, path, delay, attempt + 1, self.max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < self.max_retries:
                    delay = self.retry_backoff * (2 ** attempt)
                    logger.warning(
                        "LRCLIB network error (%s) on %s. Retrying in %.2fs (attempt %d/%d)...",
                        exc, path, delay, attempt + 1, self.max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error("LRCLIB request failed after %d retries: %s", self.max_retries, exc)
                return None
        return None

    def verify_and_build(
        self,
        raw_data: Dict[str, Any],
        audio_duration: Optional[float] = None,
    ) -> LyricsResult:
        """Applies strict duration verification and constructs a LyricsResult."""
        lrc_dur = raw_data.get("duration")
        raw_synced = raw_data.get("syncedLyrics")
        raw_plain = raw_data.get("plainLyrics")
        track_name = raw_data.get("name") or raw_data.get("trackName")
        artist_name = raw_data.get("artistName")
        album_name = raw_data.get("albumName")

        # Duration verification check
        if audio_duration is not None and audio_duration > 0 and lrc_dur is not None:
            diff = abs(float(lrc_dur) - float(audio_duration))
            if diff <= self.MAX_DURATION_DIFF_SECONDS:
                # Within tolerance: accept synced lyrics
                return LyricsResult(
                    synced_lyrics=raw_synced,
                    plain_lyrics=raw_plain or (strip_lrc_timestamps(raw_synced) if raw_synced else None),
                    duration=float(lrc_dur),
                    duration_diff=diff,
                    is_synced=bool(raw_synced),
                    track_name=track_name,
                    artist_name=artist_name,
                    album_name=album_name,
                )
            else:
                # Exceeds tolerance: REJECT synced lyrics, preserve plain text
                plain = raw_plain or (strip_lrc_timestamps(raw_synced) if raw_synced else None)
                logger.info(
                    "Rejected synced lyrics for '%s' (diff=%.2fs > %.1fs limit). Using plain fallback.",
                    track_name, diff, self.MAX_DURATION_DIFF_SECONDS,
                )
                return LyricsResult(
                    synced_lyrics=None,
                    plain_lyrics=plain,
                    duration=float(lrc_dur),
                    duration_diff=diff,
                    is_synced=False,
                    track_name=track_name,
                    artist_name=artist_name,
                    album_name=album_name,
                )

        # No audio duration provided: accept synced if present
        return LyricsResult(
            synced_lyrics=raw_synced,
            plain_lyrics=raw_plain or (strip_lrc_timestamps(raw_synced) if raw_synced else None),
            duration=float(lrc_dur) if lrc_dur is not None else None,
            duration_diff=None,
            is_synced=bool(raw_synced),
            track_name=track_name,
            artist_name=artist_name,
            album_name=album_name,
        )

    async def fetch_lyrics(
        self,
        artist: str,
        title: str,
        album: Optional[str] = None,
        audio_duration: Optional[float] = None,
    ) -> LyricsResult:
        """Fetches and verifies lyrics for a track, executing fallbacks if necessary."""
        # 1. Primary query: GET /api/get
        params: Dict[str, Any] = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if audio_duration and audio_duration > 0:
            params["duration"] = int(round(audio_duration))

        resp = await self._request_with_retry("GET", "/api/get", params)
        if resp and resp.status_code == 200:
            return self.verify_and_build(resp.json(), audio_duration)

        # 2. Fallback 1: Query without album name (album metadata frequently differs)
        if album and resp and resp.status_code == 404:
            params_no_alb = {"artist_name": artist, "track_name": title}
            if audio_duration and audio_duration > 0:
                params_no_alb["duration"] = int(round(audio_duration))
            resp_no_alb = await self._request_with_retry("GET", "/api/get", params_no_alb)
            if resp_no_alb and resp_no_alb.status_code == 200:
                return self.verify_and_build(resp_no_alb.json(), audio_duration)

        # 3. Fallback 2: GET /api/search
        search_params: Dict[str, Any] = {"artist_name": artist, "track_name": title}
        s_resp = await self._request_with_retry("GET", "/api/search", search_params)
        if s_resp and s_resp.status_code == 200:
            candidates = s_resp.json()
            if isinstance(candidates, list) and candidates:
                best_cand = None
                best_diff = float("inf")
                # Prioritize candidates with synced lyrics and minimal duration difference
                for c in candidates:
                    c_dur = c.get("duration")
                    if audio_duration and c_dur:
                        diff = abs(float(c_dur) - float(audio_duration))
                        if c.get("syncedLyrics") and diff <= self.MAX_DURATION_DIFF_SECONDS:
                            if diff < best_diff:
                                best_diff = diff
                                best_cand = c
                if not best_cand:
                    # Fallback to candidate with plain lyrics
                    for c in candidates:
                        if c.get("plainLyrics") or c.get("syncedLyrics"):
                            best_cand = c
                            break
                if not best_cand:
                    best_cand = candidates[0]
                return self.verify_and_build(best_cand, audio_duration)

        # 4. Fallback 3: Search with cleaned title if title contains features/remasters
        clean_title = clean_query_title(title)
        if clean_title != title:
            clean_params = {"artist_name": artist, "track_name": clean_title}
            c_resp = await self._request_with_retry("GET", "/api/search", clean_params)
            if c_resp and c_resp.status_code == 200:
                candidates = c_resp.json()
                if isinstance(candidates, list) and candidates:
                    return self.verify_and_build(candidates[0], audio_duration)

        # No lyrics found
        return LyricsResult(track_name=title, artist_name=artist, album_name=album)
