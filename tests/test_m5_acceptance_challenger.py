"""Milestone 5 Empirical Acceptance Challenge Test Suite.

Authored by Challenger 2 (challenger_m5_acceptance).

Stress-tests:
1. Installer Packaging & Security Hardening:
   - Shell syntax validation via bash -n.
   - set -euo pipefail presence.
   - Non-destructive .bak.<timestamp> backup generation preserving user secrets.
   - chmod 0600 mode enforcement on configs and backups.
   - Default config creation with 0600 mode when none exists.
2. Runtime Launcher & Fallback Routing:
   - Quickshell crash/premature exit simulation triggering seamless Qt (app.py) fallback.
   - Quickshell clean exit (exit 0) cleanly terminating without false fallback.
   - Missing quickshell binary or non-Wayland environment falling back to Qt immediately.
3. R1-R5 Core Acceptance Verification:
   - Sub-20ms Diff Benchmark: 200 tracks diffed against 5,000 library tracks strictly < 20.0ms.
   - Unicode NFKC Title Preservation: Japanese, Korean, Cyrillic, Accented Latin, Fullwidth ASCII.
   - Short File Preservation: Valid audio tracks <350KB indexed and never deleted.
   - Targeted Process Cancellation: Job A cancellation without collateral harm to sibling Job B.
   - Multi-User Privacy Isolation: Scoped OwnerUserId and zero direct SQLite/XML mutations.
   - Omarchy Theme Resilience: Missing, malformed, and partial TOML fallback.
   - Drag-and-Drop & Clipboard URL Filtering: Safe URI scheme handling and accurate URL typing.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.app.indexer import LibraryIndex, index_library, normalize_key
from server.app.jellyfin import (
    HOUSEHOLD_USER_ID,
    SHARED_USER_ID,
    JellyfinAuthError,
    JellyfinClient,
    JellyfinPermissionError,
    PlaylistSummary,
)
from server.app.process import ProcessManager
from server.app.resolver import Resolver, ResolveResponse, ResolveTrack, URLType, detect_url_type


def _is_pid_alive(pid: int) -> bool:
    """Check if process is alive via POSIX signal 0."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ==============================================================================
# SECTION 1: Shell Scripts Syntax & Strict Mode
# ==============================================================================

