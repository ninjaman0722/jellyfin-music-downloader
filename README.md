# 🎵 Jellyfin Music Downloader (V2.0)

A high-performance, containerized music ingestion pipeline and native desktop client designed for **Omarchy Linux** and **Jellyfin Media Server**.

Paste or drag-and-drop Spotify, YouTube Music, or SoundCloud links (playlists, full artist discographies, albums, or tracks). The V2 engine automatically resolves metadata, detects and skips pre-existing library tracks in milliseconds, downloads official studio audio via concurrent workers, tags tracks with ID3v2.4 metadata and high-res cover art, fetches synchronized karaoke lyrics (`.lrc`) via LRCLIB, and registers them directly into Jellyfin playlists.

---

## 🌟 Key Features (V2 Architecture)

- **⚡ Client-Server REST & WebSocket Architecture:** Lightweight decoupled design. The client communicates with the backend daemon via FastAPI REST endpoints and real-time WebSocket progress event streams.
- **🚀 Dual-Engine Graphical Interface:**
  - **Native Quickshell (Wayland / Hyprland):** Modular Wayland LayerShell interface (`qml/`) with live reactive progress bars, Omarchy theme integration, and smooth hardware-accelerated animations.
  - **Universal Qt6 Fallback (`app.py`):** Standalone PySide6 / PyQt desktop client for generic Linux distributions, X11 sessions, GNOME, KDE, or headless/remote management.
  - **Smart Fallback Router (`run.sh`):** Probes daemon health and automatically switches to the Qt client if Quickshell is unavailable.
- **🛡️ Instant Local Library Deduplication:** Pre-flight diff engine indexes 1,000+ library audio files in under 2 seconds. Identifies existing tracks before downloading to prevent duplicates.
- **🎤 Synchronized Karaoke Lyrics (`.lrc`):** Queries LRCLIB for timestamped lyrics with duration validation ($\pm 3\text{s}$) and embeds them directly into ID3v2.4 tags for seamless playback in Finamp, Feishin, and Jellyfin.
- **👥 Multi-User Account Scoping & Global Ingest:**
  - Automatically queries Jellyfin user accounts and playlists.
  - **Strict User Privacy Isolation:** Playlists are created as private (`IsPublic: false`) and locked strictly to the targeted user account (`/Playlists/{id}/Users/{userId}`), preventing playlist clutter across shared household profiles.
  - Full support for playlists of any size (>100 tracks paginated via Spotify API).
  - Artist discographies, albums, and singles can optionally bypass playlist creation and ingest directly to the server-wide music library (`__NO_PLAYLIST__`).
- **🐳 Hardened Docker Deployment:** Non-root execution (`appuser`, PUID/PGID), zero-secret public API masking, container healthchecks, and POSIX 0600 configuration hardening.
- **🧪 Comprehensive Test Suite:** 437 automated unit, integration, stress, and packaging tests.

---

## 📁 Repository Structure

```
jellyfin-music-app/
├── qml/                                # Modular Quickshell QML components
│   ├── components/                     # AnalysisCard, PlaylistPicker, ToastBanner, UserSelector
│   ├── theme/                          # Dynamic Omarchy theme color definitions
│   └── views/                          # IngestView, ProgressView, SettingsView
├── server/                             # V2 Backend Daemon
│   ├── app/                            # Core application package
│   │   ├── config.py                   # Configuration schemas & 0600 permission hardening
│   │   ├── downloader.py               # Concurrent spotDL subprocess worker pool
│   │   ├── indexer.py                  # High-performance disk audio indexer
│   │   ├── jellyfin.py                 # Jellyfin REST API client & chunked playlist manager
│   │   ├── logger.py                   # Rotating JSON/text log manager
│   │   ├── lyrics.py                   # Async LRCLIB client with duration validation
│   │   ├── main.py                     # FastAPI REST & WebSocket server
│   │   ├── process.py                  # Job lifecycle, cancellation & lock manager
│   │   ├── resolver.py                 # Spotify & yt-dlp metadata extractor
│   │   ├── tagger.py                   # ID3v2.4 & lyrics audio file tagger
│   │   └── ws.py                       # WebSocket connection pool & event dispatcher
│   ├── Dockerfile                      # Hardened multi-stage container definition
│   ├── docker-compose.yml              # Headless server deployment compose file
│   ├── jellyfin-music-daemon.service   # Systemd user service definition
│   ├── requirements.txt                # Daemon production dependencies
│   └── requirements-dev.txt            # Development & testing dependencies
├── scripts/
│   └── ws_listener.py                  # Python WebSocket companion bridge for Quickshell
├── tests/                              # Automated test suite (437 tests)
├── app.py                              # Universal PySide6 / PyQt fallback desktop client
├── main.qml                            # Root Quickshell Wayland interface
├── run.sh                              # Runtime launcher & intelligent fallback router
├── launcher.sh                         # Omarchy-shell summon wrapper
├── install.sh                          # POSIX hardened 1-click system installer
├── docker-compose.yml                  # Root Docker Compose deployment
├── config.example.json                 # Client configuration template
├── .env.example                        # Server environment configuration template
└── jellyfin-music-downloader.desktop   # FreeDesktop application entry
```

---

## 🚀 Installation & Deployment

### Mode 1: Remote Media Server (Recommended)

Run the backend daemon in Docker on your media server (e.g. Ubuntu Server, Debian, or TrueNAS) alongside Jellyfin:

#### Step 1: Deploy the Daemon on the Server
1. Copy or clone the repository to your media server:
   ```bash
   git clone https://github.com/ninjaman0722/jellyfin-music-downloader.git ~/jellyfin-music-daemon
   cd ~/jellyfin-music-daemon
   ```
