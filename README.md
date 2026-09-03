# 🎵 Jellyfin Music Downloader for Omarchy

A native, high-performance music ingestion desktop client and backend pipeline designed for Omarchy Linux and Jellyfin Media Server.

It allows you to paste or drag-and-drop Spotify, YouTube Music, or Soundcloud links (playlists, artists, albums, or tracks) and automatically downloads official studio audio, sidecars synchronized karaoke lyrics (`.lrc`), trims video skits via SponsorBlock, and syncs directly to private or shared Jellyfin playlists.

---

## 🌟 Key Features

- **🚀 Native Omarchy GUI:** Built with Quickshell and Wayland LayerShell, featuring smooth animations, drag-and-drop ingestion, and integrated console log filtering.
- **👥 Multi-User Jellyfin Isolation:** Discovers user accounts dynamically and locks imported playlists to the selected Jellyfin account (or a shared household profile).
- **🎤 Synchronized Karaoke Lyrics (`.lrc`):** Automatically fetches and saves real-time scrolling lyrics via LRCLIB for Finamp and Feishin.
- **✂️ SponsorBlock Integration:** Automatically strips non-music dialogue, YouTube video skits, and intro/outro silence.
- **⚡ Instant Skip & Deduplication:** Pre-scans local disk files in milliseconds to skip existing library tracks with zero network overhead.
- **⚙️ Configurable Architecture:** Flexible connection settings supporting remote SSH execution or local machine downloads.

---

## 📁 Repository Structure

```
jellyfin-music-app/
├── main.qml                          # Quickshell Wayland GUI application
├── launcher.sh                       # Client launcher script
├── install.sh                        # 1-click desktop installation script
├── config.json                       # Host, port, and directory configuration
├── jellyfin-music-downloader.desktop # FreeDesktop application shortcut
├── log-filter.py                     # Output protocol demuxer & timestamp logger
└── server/                           # Backend server components
    ├── Dockerfile                    # Container definition with spotDL, Deno, and patches
    ├── ingest.py                     # Ingestion orchestrator & Jellyfin API synchronizer
    ├── patch_skip.py                 # In-memory indexer & rapid skip filter
    └── get-music.sh                  # Optional CLI ingestion wrapper
```

---

## 💻 Client Installation

### On Linux Mint / Ubuntu / Debian / Generic Linux
1. Clone or copy this repository:
   ```bash
   git clone <repo-url> ~/.config/omarchy/extensions/jellyfin-music-app
   ```
2. Run the 1-click installer:
   ```bash
   cd ~/.config/omarchy/extensions/jellyfin-music-app
   ./install.sh
   ```
3. Launch **Download to Jellyfin (Music)** from your Mint application menu (**Menu $\rightarrow$ Sound & Video**) or run `~/.config/omarchy/extensions/jellyfin-music-app/run.sh`.

### On Omarchy Linux (Wayland / Hyprland)
Follow the exact same steps! The smart launcher (`run.sh`) automatically detects Omarchy and boots the native Quickshell LayerShell interface.

---

## 🖥️ Server Setup (Jellyfin Host)

If running the backend on a separate server (e.g. an Ubuntu Server PC running Docker & Jellyfin):

1. **Copy Server Scripts:**
   ```bash
   scp -r server/ user@server:~/spotdl/
   ```
2. **Build the Custom SpotDL Container:**
   ```bash
   ssh user@server "cd ~/spotdl && docker build -t spotdl-custom:latest ."
   ```
3. **Configure Environment Variables / Paths in `ingest.py`:**
   Ensure `MUSIC_DIR` (e.g. `/mnt/media/music`) and your `JELLYFIN_URL` / `JELLYFIN_TOKEN` are set.
4. **Setup SSH Keys:** Ensure the Omarchy desktop machine has passwordless SSH access to the server (`ssh-copy-id user@server`).

---

## ⚙️ Configuration (`config.json`)

Settings can be customized directly in the GUI under the **Settings** tab or in `~/.config/omarchy/extensions/jellyfin-music-app/config.json`:

```json
{
  "serverHost": "your-server-ip-or-hostname",
  "jellyfinWebUrl": "http://your-server-ip:8096",
  "musicFolderUrl": "sftp://your-server-ip/mnt/media/music",
  "remoteScriptPath": "~/spotdl/ingest.py"
}
```

---

## 📄 License
MIT License. Built for the Omarchy Linux community.
