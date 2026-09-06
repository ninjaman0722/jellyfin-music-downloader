"""server/app/tagger.py - Mutagen Metadata & Cover Art Tagging Engine for Jellyfin Music Downloader V2.

Features:
- MP3 ID3v2.4 embedding:
    * TIT2 (Title), TPE1 (Artist), TALB (Album), TPE2 (Album Artist)
    * TRCK (Track/Total), TPOS (Disc/Total), TDRC (Date/Year), TCON (Genre)
    * USLT (Plain Lyrics), SYLT (Synchronized Lyrics in ms)
    * APIC (Cover Art with MIME detection, type 3 Front Cover)
- FLAC Vorbis Comments & Picture Block embedding:
    * TITLE, ARTIST, ALBUM, ALBUMARTIST, TRACKNUMBER, TRACKTOTAL, DISCNUMBER, DISCTOTAL, DATE, GENRE, LYRICS, UNSYNCEDLYRICS
    * mutagen.flac.Picture block (type 3 Front Cover)
- Strict UTF-8 text encoding across all scripts (Japanese, Korean, Cyrillic, Accented Latin).
- Automatic sidecar '{audio_path}.lrc' writing for Jellyfin / mobile clients.
- Automatic album '{album_dir}/cover.jpg' extraction for fast Jellyfin scanning.
- Duck-typed metadata coercion accepting TrackMetadata, dicts, or ResolveTrack models.
- Non-destructive operation: corrupt tags log warnings and never delete audio files.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    SYLT,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPOS,
    TPE1,
    TPE2,
    TRCK,
    USLT,
    ID3NoHeaderError,
)

logger = logging.getLogger("server.app.tagger")


# ==============================================================================
# 1. Data Models & Helper Functions
# ==============================================================================

@dataclass
class TrackMetadata:
    """Canonical audio metadata transfer object."""
    title: str
    artist: str
    album: str = ""
    album_artist: Optional[str] = None
    track_number: Optional[int] = 1
    total_tracks: Optional[int] = None
    disc_number: Optional[int] = 1
    total_discs: Optional[int] = None
    year: Optional[Union[int, str]] = None
    genre: Optional[str] = None
    lyrics: Optional[str] = None          # Plain lyrics text
    synced_lyrics: Optional[str] = None   # Raw .lrc synchronized text
    cover_bytes: Optional[bytes] = None
    cover_mime: Optional[str] = None


def detect_image_mime(data: bytes) -> str:
    """Detects image MIME type from initial binary magic bytes."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    elif data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def split_artists(artist_str: str) -> List[str]:
    """Splits composite artist strings into distinct artist names for multi-value tagging.
    Handles delimiters: '/', ';', ' feat. ', ' ft. ', ' with '.
    """
    if not artist_str:
        return []
    parts = re.split(r"\s*[/;]\s*|\s+(?:feat\.?|ft\.?|with)\s+", artist_str, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def strip_lrc_timestamps(synced: Optional[str]) -> str:
    """Strips LRC timestamps to create plain text lyrics."""
    if not synced:
        return ""
    lines: List[str] = []
    tag_pattern = re.compile(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]")
    for raw_line in synced.splitlines():
        if re.match(r"^\[[a-zA-Z]+:.*\]$", raw_line.strip()):
            continue
        clean = tag_pattern.sub("", raw_line).strip()
        if clean:
            lines.append(clean)
    return "\n".join(lines)


def parse_lrc_lines(synced: Optional[str]) -> List[Tuple[str, int]]:
    """Parses LRC into [(lyric_text, millisecond_timestamp), ...] for SYLT."""
    if not synced:
        return []
    entries: List[Tuple[str, int]] = []
    pattern = re.compile(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\](.*)")
    for line in synced.splitlines():
        m = pattern.match(line.strip())
        if m:
            minutes = int(m.group(1))
            seconds = float(m.group(2))
            ms = int((minutes * 60 + seconds) * 1000)
            text = m.group(3).strip()
            if text:
                entries.append((text, ms))
    return entries


def save_cover_file(album_dir: Path, cover_bytes: bytes, filename: str = "cover.jpg") -> Optional[Path]:
    """Writes cover.jpg in the album directory if not already present."""
    try:
        album_dir.mkdir(parents=True, exist_ok=True)
        target = album_dir / filename
        if not target.exists():
            target.write_bytes(cover_bytes)
            logger.debug("Saved album cover to %s", target)
            return target
    except Exception as exc:
        logger.warning("Failed to save album cover file in %s: %s", album_dir, exc)
    return None


def save_lrc_sidecar(audio_path: Path, synced_lrc: str) -> Optional[Path]:
    """Writes {audio_path}.lrc sidecar file in UTF-8."""
    try:
        target = audio_path.with_suffix(".lrc")
        target.write_text(synced_lrc, encoding="utf-8")
        logger.debug("Saved synchronized lyrics sidecar to %s", target)
        return target
    except Exception as exc:
        logger.warning("Failed to save .lrc sidecar for %s: %s", audio_path, exc)
    return None


def coerce_metadata(
    track: Any,
    lyrics: Optional[str] = None,
    cover_bytes: Optional[bytes] = None,
) -> TrackMetadata:
    """Normalizes dicts, dataclasses, or Pydantic models into TrackMetadata."""
    if isinstance(track, TrackMetadata):
        meta = track
    elif isinstance(track, dict):
        meta = TrackMetadata(
            title=track.get("title") or track.get("name") or "Unknown Title",
            artist=track.get("artist") or track.get("artist_name") or "Unknown Artist",
            album=track.get("album") or track.get("album_name") or "",
            album_artist=track.get("album_artist") or track.get("artist"),
            track_number=track.get("track_number", 1),
            total_tracks=track.get("total_tracks"),
            disc_number=track.get("disc_number", 1),
            total_discs=track.get("total_discs"),
            year=track.get("year") or track.get("release_date"),
            genre=track.get("genre"),
        )
    else:
        meta = TrackMetadata(
            title=getattr(track, "title", None) or getattr(track, "name", "Unknown Title"),
            artist=getattr(track, "artist", None) or getattr(track, "artist_name", "Unknown Artist"),
            album=getattr(track, "album", None) or getattr(track, "album_name", ""),
            album_artist=getattr(track, "album_artist", None) or getattr(track, "artist", None),
            track_number=getattr(track, "track_number", 1),
            total_tracks=getattr(track, "total_tracks", None),
            disc_number=getattr(track, "disc_number", 1),
            total_discs=getattr(track, "total_discs", None),
            year=getattr(track, "year", None) or getattr(track, "release_date", None),
            genre=getattr(track, "genre", None),
        )

    if lyrics is not None:
        if "[" in lyrics and "]" in lyrics:
            meta.synced_lyrics = lyrics
            meta.lyrics = strip_lrc_timestamps(lyrics)
        else:
            meta.lyrics = lyrics

    if cover_bytes is not None:
        meta.cover_bytes = cover_bytes

    return meta


# ==============================================================================
# 2. Audio Tagger Engine
# ==============================================================================

class AudioTagger:
    """Unified tagging engine for MP3 (ID3v2.4) and FLAC audio files."""

    @staticmethod
    def tag_mp3(
        file_path: Union[Path, str],
        meta: TrackMetadata,
        save_cover: bool = True,
        save_lrc: bool = True,
    ) -> None:
        """Tags an MP3 file with Mutagen ID3v2.4 tags and APIC cover art."""
        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(f"Audio file not found: {p}")

        try:
            tags = ID3(p)
        except ID3NoHeaderError:
            tags = ID3()

        # 1. Text metadata frames (Strict UTF-8 encoding=3)
        tags.setall("TIT2", [TIT2(encoding=3, text=meta.title)])
        artist_list = split_artists(meta.artist) or [meta.artist]
        tags.setall("TPE1", [TPE1(encoding=3, text=artist_list)])
        tags.setall("TALB", [TALB(encoding=3, text=meta.album)])
        album_artist = meta.album_artist or (artist_list[0] if artist_list else meta.artist)
        tags.setall("TPE2", [TPE2(encoding=3, text=album_artist)])

        trck_val = f"{meta.track_number}/{meta.total_tracks}" if meta.total_tracks else str(meta.track_number or 1)
        tags.setall("TRCK", [TRCK(encoding=3, text=trck_val)])

        tpos_val = f"{meta.disc_number}/{meta.total_discs}" if meta.total_discs else str(meta.disc_number or 1)
        tags.setall("TPOS", [TPOS(encoding=3, text=tpos_val)])

        if meta.year:
            tags.setall("TDRC", [TDRC(encoding=3, text=str(meta.year))])
        if meta.genre:
            tags.setall("TCON", [TCON(encoding=3, text=meta.genre)])

        # 2. Lyrics (USLT plain text and SYLT synchronized events)
        plain_lyrics = meta.lyrics or (strip_lrc_timestamps(meta.synced_lyrics) if meta.synced_lyrics else None)
        if plain_lyrics:
            tags.setall("USLT", [USLT(encoding=3, lang="eng", desc="", text=plain_lyrics)])

        if meta.synced_lyrics:
            sylt_entries = parse_lrc_lines(meta.synced_lyrics)
            if sylt_entries:
                tags.setall("SYLT", [SYLT(encoding=3, lang="eng", format=1, type=1, desc="", text=sylt_entries)])
            if save_lrc:
                save_lrc_sidecar(p, meta.synced_lyrics)

        # 3. Cover Art (APIC Frame, type 3 Front Cover)
        if meta.cover_bytes:
            mime = meta.cover_mime or detect_image_mime(meta.cover_bytes)
            tags.setall("APIC", [APIC(
                encoding=3,
                mime=mime,
                type=3,  # Front cover
                desc="Cover",
                data=meta.cover_bytes,
            )])
            if save_cover:
                save_cover_file(p.parent, meta.cover_bytes)

        # 4. Commit to disk using ID3v2.4 standard
        tags.save(p, v2_version=4)
        logger.info("Successfully tagged MP3 with ID3v2.4: %s", p)

    @staticmethod
    def tag_flac(
        file_path: Union[Path, str],
        meta: TrackMetadata,
        save_cover: bool = True,
        save_lrc: bool = True,
    ) -> None:
        """Tags a FLAC file with Vorbis comments and Picture block."""
        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(f"Audio file not found: {p}")

        audio = FLAC(p)

        # 1. Vorbis comments (UTF-8 by specification)
        audio["title"] = meta.title
        artist_list = split_artists(meta.artist) or [meta.artist]
        audio["artist"] = artist_list
        audio["album"] = meta.album
        audio["albumartist"] = meta.album_artist or (artist_list[0] if artist_list else meta.artist)
        audio["tracknumber"] = str(meta.track_number or 1)
        if meta.total_tracks:
            audio["tracktotal"] = str(meta.total_tracks)
        audio["discnumber"] = str(meta.disc_number or 1)
        if meta.total_discs:
            audio["disctotal"] = str(meta.total_discs)
        if meta.year:
            audio["date"] = str(meta.year)
        if meta.genre:
            audio["genre"] = meta.genre

        # 2. Lyrics
        plain_lyrics = meta.lyrics or (strip_lrc_timestamps(meta.synced_lyrics) if meta.synced_lyrics else None)
        if meta.synced_lyrics:
            audio["lyrics"] = meta.synced_lyrics
            if plain_lyrics:
                audio["unsyncedlyrics"] = plain_lyrics
            if save_lrc:
                save_lrc_sidecar(p, meta.synced_lyrics)
        elif plain_lyrics:
            audio["lyrics"] = plain_lyrics

        # 3. Cover Art (Picture Block)
        if meta.cover_bytes:
            mime = meta.cover_mime or detect_image_mime(meta.cover_bytes)
            pic = Picture()
            pic.type = 3  # Front cover
            pic.mime = mime
            pic.desc = "Front Cover"
            pic.data = meta.cover_bytes

            audio.clear_pictures()
            audio.add_picture(pic)

            if save_cover:
                save_cover_file(p.parent, meta.cover_bytes)

        audio.save()
        logger.info("Successfully tagged FLAC with Vorbis comments & Picture block: %s", p)

    @classmethod
    def embed_metadata(
        cls,
        file_path: Union[Path, str],
        track: Any,
        lyrics: Optional[str] = None,
        cover_bytes: Optional[bytes] = None,
        save_cover: bool = True,
        save_lrc: bool = True,
    ) -> None:
        """Universal entrypoint satisfying the PROJECT.md § 123 contract:
        embed_metadata(path, track, lyrics, cover_bytes).
        """
        p = Path(file_path)
        meta = coerce_metadata(track, lyrics=lyrics, cover_bytes=cover_bytes)
        ext = p.suffix.lower()

        if ext == ".mp3":
            cls.tag_mp3(p, meta, save_cover=save_cover, save_lrc=save_lrc)
        elif ext == ".flac":
            cls.tag_flac(p, meta, save_cover=save_cover, save_lrc=save_lrc)
        else:
            raise ValueError(f"Unsupported audio container format: '{ext}'. Supported: .mp3, .flac")
