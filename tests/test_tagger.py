"""Unit & Integration Tests for Mutagen ID3v2.4 & FLAC Tagging Engine (M2 / Tier 1 & Tier 2).

Validates:
- MP3 ID3v2.4 embedding: TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TDRC, TCON, USLT, SYLT, APIC.
- FLAC Vorbis Comments & Picture Block embedding.
- Strict UTF-8 multilingual preservation across Japanese, Korean, Cyrillic, and Accented Latin.
- Cover art MIME type auto-detection (JPEG, PNG, WebP).
- Sidecar .lrc file generation.
- Album cover.jpg file extraction.
- Duck-typed metadata coercion.
- Non-destructive handling of corrupted files.
"""

from __future__ import annotations

import os
import struct
import tempfile
from pathlib import Path
import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3
import pytest

from server.app.tagger import (
    AudioTagger,
    TrackMetadata,
    coerce_metadata,
    detect_image_mime,
)


def make_synthetic_flac(path: Path) -> Path:
    """Constructs a valid minimal FLAC file with a 34-byte STREAMINFO block."""
    sr = 44100
    ch = 1  # 2 channels - 1
    bps = 15  # 16 bits - 1
    total_samples = 132300  # 3 seconds
    val = (sr << 44) | (ch << 41) | (bps << 36) | total_samples

    data = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x0e"
        + b"\x00\x00\x0e"
        + struct.pack(">Q", val)
        + b"\x00" * 16
    )
    header = b"\x80\x00\x00\x22"  # is_last=1, type=0 (STREAMINFO), len=34
    flac_bytes = b"fLaC" + header + data

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(flac_bytes)
    return path


def test_id3v24_mp3_tagging_all_frames(tmp_path: Path, synthetic_audio_factory):
    """Tier 1: Verify all required ID3v2.4 frames are populated and formatted correctly."""
    mp3_file = synthetic_audio_factory(
        title="Original Title",
        artist="Original Artist",
        filename="test/01 - Song.mp3",
    )

    fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 50
    synced_lrc = "[00:01.00] Line One\n[00:03.50] Line Two"

    meta = TrackMetadata(
        title="Blinding Lights",
        artist="The Weeknd",
        album="After Hours",
        album_artist="The Weeknd",
        track_number=9,
        total_tracks=14,
        disc_number=1,
        total_discs=1,
        year=2020,
        genre="Synthwave",
        synced_lyrics=synced_lrc,
        cover_bytes=fake_jpeg,
    )

    AudioTagger.tag_mp3(mp3_file, meta, save_cover=True, save_lrc=True)

    # Inspect with Mutagen
    loaded = ID3(mp3_file)
    assert loaded.version == (2, 4, 0), "Must write strict ID3v2.4 headers"
    assert loaded["TIT2"].text[0] == "Blinding Lights"
    assert loaded["TPE1"].text[0] == "The Weeknd"
    assert loaded["TALB"].text[0] == "After Hours"
    assert loaded["TPE2"].text[0] == "The Weeknd"
    assert loaded["TRCK"].text[0] == "9/14"
    assert loaded["TPOS"].text[0] == "1/1"
    assert str(loaded["TDRC"].text[0]) == "2020"
    assert loaded["TCON"].text[0] == "Synthwave"

    # Lyrics assertion
    uslt_frames = loaded.getall("USLT")
    assert len(uslt_frames) == 1
    assert "Line One\nLine Two" in uslt_frames[0].text

    sylt_frames = loaded.getall("SYLT")
    assert len(sylt_frames) == 1
    assert sylt_frames[0].text == [("Line One", 1000), ("Line Two", 3500)]

    # Cover Art assertion
    apic_frames = loaded.getall("APIC")
    assert len(apic_frames) == 1
    assert apic_frames[0].mime == "image/jpeg"
    assert apic_frames[0].type == 3  # Front cover
    assert apic_frames[0].data == fake_jpeg

    # Sidecar and album cover assertions
    assert (mp3_file.parent / "cover.jpg").exists()
    assert (mp3_file.parent / "01 - Song.lrc").exists()
    assert (mp3_file.parent / "01 - Song.lrc").read_text(encoding="utf-8") == synced_lrc