2. Create your environment configuration:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Configure the following parameters:
   ```env
   MUSIC_DIR=/mnt/media/music             # Path to your music library on the server
   JELLYFIN_URL=http://localhost:8096      # Internal Jellyfin URL
   JELLYFIN_TOKEN=your_jellyfin_api_token # Jellyfin API key (Dashboard -> Keys)
   DOWNLOAD_THREADS=4                     # Concurrent download workers
   BITRATE=320k                           # Target MP3 audio bitrate
   PUID=1000                              # Server user ID
   PGID=1000                              # Server group ID

   # Optional: Required for Spotify playlists exceeding 100 tracks
   SPOTIFY_CLIENT_ID=your_spotify_client_id
   SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
   SPOTIFY_REFRESH_TOKEN=your_spotify_refresh_token
   ```
3. Start the daemon:
   ```bash
   docker compose up -d --build
   ```
4. Verify the server is running:
   ```bash
   curl http://localhost:8095/health
   # Returns: {"status":"healthy","version":"2.1.0",...}
   ```

#### Step 2: Install the Client on Your Desktop (Omarchy / Linux)
1. On your desktop machine, clone the repository:
   ```bash
   git clone https://github.com/ninjaman0722/jellyfin-music-downloader.git ~/.config/omarchy/extensions/jellyfin-music-app
   cd ~/.config/omarchy/extensions/jellyfin-music-app
   ```
2. Run the installer:
   ```bash
   ./install.sh
   ```
3. Point the client to your server:
   Edit `~/.config/omarchy/extensions/jellyfin-music-app/config.json` (or use the in-app **Settings** tab):
   ```json
   {
     "daemonUrl": "http://<server-ip>:8095",
     "wsUrl": "ws://<server-ip>:8095/ws/events",
     "jellyfinWebUrl": "http://<server-ip>:8096"
   }
   ```
4. Launch the app from your application launcher (**Download to Jellyfin (Music)**) or by running:
   ```bash
   ~/.config/omarchy/extensions/jellyfin-music-app/run.sh
   ```

---

### Mode 2: All-in-One Local Setup (Single Desktop Machine)

If Jellyfin and the music downloader run on the same computer:

1. Clone and run the installer:
   ```bash
   git clone https://github.com/ninjaman0722/jellyfin-music-downloader.git ~/.config/omarchy/extensions/jellyfin-music-app
   cd ~/.config/omarchy/extensions/jellyfin-music-app
   ./install.sh
   ```
2. Configure credentials in `server/server_config.json`:
   ```json
   {
     "jellyfin_url": "http://127.0.0.1:8096",
     "jellyfin_token": "your_api_key_here",
     "music_dir": "/mnt/media/music"
   }
   ```
3. Enable and start the systemd user daemon:
   ```bash
   systemctl --user enable --now jellyfin-music-daemon.service
   ```
4. Launch the application:
   ```bash
   ./run.sh
   ```

---

## ⚙️ Configuration Reference

### Client Configuration (`config.json`)
Stored at `~/.config/omarchy/extensions/jellyfin-music-app/config.json` (permissions enforced at `0600`):

| Key | Default | Description |
| :--- | :--- | :--- |
| `daemonUrl` | `http://127.0.0.1:8095` | HTTP REST endpoint of the music daemon |
| `wsUrl` | `ws://127.0.0.1:8095/ws/events` | WebSocket live events endpoint |
| `jellyfinWebUrl` | `http://127.0.0.1:8096` | Web address for opening Jellyfin in your browser |
| `musicFolderUrl` | `/mnt/media/music` | Local or network path to the music library |
| `defaultUser` | `""` | Default Jellyfin user ID (leave blank to select in UI) |
| `bitrate` | `"320k"` | Target audio bitrate (`320k`, `256k`, `192k`, `128k`) |
| `embedLyrics` | `true` | Fetch and embed synchronized LRCLIB karaoke lyrics |
| `embedCover` | `true` | Embed high-resolution album artwork into ID3 tags |
| `autoClipboardDetect` | `true` | Automatically detect supported URLs in clipboard |
| `themeSync` | `true` | Follow Omarchy desktop theme palette |

### Server Daemon Environment (`.env` or Container Environment)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MUSIC_DIR` | `/mnt/media/music` | Destination directory on host for music downloads |
| `JELLYFIN_URL` | `http://localhost:8096` | URL to reach Jellyfin server |
| `JELLYFIN_TOKEN` | *required* | Jellyfin API token (from Jellyfin Dashboard $\rightarrow$ API Keys) |
| `DOWNLOAD_THREADS` | `4` | Number of parallel spotDL download workers |
| `BITRATE` | `320k` | Output audio bitrate |
| `LOG_LEVEL` | `INFO` | Application log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `PUID` / `PGID` | `1000` / `1000` | User/Group ID for write permissions on downloaded files |
| `SPOTIFY_CLIENT_ID` | *optional* | Spotify Developer App Client ID (required for playlists >100 tracks) |
| `SPOTIFY_CLIENT_SECRET` | *optional* | Spotify Developer App Client Secret |
| `SPOTIFY_REFRESH_TOKEN` | *optional* | Spotify OAuth user refresh token for paginating extended playlists |

---

## 🧪 Testing & Verification

The project includes an extensive test suite verifying process management, API contracts, character encoding, multi-disc tracks, and UI fallbacks:

```bash
# Set up development virtual environment
cd ~/.config/omarchy/extensions/jellyfin-music-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements-dev.txt

# Run full test suite
pytest -v
```

**Results:**
```
437 passed, 2 warnings in 187s
```

---

## 📄 License
MIT License. Built for the Omarchy Linux and Jellyfin community.
