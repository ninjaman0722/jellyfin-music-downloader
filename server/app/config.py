"""Configuration loader and validator for Jellyfin Music Downloader Daemon.

Enforces 0600 file permissions and masks secrets from public APIs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:
    from pydantic import BaseSettings  # type: ignore
    SettingsConfigDict = None  # type: ignore

logger = logging.getLogger("jellyfin_music_daemon.config")

APP_VERSION = "2.1.0"


def default_music_dir() -> Path:
    """Detect default music directory depending on host or container environment."""
    container_dir = Path("/music")
    if container_dir.is_dir():
        return container_dir
    return Path("/mnt/media/music")


def default_log_file() -> Path:
    """Detect default log file depending on host or container environment."""
    container_logs = Path("/app/logs")
    if container_logs.is_dir():
        return container_logs / "daemon.log"
    return Path("server/daemon.log")


def find_config_file() -> Optional[Path]:
    """Search for existing configuration file across standard locations."""
    candidates: List[Path] = []
    env_path = os.environ.get("CONFIG_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    home = Path.home()
    candidates.extend([
        home / ".config" / "omarchy" / "extensions" / "jellyfin-music-app" / "server" / "server_config.json",
        Path("server/server_config.json").resolve(),
        Path("server_config.json").resolve(),
        Path("/app/server_config.json"),
        home / "spotdl" / "server_config.json",
    ])

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def enforce_config_permissions(path: Path) -> None:
    """Enforce POSIX 0600 (read/write only by owner) on configuration file
    to prevent unauthorized reading of Jellyfin API tokens.
    """
    if not path or not path.exists():
        return
    try:
        current_mode = stat.S_IMODE(path.stat().st_mode)
        if current_mode & 0o077 != 0:
            logger.warning(
                "Config file '%s' has permissive mode %s. Enforcing 0600 permissions.",
                path,
                oct(current_mode),
            )
            path.chmod(0o600)
    except (PermissionError, OSError) as e:
        logger.warning("Could not enforce 0600 permissions on '%s': %s", path, e)


class PublicConfig(BaseModel):
    """Sanitized configuration returned to clients via GET /api/config (no secret tokens)."""
    music_dir: str
    bitrate: str
    default_user: str
    jellyfin_url: str
    download_threads: int
    sponsorblock: bool
    lyrics_provider: str
    max_file_size_mb: int
    version: str = APP_VERSION


class ServerConfig(BaseSettings):
    """Master server configuration supporting environment variables and JSON config files."""
    music_dir: Path = Field(default_factory=default_music_dir, description="Target music storage path")
    bitrate: str = Field(default="320k", description="Audio bitrate (320k, 256k, 192k, auto)")
    download_threads: int = Field(default=4, ge=1, le=16, description="Concurrent download workers")
    jellyfin_url: str = Field(default="http://127.0.0.1:8096", description="Jellyfin server REST URL")
    jellyfin_token: str = Field(default="", description="Jellyfin administrative API token (sensitive)")
    default_user: str = Field(default="", description="Default Jellyfin user account")
    sponsorblock: bool = Field(default=True, description="Enable SponsorBlock during extraction")
    lyrics_provider: str = Field(default="lrclib", description="Lyrics provider (lrclib)")
    max_file_size_mb: int = Field(default=250, description="Max audio track file size limit in MB")
    log_file: Path = Field(default_factory=default_log_file, description="Rotating log file destination")
    host: str = Field(default="0.0.0.0", description="Host address to bind server")
    port: int = Field(default=8095, description="Port to listen on")
    cors_origins: List[str] = Field(default_factory=lambda: ["*"], description="Allowed CORS origins")

    if SettingsConfigDict:
        model_config = SettingsConfigDict(
            env_prefix="",
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )
    else:
        class Config:
            extra = "ignore"
            env_prefix = ""

    def to_public(self) -> PublicConfig:
        """Convert to sanitized PublicConfig with secrets stripped."""
        return PublicConfig(
            music_dir=str(self.music_dir),
            bitrate=self.bitrate,
            default_user=self.default_user,
            jellyfin_url=self.jellyfin_url,
            download_threads=self.download_threads,
            sponsorblock=self.sponsorblock,
            lyrics_provider=self.lyrics_provider,
            max_file_size_mb=self.max_file_size_mb,
            version=APP_VERSION,
        )


def load_server_config(config_file: Optional[Path] = None) -> ServerConfig:
    """Factory function to load configuration:
    1. Base defaults
    2. Overridden by server_config.json if found
    3. Overridden by environment variables (highest priority)
    """
    file_path = config_file or find_config_file()
    file_data: Dict[str, Any] = {}
    if file_path and file_path.is_file():
        enforce_config_permissions(file_path)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                file_data = json.load(f)
            logger.info("Loaded server configuration from '%s'", file_path)
        except Exception as e:
            logger.error("Failed to parse config file '%s': %s", file_path, e)

    # Environment variables have highest priority (override JSON file)
    env_overrides = {
        "MUSIC_DIR": "music_dir",
        "BITRATE": "bitrate",
        "JELLYFIN_URL": "jellyfin_url",
        "JELLYFIN_TOKEN": "jellyfin_token",
        "DEFAULT_USER": "default_user",
        "DOWNLOAD_THREADS": "download_threads",
        "LOG_FILE": "log_file",
        "PORT": "port",
    }
    for env_var, key in env_overrides.items():
        val = os.environ.get(env_var)
        if val:
            if key in ("download_threads", "port"):
                try:
                    file_data[key] = int(val)
                except ValueError:
                    pass
            else:
                file_data[key] = val

    # In container environments, route localhost/127.0.0.1 to host.docker.internal
    if os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER") or Path("/music").is_dir():
        url = file_data.get("jellyfin_url", "http://127.0.0.1:8096")
        if "127.0.0.1" in url or "localhost" in url:
            file_data["jellyfin_url"] = re.sub(r"(localhost|127\.0\.0\.1)", "host.docker.internal", url)

        # In container, if host path /mnt/media/music doesn't exist but /music does, remap
        md = file_data.get("music_dir")
        if (not md or not Path(str(md)).is_dir()) and Path("/music").is_dir():
            file_data["music_dir"] = "/music"

    return ServerConfig(**file_data)


@lru_cache()
def get_settings() -> ServerConfig:
    """Cached singleton provider for FastAPI dependency injection."""
    return load_server_config()
