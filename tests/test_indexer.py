"""Tests for Stage 1 Library Indexer and Unicode NFKC Diff Logic (M2 / Tier 2 & Tier 4).

Validates:
- Non-ASCII title preservation (Japanese, Korean, Cyrillic, Accented Latin) via NFKC normalization.
- Prevention of Unicode collapse where foreign-language titles collapsed to empty string in legacy code.
- Non-destructive handling of valid audio tracks under 350KB (interludes, skits, ringtones).
- Sub-20ms diff benchmark asserting that 200 tracks diff against 5,000+ indexed tracks in <20ms.
- Multi-artist collaboration matching and parenthetical title tolerance.
"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import mutagen
import pytest

from server.app.indexer import LibraryIndex, index_library, normalize_key


# -----------------------------------------------------------------------------
# Tier 2 Tests: Unicode NFKC Non-ASCII Title Preservation & Collapse Prevention
# -----------------------------------------------------------------------------

def legacy_flawed_clean_string(s: str) -> str:
    """The flawed regex logic from legacy server/patch_skip.py:23.
    Demonstrates the exact DAT-01 bug that collapsed non-ASCII characters to empty string.
    """
    return re.sub(r"[^a-z0-9]", "", s.lower())


def test_unicode_nfkc_preserves_non_ascii_scripts():
    """Tier 2: Verify that Japanese, Korean, Cyrillic, and accented Latin titles
    are preserved and never collapsed to empty strings.
    """
    test_cases = [
        ("RADWIMPS", "前前前世", "Japanese Kanji"),
        ("米津玄師", "Lemon", "Japanese Artist Kanji"),
        ("YOASOBI", "アイドル", "Japanese Katakana"),
        ("BTS", "봄날 (Spring Day)", "Korean Hangul"),
        ("BLACKPINK", "뚜두뚜두 (DDU-DU DDU-DU)", "Korean Hangul"),
        ("Кино", "Группа крови", "Russian Cyrillic"),
        ("Би-2", "Полковнику никто не пишет", "Russian Cyrillic"),
        ("Beyoncé", "Déjà Vu", "Accented Latin"),
        ("Sigur Rós", "Hoppípolla", "Icelandic Latin"),
        ("Ásgeir", "Dýrð í dauðaþögn", "Nordic Latin"),
    ]

    for artist, title, script_name in test_cases:
        normalized_title = normalize_key(title)
        normalized_artist = normalize_key(artist)

        # Invariant 1: Non-ASCII characters must NOT collapse to empty string
        assert normalized_title != "", f"Fatal: {script_name} title '{title}' collapsed to empty string!"
        assert normalized_artist != "", f"Fatal: {script_name} artist '{artist}' collapsed to empty string!"

        # Invariant 2: Compare against legacy bug to prove regression fix
        legacy_title_result = legacy_flawed_clean_string(title)
        if script_name in ("Japanese Kanji", "Japanese Katakana", "Russian Cyrillic"):
            assert legacy_title_result == "", (
                f"Expected legacy code to collapse '{title}', but got '{legacy_title_result}'"
            )
            # NFKC algorithm correctly retains the characters
            assert len(normalized_title) > 0, f"NFKC failed to preserve characters for '{title}'"


def test_nfkc_half_and_full_width_katakana_equivalence():
    """Tier 2: Verify NFKC normalizes full-width and half-width Japanese Katakana to identical keys."""
    full_width = "アイドル"  # Full-width Katakana (Aidoru)
    half_width = "ｱｲﾄﾞﾙ"    # Half-width Katakana (Aidoru)

    norm_full = normalize_key(full_width)
    norm_half = normalize_key(half_width)

    assert norm_full == norm_half, f"NFKC mismatch: full-width '{norm_full}' != half-width '{norm_half}'"
    assert norm_full == "アイドル"


def test_nfkc_whitespace_and_punctuation_handling():
    """Tier 2: Verify punctuation stripping and whitespace collapsing."""
    raw = "  Song    Name  (Feat.  Artist)  -  Remastered 2024!  "
    clean = normalize_key(raw)
    assert "  " not in clean, "Extra whitespace was not collapsed"
    assert "!" not in clean, "Punctuation was not stripped"
    assert "-" not in clean, "Hyphen was not stripped"


# -----------------------------------------------------------------------------
# Tier 2 Tests: Non-Destructive File Ingestion (<350KB Audio File Protection)
# -----------------------------------------------------------------------------

def test_valid_short_tracks_under_350kb_preserved(tmp_path: Path, synthetic_audio_factory):
    """Tier 2 / DAT-02: Verify audio files under 350KB (intros, skits, interludes)
    are indexed and NEVER unlinked/deleted from disk.
    """
    music_dir = tmp_path / "music_library"
    music_dir.mkdir(parents=True, exist_ok=True)

    # Generate audio files with sizes below the legacy 350,000-byte kill threshold
    files_to_test = [
        ("Album Intro", "Kendrick Lamar", 45_000),    # 45 KB
        ("Radio Skit", "Eminem", 120_000),             # 120 KB
        ("Interlude", "Frank Ocean", 250_000),         # 250 KB
        ("Transition", "Travis Scott", 345_000),       # 345 KB (< 350KB boundary)
        ("Standard Track", "The Weeknd", 500_000),     # 500 KB (> 350KB)
    ]

    created_paths = []
    for title, artist, target_size in files_to_test:
        fpath = synthetic_audio_factory(
            title=title,
            artist=artist,
            target_size_bytes=target_size,
            filename=f"test_library/{artist} - {title}.mp3",
        )
        assert fpath.exists(), f"Failed to generate {fpath}"
        assert fpath.stat().st_size >= target_size, "Generated file smaller than target"
        created_paths.append((fpath, title, artist, target_size))

    # Run the indexer on the directory containing <350KB files
    index = index_library(tmp_path / "test_library")

    # Invariant 1: ALL files must still exist on disk (zero unlinks!)
    for fpath, title, artist, target_size in created_paths:
        assert fpath.exists(), (
            f"DESTRUCTIVE DEFECT DETECTED: File '{fpath}' ({fpath.stat().st_size} bytes) "
            f"was unlinked/deleted by library indexer!"
        )

    # Invariant 2: Files under 350KB must be indexed successfully
    for fpath, title, artist, target_size in created_paths:
        match = index.find_match(title, artist)
        assert match is not None, f"Failed to find indexed track '{title}' by '{artist}' (<350KB)"
        assert match == fpath, f"Matched path mismatch: expected {fpath}, got {match}"


# -----------------------------------------------------------------------------
# Tier 3 & Tier 4 Tests: Library Index Lookup & Sub-20ms Diff Benchmark
# -----------------------------------------------------------------------------

def test_collaboration_and_multi_artist_matching(sample_library_dir: Path):
    """Tier 3: Verify matching tracks with multiple artists or collaborative tokens."""
    index = index_library(sample_library_dir)

    # 1. Multi-artist exact title and primary artist
    match = index.find_match("I'm Good (Blue)", "David Guetta")
    assert match is not None, "Failed to match multi-artist track by primary artist"

    # 2. Case-insensitive match
    match_lower = index.find_match("blinding lights", "the weeknd")
    assert match_lower is not None, "Case-insensitive title/artist matching failed"

    # 3. Japanese track lookup
    match_jp = index.find_match("前前前世", "RADWIMPS")
    assert match_jp is not None, "Failed to match Japanese track '前前前世'"


def test_sub_20ms_diff_benchmark():
    """Tier 4: Benchmark verifying that diffing 200 tracks against 5,000 in-memory
    indexed tracks completes in strictly under 20 milliseconds (Acceptance Criteria R2).
    """
    index = LibraryIndex()

    # Pre-populate index with 5,000 distinct tracks
    for i in range(5000):
        fake_path = Path(f"/music/Artist_{i % 200}/Album_{i % 50}/01 - Song_{i}.mp3")
        index.add_track(fake_path, f"Song {i}", f"Artist {i % 200}")

    # Construct test playlist of 200 tracks (193 existing in index, 7 missing)
    query_playlist = []
    # 193 existing
    for i in range(193):
        query_playlist.append({"title": f"Song {i}", "artist": f"Artist {i % 200}"})
    # 7 missing
    for i in range(7):
        query_playlist.append({"title": f"Missing New Track {i}", "artist": f"New Artist {i}"})

    assert len(query_playlist) == 200

    # Execute and time the diff operation
    start_time = time.perf_counter()

    existing_count = 0
    missing_count = 0
    for track in query_playlist:
        match = index.find_match(track["title"], track["artist"])
        if match:
            existing_count += 1
        else:
            missing_count += 1

    elapsed_ms = (time.perf_counter() - start_time) * 1000.0

    # Assertions
    assert existing_count == 193, f"Expected 193 existing tracks, got {existing_count}"
    assert missing_count == 7, f"Expected 7 missing tracks, got {missing_count}"

    # Strict performance threshold: Must be under 20.0ms per ORIGINAL_REQUEST.md line 83
    assert elapsed_ms < 20.0, f"Diff benchmark exceeded 20ms: took {elapsed_ms:.2f}ms for 200 tracks"
    # Document typical speed (usually < 5ms)
    print(f"\n[BENCHMARK] Diffed 200 tracks against 5,000 songs in {elapsed_ms:.3f}ms (Threshold: <20.0ms)")


def test_indexer_incremental_add_and_remove(tmp_path: Path):
    """Tier 2: Verify dynamic add_track and remove_track update total_indexed and lookup map."""
    index = LibraryIndex()
    track_path = tmp_path / "Artist" / "Album" / "01 - Track.mp3"

    index.add_track(track_path, "Dynamic Song", "Dynamic Artist")
    assert index.total_indexed == 1
    assert index.find_match("Dynamic Song", "Dynamic Artist") == track_path.resolve()

    removed = index.remove_track(track_path)
    assert removed is True
    assert index.total_indexed == 0
    assert index.find_match("Dynamic Song", "Dynamic Artist") is None


def test_indexer_zero_byte_and_corrupt_files_ignored(tmp_path: Path):
    """Tier 2: Verify 0-byte or unreadable corrupt files are skipped and NEVER unlinked."""
    empty_file = tmp_path / "empty.mp3"
    empty_file.write_bytes(b"")

    corrupt_file = tmp_path / "corrupt.mp3"
    corrupt_file.write_bytes(b"INVALID_MP3_NOT_AN_AUDIO_STREAM")

    index = index_library(tmp_path)
    assert index.total_indexed == 0

    # Ensure files were NOT unlinked/deleted
    assert empty_file.exists()
    assert corrupt_file.exists()


def test_untagged_multidisc_filename_regex_pattern():
    """Verify regex for untagged audio filenames correctly strips multi-disc prefixes
    (e.g. '01-01 - Title', '1-03 - Title') without prematurely matching only the first disc digit.
    """
    pattern = r"^(?:\d+-\d+|\d+)[\s\.\-_]+"

    test_cases = [
        # Multi-disc variations
        ("01-01 - Blinding Lights", "Blinding Lights"),
        ("1-03 - Comfortably Numb", "Comfortably Numb"),
        ("02-12 - Stairway to Heaven", "Stairway to Heaven"),
        ("01-05_Song_Title", "Song_Title"),
        ("2-01. Bohemian Rhapsody", "Bohemian Rhapsody"),
        ("01-01 - 夜に駆ける", "夜に駆ける"),
        ("1-01 - 봄날", "봄날"),
        # Single track variations
        ("01 - Starboy", "Starboy"),
        ("05. Yesterday", "Yesterday"),
        ("12_Song_Title", "Song_Title"),
        ("1 - Solo", "Solo"),
        # Fallback invariant: pure number stems preserve number
        ("1984", "1984"),
    ]

    for stem, expected in test_cases:
        cleaned = re.sub(pattern, "", stem).strip()
        if not cleaned:
            cleaned = stem
        assert cleaned == expected, f"Failed for stem '{stem}': expected '{expected}', got '{cleaned}'"


def test_indexer_untagged_multidisc_file_scanning(tmp_path: Path):
    """Verify LibraryIndex extracts correct title and artist from untagged multi-disc audio files."""
    music_dir = tmp_path / "music"
    album_dir = music_dir / "Pink Floyd" / "The Wall"
    album_dir.mkdir(parents=True, exist_ok=True)

    # Create raw audio file without ID3 metadata tags
    fpath = album_dir / "02-06 - Comfortably Numb.mp3"
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    fpath.write_bytes(frame * 10)

    index = index_library(music_dir)
    assert index.total_indexed == 1

    # Invariant 1: Must match title 'Comfortably Numb' by primary artist 'Pink Floyd'
    match = index.find_match("Comfortably Numb", "Pink Floyd")
    assert match is not None, "Failed to match untagged multi-disc track by title"
    assert match == fpath.resolve()

    # Invariant 2: Track prefix '06 - Comfortably Numb' must NOT be in the index
    assert index.find_match("06 - Comfortably Numb", "Pink Floyd") is None, (
        "Track title was improperly indexed with track number prefix remaining!"
    )

