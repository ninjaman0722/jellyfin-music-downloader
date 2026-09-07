# syntax=docker/dockerfile:1.4
# ==============================================================================
# Jellyfin Music Downloader V2 - Production Daemon Multi-Stage Dockerfile
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Build & Dependency Compilation Stage
# ------------------------------------------------------------------------------
FROM python:3.12.8-slim-bookworm AS builder

# Prevent Python from writing .pyc bytecode and force unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install minimal build tools required for compiling Python C-extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy production requirements and install wheels into isolated /install prefix
COPY requirements.txt .
RUN pip install --prefix=/install --no-warn-script-location -r requirements.txt

# Install spotdl in an isolated virtual environment to avoid fastapi dependency conflicts
RUN python3 -m venv /opt/spotdl && \
    /opt/spotdl/bin/pip install --no-cache-dir "spotdl>=4.5.2" && \
    sed -i 's/language="de"/language="en"/g' /opt/spotdl/lib/python*/site-packages/spotdl/providers/audio/ytmusic.py && \
    sed -i 's/auto_retries: int = 0/auto_retries: int = 3/g' /opt/spotdl/lib/python*/site-packages/spotapi/http/request.py

# ------------------------------------------------------------------------------
# Stage 2: Hardened Unprivileged Production Runtime Stage
# ------------------------------------------------------------------------------
FROM python:3.12.8-slim-bookworm AS runtime

# Runtime labels following OCI specifications
LABEL org.opencontainers.image.title="Jellyfin Music Downloader Daemon" \
      org.opencontainers.image.description="Asynchronous media daemon for Spotify/YouTube music downloading and Jellyfin REST synchronization" \
      org.opencontainers.image.version="2.1.0" \
      org.opencontainers.image.authors="Omarchy Linux Community" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:$PATH" \
    PYTHONPATH="/app" \
    PORT=8095 \
    MUSIC_DIR=/music

# Install minimal system runtime dependencies:
# - ffmpeg: Required for spotdl audio conversion, yt-dlp post-processing, and format transcoding
# - ca-certificates: Required for secure TLS connections to Spotify, YouTube, Jellyfin, and LRCLIB
# - curl: Required for Docker healthcheck probes against http://localhost:8095/health
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pre-built Python packages and binaries from builder stage
COPY --from=builder /install /usr/local

# Copy isolated spotdl virtual environment and link CLI binary into PATH
COPY --from=builder /opt/spotdl /opt/spotdl
RUN ln -s /opt/spotdl/bin/spotdl /usr/local/bin/spotdl

# Create unprivileged system user and group (UID 1000, GID 1000)
# This prevents container breakouts and matches host user UID 1000 file ownership
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -d /app -s /usr/sbin/nologin appuser

# Set up application, media, and log directories with strict POSIX permissions
RUN mkdir -p /app /music /app/logs && \
    chown -R 1000:1000 /app /music && \
    chmod 755 /app /music /app/logs

WORKDIR /app

# Copy application source code with unprivileged ownership
COPY --chown=1000:1000 app/ ./app/
RUN mkdir -p /app/server && ln -s /app/app /app/server/app && touch /app/server/__init__.py && chown -R 1000:1000 /app

# Switch to non-root user
USER 1000:1000

# Expose daemon HTTP/WebSocket port
EXPOSE 8095

# Healthcheck probe hitting daemon REST /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8095/health || exit 1

# Clean entrypoint launching Uvicorn ASGI server
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