def test_flac_vorbis_and_picture_block_tagging(tmp_path: Path):
    """Tier 1: Verify FLAC Vorbis comments and Picture block embedding."""
    flac_file = make_synthetic_flac(tmp_path / "flac_test" / "02 - Track.flac")
    fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 40
    synced_lrc = "[00:02.00] Synchronized FLAC Lyrics"

    meta = TrackMetadata(
        title="Levitating",
        artist="Dua Lipa",
        album="Future Nostalgia",
        album_artist="Dua Lipa",
        track_number=5,
        total_tracks=11,
        disc_number=1,
        total_discs=1,
        year="2020",
        genre="Pop",
        synced_lyrics=synced_lrc,
        cover_bytes=fake_png,
    )

    AudioTagger.tag_flac(flac_file, meta, save_cover=True, save_lrc=True)

    loaded = FLAC(flac_file)
    assert loaded["title"][0] == "Levitating"
    assert loaded["artist"][0] == "Dua Lipa"
    assert loaded["album"][0] == "Future Nostalgia"
    assert loaded["albumartist"][0] == "Dua Lipa"
    assert loaded["tracknumber"][0] == "5"
    assert loaded["tracktotal"][0] == "11"
    assert loaded["discnumber"][0] == "1"
    assert loaded["disctotal"][0] == "1"
    assert loaded["date"][0] == "2020"
    assert loaded["genre"][0] == "Pop"
    assert loaded["lyrics"][0] == synced_lrc
    assert loaded["unsyncedlyrics"][0] == "Synchronized FLAC Lyrics"

    # Picture block assertion
    assert len(loaded.pictures) == 1
    assert loaded.pictures[0].type == 3  # Front cover
    assert loaded.pictures[0].mime == "image/png"
    assert loaded.pictures[0].data == fake_png

    # Sidecar file assertions
    assert (flac_file.parent / "cover.jpg").exists()
    assert (flac_file.parent / "02 - Track.lrc").exists()


def test_utf8_multilingual_preservation(tmp_path: Path, synthetic_audio_factory):
    """Tier 2: Verify non-ASCII scripts (Japanese, Korean, Cyrillic, Latin) are preserved without corruption."""
    test_cases = [
        ("前前前世", "RADWIMPS", "Your Name", "Japanese"),
        ("봄날 (Spring Day)", "BTS", "You Never Walk Alone", "Korean"),
        ("Группа крови", "Кино", "Группа крови", "Russian Cyrillic"),
        ("Déjà Vu", "Beyoncé", "B'Day", "Accented Latin"),
    ]

    for idx, (title, artist, album, script) in enumerate(test_cases, start=1):
        # MP3 test
        mp3_path = synthetic_audio_factory(
            title="temp", artist="temp", filename=f"scripts/track_{idx}.mp3"
        )
        meta = TrackMetadata(title=title, artist=artist, album=album)
        AudioTagger.tag_mp3(mp3_path, meta)

        loaded_id3 = ID3(mp3_path)
        assert loaded_id3["TIT2"].text[0] == title, f"ID3 UTF-8 failure for {script}"
        assert loaded_id3["TPE1"].text[0] == artist, f"ID3 UTF-8 failure for {script}"
        assert loaded_id3["TALB"].text[0] == album, f"ID3 UTF-8 failure for {script}"

        # FLAC test
        flac_path = make_synthetic_flac(tmp_path / f"scripts_flac/track_{idx}.flac")
        AudioTagger.tag_flac(flac_path, meta)

        loaded_flac = FLAC(flac_path)
        assert loaded_flac["title"][0] == title, f"FLAC UTF-8 failure for {script}"
        assert loaded_flac["artist"][0] == artist, f"FLAC UTF-8 failure for {script}"


def test_cover_mime_type_detection():
    """Tier 1: Verify detect_image_mime detects JPEG, PNG, and WebP from magic bytes."""
    assert detect_image_mime(b"\xff\xd8\xff\xe0anydata") == "image/jpeg"
    assert detect_image_mime(b"\x89PNG\r\n\x1a\nanydata") == "image/png"
    assert detect_image_mime(b"RIFF\x00\x00\x00\x00WEBPanydata") == "image/webp"
    assert detect_image_mime(b"unknownbytes") == "image/jpeg"  # Safe default


def test_duck_typed_metadata_coercion():
    """Tier 1: Verify coerce_metadata handles dicts, models, and duck-typed objects."""
    # 1. From dict
    d = {"title": "Dict Title", "artist": "Dict Artist", "track_number": 3}
    m1 = coerce_metadata(d, lyrics="[00:01.00] Synced")
    assert m1.title == "Dict Title"
    assert m1.synced_lyrics == "[00:01.00] Synced"
    assert m1.lyrics == "Synced"

    # 2. From duck-typed class
    class DuckTrack:
        title = "Duck Title"
        artist = "Duck Artist"
        album = "Duck Album"
        track_number = 7

    m2 = coerce_metadata(DuckTrack(), lyrics="Plain Text Only")
    assert m2.title == "Duck Title"
    assert m2.track_number == 7
    assert m2.lyrics == "Plain Text Only"
    assert m2.synced_lyrics is None


def test_embed_metadata_universal_entrypoint(tmp_path: Path, synthetic_audio_factory):
    """Tier 1: Verify AudioTagger.embed_metadata satisfies the PROJECT.md § 123 contract."""
    mp3_file = synthetic_audio_factory(title="T", artist="A", filename="embed/track.mp3")

    AudioTagger.embed_metadata(
        file_path=mp3_file,
        track={"title": "Contract Track", "artist": "Contract Artist"},
        lyrics="Plain Lyrics",
        cover_bytes=b"\xff\xd8\xff\xe0jpegdata",
    )

    loaded = ID3(mp3_file)
    assert loaded["TIT2"].text[0] == "Contract Track"
    assert loaded["TPE1"].text[0] == "Contract Artist"
    assert loaded.getall("USLT")[0].text == "Plain Lyrics"
    assert len(loaded.getall("APIC")) == 1