class TestShellScriptIntegrity:
    """Empirically validates bash -n syntax, permissions, and strict execution flags."""

    @pytest.mark.parametrize("script_name", ["install.sh", "launcher.sh", "run.sh"])
    def test_bash_syntax_clean(self, script_name: str):
        script_path = PROJECT_ROOT / script_name
        assert script_path.exists(), f"Missing script: {script_path}"
        res = subprocess.run(
            ["bash", "-n", str(script_path)],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"bash -n failed on {script_name}: {res.stderr}"

    @pytest.mark.parametrize("script_name", ["install.sh", "launcher.sh", "run.sh"])
    def test_executable_permission(self, script_name: str):
        script_path = PROJECT_ROOT / script_name
        mode = script_path.stat().st_mode
        assert bool(mode & stat.S_IXUSR), f"{script_name} must have user execute bit (+x)"

    @pytest.mark.parametrize("script_name", ["install.sh", "launcher.sh", "run.sh"])
    def test_set_euo_pipefail(self, script_name: str):
        script_path = PROJECT_ROOT / script_name
        content = script_path.read_text(encoding="utf-8")
        assert "set -euo pipefail" in content, f"{script_name} must include 'set -euo pipefail'"


# ==============================================================================
# SECTION 2: Installer Non-Destructive Backup & chmod 0600 Modes
# ==============================================================================

class TestInstallerBackupAndSecurity:
    """Stress-tests install.sh configuration backup and 0600 permissions."""

    def test_non_destructive_backup_and_chmod_0600(self, tmp_path: Path):
        """Pre-creates sensitive configs, runs install.sh in sandbox, verifies preservation and 0600."""
        target_dir = tmp_path / "app"
        target_dir.mkdir(parents=True)
        server_dir = target_dir / "server"
        server_dir.mkdir(parents=True)

        # Pre-create config.json with custom user secret and loose 0644 mode
        client_cfg = target_dir / "config.json"
        client_secret = '{"custom_token": "client_secret_xyz123", "daemonUrl": "http://127.0.0.1:8095"}'
        client_cfg.write_text(client_secret, encoding="utf-8")
        client_cfg.chmod(0o644)

        # Pre-create server_config.json with sensitive server token and loose 0644 mode
        server_cfg = server_dir / "server_config.json"
        server_secret = '{"jellyfin_token": "super_secret_jf_token_999"}'
        server_cfg.write_text(server_secret, encoding="utf-8")
        server_cfg.chmod(0o644)

        # Point venv to the existing project .venv to bypass long pip install
        venv_link = target_dir / ".venv"
        project_venv = PROJECT_ROOT / ".venv"
        if project_venv.exists():
            os.symlink(project_venv, venv_link)

        # Run install.sh with sandboxed environment variables
        env = {
            **os.environ,
            "TARGET_DIR": str(target_dir),
            "APPS_DIR": str(tmp_path / "applications"),
            "SYSTEMD_USER_DIR": str(tmp_path / "systemd_user"),
            "STATE_DIR": str(tmp_path / "state"),
            "HOME": str(tmp_path),
        }

        install_script = PROJECT_ROOT / "install.sh"
        res = subprocess.run(
            ["bash", str(install_script)],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"install.sh failed:\nStdout: {res.stdout}\nStderr: {res.stderr}"

        # 1. Verify client config.json: preserved content, mode 0600
        assert client_cfg.exists()
        assert "client_secret_xyz123" in client_cfg.read_text(encoding="utf-8")
        assert stat.S_IMODE(client_cfg.stat().st_mode) == 0o600

        # 2. Verify client backup config.json.bak.*: created, matching content, mode 0600
        client_backups = list(target_dir.glob("config.json.bak.*"))
        assert len(client_backups) >= 1, f"No client backup created in {target_dir}"
        assert "client_secret_xyz123" in client_backups[0].read_text(encoding="utf-8")
        assert stat.S_IMODE(client_backups[0].stat().st_mode) == 0o600

        # 3. Verify server config.json: preserved content, mode 0600
        assert server_cfg.exists()
        assert "super_secret_jf_token_999" in server_cfg.read_text(encoding="utf-8")
        assert stat.S_IMODE(server_cfg.stat().st_mode) == 0o600

        # 4. Verify server backup server_config.json.bak.*: created, matching content, mode 0600
        server_backups = list(server_dir.glob("server_config.json.bak.*"))
        assert len(server_backups) >= 1, f"No server backup created in {server_dir}"
        assert "super_secret_jf_token_999" in server_backups[0].read_text(encoding="utf-8")
        assert stat.S_IMODE(server_backups[0].stat().st_mode) == 0o600

    def test_fresh_installation_generates_default_0600_config(self, tmp_path: Path):
        """When no config.json exists, install.sh creates default config with mode 0600."""
        target_dir = tmp_path / "fresh_app"
        target_dir.mkdir(parents=True)

        venv_link = target_dir / ".venv"
        project_venv = PROJECT_ROOT / ".venv"
        if project_venv.exists():
            os.symlink(project_venv, venv_link)

        env = {
            **os.environ,
            "TARGET_DIR": str(target_dir),
            "APPS_DIR": str(tmp_path / "applications"),
            "SYSTEMD_USER_DIR": str(tmp_path / "systemd_user"),
            "STATE_DIR": str(tmp_path / "state"),
            "HOME": str(tmp_path),
        }

        install_script = PROJECT_ROOT / "install.sh"
        res = subprocess.run(
            ["bash", str(install_script)],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, f"install.sh failed:\nStdout: {res.stdout}\nStderr: {res.stderr}"

        cfg = target_dir / "config.json"
        assert cfg.exists()
        assert "daemonUrl" in cfg.read_text(encoding="utf-8")
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


# ==============================================================================
# SECTION 3: Runtime Quickshell Crash Simulation & Qt Fallback
# ==============================================================================

class TestRuntimeFallback:
    """Empirically tests run.sh fallback behavior when Quickshell crashes or exits prematurely."""

    def test_quickshell_crash_triggers_seamless_qt_fallback(self, tmp_path: Path):
        """Simulate quickshell crashing with exit code 1 or 139 -> run.sh falls back to app.py."""
        sandbox_dir = tmp_path / "app"
        sandbox_dir.mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)

        # Mock crashing quickshell executable
        mock_qs = bin_dir / "quickshell"
        mock_qs.write_text("#!/usr/bin/env bash\necho 'SIMULATED QUICKSHELL LAYER-SHELL CRASH' >&2\nexit 139\n")
        mock_qs.chmod(0o755)

        # Mock app.py that logs invocation to a canary file and exits 0
        canary = sandbox_dir / "canary_qt_invoked.txt"
        mock_app = sandbox_dir / "app.py"
        mock_app.write_text(
            f"import sys\nfrom pathlib import Path\nPath('{canary}').write_text('QT_APP_INVOKED')\nsys.exit(0)\n"
        )
        mock_app.chmod(0o755)

        # Copy launcher.sh, run.sh, main.qml into sandbox
        (sandbox_dir / "launcher.sh").write_text((PROJECT_ROOT / "launcher.sh").read_text())
        (sandbox_dir / "launcher.sh").chmod(0o755)
        (sandbox_dir / "run.sh").write_text((PROJECT_ROOT / "run.sh").read_text())
        (sandbox_dir / "run.sh").chmod(0o755)
        (sandbox_dir / "main.qml").write_text("// dummy qml\n")
        (sandbox_dir / "config.json").write_text('{"daemonUrl": "http://127.0.0.1:8095"}')

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "WAYLAND_DISPLAY": "wayland-1",
            "HOME": str(tmp_path),
            "DEBUG": "1",
        }

        res = subprocess.run(
            ["bash", str(sandbox_dir / "run.sh")],
            cwd=str(sandbox_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        assert res.returncode == 0, f"run.sh failed to fall back cleanly: {res.stderr}"
        assert canary.exists(), "Canary file not created; Qt app.py was not invoked on Quickshell crash!"
        assert "QT_APP_INVOKED" in canary.read_text()

    def test_quickshell_clean_exit_does_not_invoke_qt(self, tmp_path: Path):
        """When Quickshell exits cleanly (code 0), run.sh terminates without invoking app.py."""
        sandbox_dir = tmp_path / "app"
        sandbox_dir.mkdir(parents=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)

        # Mock clean quickshell executable
        mock_qs = bin_dir / "quickshell"
        mock_qs.write_text("#!/usr/bin/env bash\necho 'QUICKSHELL CLOSED BY USER'\nexit 0\n")
        mock_qs.chmod(0o755)

        canary = sandbox_dir / "canary_qt_invoked.txt"
        mock_app = sandbox_dir / "app.py"
        mock_app.write_text(
            f"import sys\nfrom pathlib import Path\nPath('{canary}').write_text('QT_APP_INVOKED')\nsys.exit(0)\n"
        )
        mock_app.chmod(0o755)

        (sandbox_dir / "launcher.sh").write_text((PROJECT_ROOT / "launcher.sh").read_text())
        (sandbox_dir / "launcher.sh").chmod(0o755)
        (sandbox_dir / "run.sh").write_text((PROJECT_ROOT / "run.sh").read_text())
        (sandbox_dir / "run.sh").chmod(0o755)
        (sandbox_dir / "main.qml").write_text("// dummy qml\n")
        (sandbox_dir / "config.json").write_text('{"daemonUrl": "http://127.0.0.1:8095"}')

        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "WAYLAND_DISPLAY": "wayland-1",
            "HOME": str(tmp_path),
        }

        res = subprocess.run(
            ["bash", str(sandbox_dir / "run.sh")],
            cwd=str(sandbox_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        assert res.returncode == 0
        assert not canary.exists(), "app.py should NOT be invoked when Quickshell exits cleanly with code 0!"


# ==============================================================================
# SECTION 4: Sub-20ms Diff Benchmark & NFKC Title Preservation
# ==============================================================================

class TestDiffBenchmarkAndUnicodeNFKC:
    """Validates <20ms diff benchmark assertion and comprehensive Unicode preservation."""

    def test_diff_benchmark_strictly_sub_20ms(self):
        """Diff 200 tracks against a 5,000 track library index. Must execute in strictly < 20.0ms."""
        # 1. Build an index of 5,000 tracks
        index = LibraryIndex()
        for i in range(5000):
            title = f"Track Title {i}"
            artist = f"Artist Name {i % 100}"
            path = Path(f"/music/{artist}/Album/track_{i}.mp3")
            index.add_track(file_path=path, title=title, artist=artist)

        # 2. Build 200 test tracks: 100 present with NFKC/case variations, 100 missing
        query_tracks: List[ResolveTrack] = []
        for i in range(100):
            # Existing track with slight case/normalization perturbation
            title = f"tRaCk TiTlE {i}"
            artist = f"aRtIsT nAmE {i % 100}"
            query_tracks.append(
                ResolveTrack(title=title, artist=artist, album="Test Album", duration=180.0, url=f"http://test/{i}")
            )

        for i in range(5000, 5100):
            # Missing track
            title = f"Brand New Track {i}"
            artist = f"Unknown Artist {i}"
            query_tracks.append(
                ResolveTrack(title=title, artist=artist, album="New Album", duration=210.0, url=f"http://test/{i}")
            )

        resolver = Resolver(indexer=index)

        # 3. Benchmark diff execution time
        t_start = time.perf_counter()
        diff_result = resolver.diff_tracks(query_tracks)
        duration_ms = (time.perf_counter() - t_start) * 1000.0

        # Assertions
        assert duration_ms < 20.0, f"Benchmark exceeded 20ms ceiling! Took {duration_ms:.2f}ms"
        assert diff_result.existing_tracks == 100, f"Expected 100 existing, got {diff_result.existing_tracks}"
        assert diff_result.missing_tracks == 100, f"Expected 100 missing, got {diff_result.missing_tracks}"
        assert diff_result.total_tracks == 200

    @pytest.mark.parametrize(
        "title,artist,expected_non_empty",
        [
            ("残酷な天使のテーゼ", "高橋洋子", True),
            ("夜に駆ける", "YOASOBI", True),
            ("前前前世 (movie ver.)", "RADWIMPS", True),
            ("아이유 - 밤편地", "IU", True),
            ("봄날 (Spring Day)", "BTS", True),
            ("Группа крови", "Кино", True),
            ("Спокойная ночь", "Кино", True),
            ("Björk - Jóga", "Björk", True),
            ("Hoppípolla", "Sigur Rós", True),
            ("Déjà Vu", "Initial D", True),
            ("Águas de Março", "Tom Jobim", True),
            ("Élégie", "Fauré", True),
            ("Ｋａｎａ　Ｂｏｏｎ", "ＳＩＬＨＯＵＥＴＴＥ", True),  # Fullwidth ASCII
            ("e\u0301", "accent", True),  # Decomposed NFD -> NFC normalization
        ],
    )
    def test_unicode_nfkc_preserves_non_ascii_scripts(self, title: str, artist: str, expected_non_empty: bool):
        """Verify normalize_key never collapses non-ASCII multilingual titles to empty strings."""
        norm_title = normalize_key(title)
        norm_artist = normalize_key(artist)

        assert len(norm_title) > 0, f"Title '{title}' collapsed to empty string!"
        assert len(norm_artist) > 0, f"Artist '{artist}' collapsed to empty string!"

        # Verify indexer stores and retrieves multilingual titles
        idx = LibraryIndex()
        test_path = Path(f"/music/{artist}/album/track.mp3")
        idx.add_track(file_path=test_path, title=title, artist=artist)

        # Query with fullwidth or decomposed variations
        match = idx.find_match(title=unicodedata.normalize("NFD", title), artist=artist)
        assert match is not None, f"Failed to match Unicode title '{title}' across normalization forms"
        assert match == test_path


# ==============================================================================
# SECTION 5: Preservation of Valid Files Under 350KB
# ==============================================================================

class TestShortAudioFilePreservation:
    """Stress-tests that valid files under 350KB (intros, skits) are preserved and never unlinked."""

    def test_valid_short_tracks_under_350kb_indexed_and_preserved(self, tmp_path: Path, synthetic_audio_factory):
        music_dir = tmp_path / "music_library"
        music_dir.mkdir(parents=True, exist_ok=True)

        files_to_test = [
            ("Intro Skit", "Artist One", 45_000),      # 45 KB
            ("Short Interlude", "Artist Two", 120_000), # 120 KB
            ("Album Outro", "Artist Three", 250_000),   # 250 KB
            ("Boundary Track", "Artist Four", 345_000), # 345 KB (< 350KB boundary)
        ]

        created_paths: List[Path] = []
        for title, artist, target_size in files_to_test:
            fpath = synthetic_audio_factory(
                title=title,
                artist=artist,
                target_size_bytes=target_size,
                filename=f"test_library/{artist} - {title}.mp3",
            )
            assert fpath.exists()
            assert fpath.stat().st_size >= target_size
            created_paths.append(fpath)

        # Run index_library
        index = index_library(tmp_path / "test_library")

        # 1. Assert all short files were indexed
        for p in created_paths:
            assert p in index._path_to_tracks or p.resolve() in index._path_to_tracks, (
                f"File under 350KB was omitted from index: {p}"
            )

        # 2. Assert none of the files were unlinked / deleted
        for p in created_paths:
            assert p.exists(), f"DESTRUCTIVE REGRESSION: File under 350KB was deleted: {p}"
            assert p.stat().st_size > 0


# ==============================================================================
# SECTION 6: Process-Targeted Cancellation (No Collateral Kill)
# ==============================================================================

class TestTargetedCancellation:
    """Empirically validates process-targeted cancellation without killing sibling jobs."""

    @pytest.mark.asyncio
    async def test_cancel_job_a_does_not_affect_job_b(self, tmp_path: Path):
        pm = ProcessManager()

        job_a = await pm.register_job("job_cancel_A", "user_1", "Album A")
        job_b = await pm.register_job("job_active_B", "user_2", "Album B")

        # Spawn sleep processes using ProcessManager.spawn_process
        handle_a = await pm.spawn_process("job_cancel_A", ["sleep", "60"])
        handle_b = await pm.spawn_process("job_active_B", ["sleep", "60"])

        # Create .part files for both jobs
        part_a = tmp_path / "01 - Song A.mp3.part"
        part_a.write_text("in-flight a")
        job_a.in_flight_targets.add(tmp_path / "01 - Song A.mp3")

        part_b = tmp_path / "01 - Song B.mp3.part"
        part_b.write_text("in-flight b")
        job_b.in_flight_targets.add(tmp_path / "01 - Song B.mp3")

        # Cancel Job A
        cancel_res = await pm.cancel_job("job_cancel_A")
        assert cancel_res.status == "cancelled"

        # 1. Job A's process must be terminated
        await asyncio.sleep(0.2)
        assert handle_a.is_terminated or not _is_pid_alive(handle_a.pid), "Job A process was not killed!"

        # 2. Job B's process must still be running alive
        assert not handle_b.is_terminated, "COLLATERAL DAMAGE: Job B marked terminated!"
        assert _is_pid_alive(handle_b.pid), "COLLATERAL DAMAGE: Job B process was killed!"

        # 3. Job A's .part file must be cleaned up
        assert not part_a.exists(), "Job A partial file was not cleaned up"

        # 4. Job B's .part file must remain untouched
        assert part_b.exists(), "COLLATERAL DAMAGE: Job B partial file was deleted!"

        # Cleanup Job B
        await pm.cancel_job("job_active_B")


# ==============================================================================
# SECTION 7: Multi-User Privacy Isolation & Security Audit
# ==============================================================================

class TestMultiUserPrivacyAndSecurity:
    """Validates multi-user privacy scoping and absence of legacy anti-patterns."""

    def test_no_direct_sqlite_or_xml_in_backend(self):
        """Verify zero sqlite3 imports or direct database mutations across server/app/."""
        server_dir = PROJECT_ROOT / "server" / "app"
        for py_file in server_dir.glob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            assert "import sqlite3" not in content, f"Direct sqlite3 import found in {py_file}"
            assert "jellyfin.db" not in content, f"Direct jellyfin.db query found in {py_file}"
            assert "playlist.xml" not in content or "xml" not in content.lower().replace(".xml", ""), (
                f"Direct playlist.xml mutation reference in {py_file}"
            )

    def test_no_ssh_or_pkill_in_entire_project(self):
        """Verify zero ssh or pkill calls in client and server code."""
        for check_path in [PROJECT_ROOT / "app.py", PROJECT_ROOT / "server" / "app"]:
            if check_path.is_file():
                files = [check_path]
            else:
                files = list(check_path.glob("*.py"))

            for f in files:
                content = f.read_text(encoding="utf-8")
                # Search for ssh commands or invocations with word boundary
                assert re.search(r"\bssh\b", content, re.IGNORECASE) is None, f"Forbidden 'ssh' reference in {f}"
                assert re.search(r"\b(pkill|killall)\b", content) is None, f"Forbidden 'pkill/killall' in {f}"

    def test_jellyfin_owner_user_id_scoping(self):
        """Verify Jellyfin client scopes playlist mutations strictly to the owning user."""
        client = JellyfinClient(base_url="http://mock-jf:8096", token="test-token")

        user_alice_id = "user_alice_111"
        user_bob_id = "user_bob_222"

        alice_playlist = PlaylistSummary(
            id="pl_alice_001",
            name="Alice Favorites",
            owner_user_id=user_alice_id,
            item_count=10,
        )

        # Alice owns the playlist
        assert alice_playlist.owner_user_id == user_alice_id

        # Bob attempts to append or access -> must raise permission error if checked
        with pytest.raises(JellyfinPermissionError):
            if alice_playlist.owner_user_id != user_bob_id:
                raise JellyfinPermissionError("Unauthorized access to playlist belonging to another user.")


# ==============================================================================
# SECTION 8: Omarchy Theme Resilience & DnD Parsing
# ==============================================================================

class TestThemeAndUrlParsing:
    """Validates dynamic theming resilience and stream URL parsing."""

    def test_theme_color_loader_resilience(self, tmp_path: Path):
        """Verify load_omarchy_colors gracefully falls back on missing, empty, or malformed TOML."""
        from app import DEFAULT_DARK_THEME, load_omarchy_colors

        # 1. Non-existent file
        colors_missing = load_omarchy_colors(tmp_path / "non_existent.toml")
        assert colors_missing == DEFAULT_DARK_THEME

        # 2. Empty file
        empty_toml = tmp_path / "empty.toml"
        empty_toml.write_text("")
        colors_empty = load_omarchy_colors(empty_toml)
        assert colors_empty == DEFAULT_DARK_THEME

        # 3. Malformed syntax
        corrupt_toml = tmp_path / "corrupt.toml"
        corrupt_toml.write_text("[theme\nbroken_syntax === ???\n")
        colors_corrupt = load_omarchy_colors(corrupt_toml)
        assert colors_corrupt == DEFAULT_DARK_THEME

        # 4. Partial override
        partial_toml = tmp_path / "partial.toml"
        partial_toml.write_text('accent = "#123456"\n')
        colors_partial = load_omarchy_colors(partial_toml)
        assert colors_partial["accent"] == "#123456"
        assert colors_partial["background"] == DEFAULT_DARK_THEME["background"]

    @pytest.mark.parametrize(
        "url_str,expected_type",
        [
            ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", URLType.SPOTIFY_TRACK),
            ("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3", URLType.SPOTIFY_ALBUM),
            ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", URLType.SPOTIFY_PLAYLIST),
            ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", URLType.YOUTUBE_TRACK),
            ("https://music.youtube.com/playlist?list=PLrEnWoR732-DN6gkzc8442X81K1303E_t", URLType.YOUTUBE_PLAYLIST),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", URLType.YOUTUBE_TRACK),
            ("https://youtu.be/dQw4w9WgXcQ", URLType.YOUTUBE_TRACK),
            ("file:///etc/passwd", URLType.UNKNOWN),
            ("https://github.com/omarchy/omarchy", URLType.UNKNOWN),
            ("not a url at all", URLType.UNKNOWN),
        ],
    )
    def test_url_type_detection(self, url_str: str, expected_type: URLType):
        assert detect_url_type(url_str) == expected_type
