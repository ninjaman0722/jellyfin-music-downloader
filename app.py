#!/usr/bin/env python3
"""
Jellyfin Music Downloader - Universal Desktop Client
Compatible with Linux Mint (Cinnamon), Ubuntu, Fedora, Debian, Arch, and Omarchy.
Supports PyQt5, PyQt6, and PySide6 seamlessly.
"""

import sys, os, json, subprocess, re, time

# Universal Qt Compatibility Layer
QT_BINDING = None
try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt, Signal, Slot, QThread
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtCore import Qt, pyqtSignal as Signal, pyqtSlot as Slot, QThread
        QT_BINDING = "PyQt6"
    except ImportError:
        try:
            from PyQt5 import QtCore, QtGui, QtWidgets
            from PyQt5.QtCore import Qt, pyqtSignal as Signal, pyqtSlot as Slot, QThread
            QT_BINDING = "PyQt5"
        except ImportError:
            print("ERROR: No Qt binding found.")
            print("Please install PyQt5, PyQt6, or PySide6:")
            print("  Linux Mint / Debian / Ubuntu: sudo apt install python3-pyqt5 (or python3-pyside6)")
            print("  Arch / Fedora / Generic:      pip install PySide6")
            sys.exit(1)

CONFIG_DIR = os.path.expanduser("~/.config/omarchy/extensions/jellyfin-music-app")
STATE_DIR = os.path.expanduser("~/.local/state/omarchy/extensions/jellyfin-music-app")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
PREF_FILE = os.path.join(STATE_DIR, "preferences.json")
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "serverHost": "192.168.1.159",
    "jellyfinWebUrl": "http://192.168.1.159:8096",
    "musicFolderUrl": "sftp://192.168.1.159/mnt/media/music",
    "remoteScriptPath": "~/spotdl/ingest.py",
    "defaultUser": ""
}

NORD_STYLE = """
QMainWindow, QWidget {
    background-color: #242933;
    color: #eceff4;
    font-family: 'Inter', 'Ubuntu', 'Segoe UI', sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #3b4252;
    background: #242933;
    border-radius: 8px;
    margin-top: -1px;
}
QTabBar::tab {
    background: #2e3440;
    color: #8fbcbb;
    padding: 9px 20px;
    margin-right: 4px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    font-weight: bold;
    border: 1px solid #3b4252;
}
QTabBar::tab:selected {
    background: #3b4252;
    color: #eceff4;
    border-bottom: 2px solid #88c0d0;
}
QTabBar::tab:hover:!selected {
    background: #353b49;
    color: #d8dee9;
}
QGroupBox {
    border: 1px solid #3b4252;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 14px;
    font-weight: bold;
    color: #d8dee9;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 12px;
}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {
    background-color: #2e3440;
    color: #eceff4;
    border: 1px solid #434c5e;
    border-radius: 6px;
    padding: 8px;
    selection-background-color: #88c0d0;
    selection-color: #242933;
}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
    border: 1px solid #88c0d0;
}
QPushButton {
    background-color: #3b4252;
    color: #eceff4;
    border: 1px solid #4c566a;
    border-radius: 6px;
    padding: 8px 16px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #434c5e;
    border-color: #88c0d0;
}
QPushButton:pressed {
    background-color: #4c566a;
}
QPushButton#primaryBtn {
    background-color: #88c0d0;
    color: #242933;
    border: none;
    font-size: 14px;
    padding: 10px 20px;
}
QPushButton#primaryBtn:hover {
    background-color: #81a1c1;
}
QPushButton#dangerBtn {
    background-color: #bf616a;
    color: #eceff4;
    border: none;
}
QPushButton#dangerBtn:hover {
    background-color: #d08770;
}
QProgressBar {
    background-color: #2e3440;
    border: 1px solid #3b4252;
    border-radius: 6px;
    text-align: center;
    color: #eceff4;
    font-weight: bold;
    height: 22px;
}
QProgressBar::chunk {
    background-color: #88c0d0;
    border-radius: 5px;
}
QScrollBar:vertical {
    border: none;
    background: #242933;
    width: 8px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: #4c566a;
    min-height: 20px;
    border-radius: 4px;
}
QScrollBar::handle:vertical:hover {
    background: #88c0d0;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
"""

class IngestWorker(QThread):
    progress_signal = Signal(int)
    track_signal = Signal(str)
    counts_signal = Signal(str)
    status_signal = Signal(str)
    log_signal = Signal(str, bool)
    finished_signal = Signal(bool, str)

    def __init__(self, server_host, script_path, payload):
        super().__init__()
        self.server_host = server_host
        self.script_path = script_path
        self.payload = payload
        self.process = None
        self.is_cancelled = False

    def run(self):
        payload_str = json.dumps(self.payload)
        cmd = [
            "ssh", "-o", "ConnectTimeout=10", self.server_host,
            f"python3 -u {self.script_path} --batch '{payload_str}'"
        ]
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            for line in iter(self.process.stdout.readline, ''):
                if self.is_cancelled:
                    break
                line = line.strip()
                if not line:
                    continue

                if line.startswith("PROGRESS:"):
                    try:
                        pct = int(float(line.split(":", 1)[1].strip()))
                        self.progress_signal.emit(pct)
                    except ValueError:
                        pass
                elif line.startswith("TRACK:"):
                    self.track_signal.emit(line.split(":", 1)[1].strip())
                elif line.startswith("COUNTS:"):
                    self.counts_signal.emit(line.split(":", 1)[1].strip())
                elif line.startswith("STATUS:"):
                    self.status_signal.emit(line.split(":", 1)[1].strip())
                elif line.startswith("STAGE:"):
                    self.status_signal.emit(f"Stage: {line.split(':', 1)[1].strip().title()}")
                elif line.startswith("COMPLETE:"):
                    msg = line.split(":", 1)[1].strip()
                    self.log_signal.emit(f"🎉 {msg}", True)
                    self.finished_signal.emit(True, msg)
                    return
                elif line.startswith("BATCH_ITEM:"):
                    parts = line.split(":", 1)[1].strip().split("|")
                    if len(parts) >= 2:
                        self.status_signal.emit(f"Processing Item {parts[0]}/{parts[1]} ({parts[2] if len(parts)>2 else ''})")
                else:
                    clean = line[4:].strip() if line.startswith("LOG:") else line
                    is_imp = any(x in clean.lower() for x in ["error", "fail", "success", "imported", "synced", "skipping"])
                    self.log_signal.emit(clean, is_imp)

            self.process.stdout.close()
            ret = self.process.wait()
            if not self.is_cancelled:
                if ret == 0:
                    self.finished_signal.emit(True, "All items processed successfully!")
                else:
                    self.finished_signal.emit(False, f"Process exited with status code {ret}")
        except Exception as e:
            self.finished_signal.emit(False, str(e))

    def cancel(self):
        self.is_cancelled = True
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        # Send remote clean termination
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=4", self.server_host,
             "pkill -9 -f 'spotdl/ingest.py' || true; docker stop -t 2 $(docker ps -q --filter ancestor=spotdl-custom:latest) 2>/dev/null || true; docker kill $(docker ps -q --filter ancestor=spotdl-custom:latest) 2>/dev/null || true; echo '{\"running\":false,\"status\":\"Cancelled by user\"}' > ~/spotdl/active_state.json"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jellyfin Music Downloader")
        self.resize(720, 640)
        self.setMinimumSize(660, 560)
        self.setStyleSheet(NORD_STYLE)

        self.config = self.load_config()
        self.users = []
        self.selected_user = None
        self.worker = None

        self.init_ui()
        self.load_users_from_server()

    def load_config(self):
        cfg = dict(DEFAULT_CONFIG)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception:
                pass
        return cfg

    def save_config(self):
        self.config["serverHost"] = self.host_input.text().strip()
        self.config["jellyfinWebUrl"] = self.jellyfin_url_input.text().strip()
        self.config["musicFolderUrl"] = self.folder_url_input.text().strip()
        self.config["remoteScriptPath"] = self.script_path_input.text().strip()
        self.config["defaultUser"] = self.default_user_input.text().strip()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
            QtWidgets.QMessageBox.information(self, "Settings Saved", "Configuration saved successfully!")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Save Error", str(e))

    def init_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Header
        header_layout = QtWidgets.QHBoxLayout()
        title_icon = QtWidgets.QLabel("🎵")
        title_icon.setStyleSheet("font-size: 24px;")
        title_text = QtWidgets.QLabel("Jellyfin Music Downloader")
        title_text.setStyleSheet("font-size: 18px; font-weight: bold; color: #88c0d0;")
        header_layout.addWidget(title_icon)
        header_layout.addWidget(title_text)
        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        # Tabs
        self.tabs = QtWidgets.QTabWidget()
        main_layout.addWidget(self.tabs)

        # Tab 1: Ingest
        self.tab_ingest = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_ingest, "📥 Add Music")
        self.setup_ingest_tab()

        # Tab 2: Live Progress
        self.tab_progress = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_progress, "📊 Task Progress")
        self.setup_progress_tab()

        # Tab 3: Settings
        self.tab_settings = QtWidgets.QWidget()
        self.tabs.addTab(self.tab_settings, "⚙️ Settings")
        self.setup_settings_tab()

    def setup_ingest_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_ingest)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # URL Input Header
        url_header = QtWidgets.QHBoxLayout()
        url_label = QtWidgets.QLabel("Spotify or YouTube Music URLs (one per line):")
        url_label.setStyleSheet("font-weight: bold; color: #d8dee9;")
        self.link_badge = QtWidgets.QLabel("0 links")
        self.link_badge.setStyleSheet("color: #8fbcbb; font-size: 11px;")
        clear_btn = QtWidgets.QPushButton("✕ Clear")
        clear_btn.setStyleSheet("padding: 4px 10px; font-size: 11px;")
        clear_btn.clicked.connect(self.clear_input)

        url_header.addWidget(url_label)
        url_header.addStretch()
        url_header.addWidget(self.link_badge)
        url_header.addWidget(clear_btn)
        layout.addLayout(url_header)

        # Text Area
        self.url_edit = QtWidgets.QPlainTextEdit()
        self.url_edit.setPlaceholderText("Paste Spotify playlist, album, artist, or track links here...\nhttps://open.spotify.com/playlist/...\nhttps://music.youtube.com/watch?v=...")
        self.url_edit.textChanged.connect(self.update_link_count)
        self.url_edit.setFixedHeight(120)
        layout.addWidget(self.url_edit)

        # User Selection Group
        user_group = QtWidgets.QGroupBox("Target Jellyfin Account")
        user_layout = QtWidgets.QVBoxLayout(user_group)
        self.user_btn_layout = QtWidgets.QHBoxLayout()
        self.user_status_label = QtWidgets.QLabel("Connecting to server to load accounts...")
        self.user_status_label.setStyleSheet("color: #8fbcbb;")
        self.user_btn_layout.addWidget(self.user_status_label)
        user_layout.addLayout(self.user_btn_layout)
        layout.addWidget(user_group)

        # Routing & Options Group
        opts_group = QtWidgets.QGroupBox("Song Ingestion & Routing Options")
        opts_layout = QtWidgets.QVBoxLayout(opts_group)

        # Playlist Routing Radios
        routing_layout = QtWidgets.QHBoxLayout()
        self.radio_lib_only = QtWidgets.QRadioButton("Library Only")
        self.radio_lib_only.setChecked(True)
        self.radio_existing_pl = QtWidgets.QRadioButton("Add to Existing Playlist")
        self.radio_new_pl = QtWidgets.QRadioButton("Create New Playlist")
        routing_layout.addWidget(self.radio_lib_only)
        routing_layout.addWidget(self.radio_existing_pl)
        routing_layout.addWidget(self.radio_new_pl)
        routing_layout.addStretch()
        opts_layout.addLayout(routing_layout)

        # Playlist Selectors Container
        self.pl_controls_layout = QtWidgets.QHBoxLayout()
        self.pl_combo = QtWidgets.QComboBox()
        self.pl_combo.setVisible(False)
        self.pl_new_input = QtWidgets.QLineEdit()
        self.pl_new_input.setPlaceholderText("Enter new playlist name...")
        self.pl_new_input.setVisible(False)
        self.pl_controls_layout.addWidget(self.pl_combo)
        self.pl_controls_layout.addWidget(self.pl_new_input)
        opts_layout.addLayout(self.pl_controls_layout)

        self.radio_lib_only.toggled.connect(self.update_routing_visibility)
        self.radio_existing_pl.toggled.connect(self.update_routing_visibility)
        self.radio_new_pl.toggled.connect(self.update_routing_visibility)

        # Audio Bitrate Selector
        bitrate_layout = QtWidgets.QHBoxLayout()
        bitrate_label = QtWidgets.QLabel("Audio Quality:")
        bitrate_label.setStyleSheet("font-weight: bold; color: #d8dee9;")
        self.bitrate_combo = QtWidgets.QComboBox()
        self.bitrate_combo.addItem("Auto (YouTube Music 256kbps Opus / High)", "auto")
        self.bitrate_combo.addItem("320 kbps MP3 (Constant Bitrate)", "320k")
        self.bitrate_combo.addItem("Lossless / FLAC (Studio Quality)", "flac")
        bitrate_layout.addWidget(bitrate_label)
        bitrate_layout.addWidget(self.bitrate_combo)
        bitrate_layout.addStretch()
        opts_layout.addLayout(bitrate_layout)
        layout.addWidget(opts_group)

        layout.addStretch()

        # Ingest Action Button
        self.start_btn = QtWidgets.QPushButton("⬇ Start Music Ingestion")
        self.start_btn.setObjectName("primaryBtn")
        self.start_btn.clicked.connect(self.start_ingestion)
        layout.addWidget(self.start_btn)

    def setup_progress_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_progress)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Status & Track
        self.status_lbl = QtWidgets.QLabel("Status: Idle")
        self.status_lbl.setStyleSheet("font-size: 13px; font-weight: bold; color: #88c0d0;")
        self.track_lbl = QtWidgets.QLabel("No active track")
        self.track_lbl.setStyleSheet("font-size: 15px; font-weight: bold; color: #eceff4;")
        layout.addWidget(self.status_lbl)
        layout.addWidget(self.track_lbl)

        # Progress Bar & Counts
        self.prog_bar = QtWidgets.QProgressBar()
        self.prog_bar.setValue(0)
        self.counts_lbl = QtWidgets.QLabel("Downloaded: 0 | Skipped: 0 | Total: 0")
        self.counts_lbl.setStyleSheet("color: #d8dee9;")
        layout.addWidget(self.prog_bar)
        layout.addWidget(self.counts_lbl)

        # Log Console Header
        log_ctrl_layout = QtWidgets.QHBoxLayout()
        log_title = QtWidgets.QLabel("Live Console Output:")
        log_title.setStyleSheet("font-weight: bold; color: #d8dee9;")
        self.filter_chk = QtWidgets.QCheckBox("Errors & Milestones Only")
        self.filter_chk.toggled.connect(self.refilter_logs)
        copy_btn = QtWidgets.QPushButton("📋 Copy Log")
        copy_btn.clicked.connect(self.copy_log)
        self.cancel_btn = QtWidgets.QPushButton("🛑 Cancel")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.clicked.connect(self.cancel_ingestion)
        self.cancel_btn.setEnabled(False)

        log_ctrl_layout.addWidget(log_title)
        log_ctrl_layout.addStretch()
        log_ctrl_layout.addWidget(self.filter_chk)
        log_ctrl_layout.addWidget(copy_btn)
        log_ctrl_layout.addWidget(self.cancel_btn)
        layout.addLayout(log_ctrl_layout)

        # Console Text Box
        self.console_edit = QtWidgets.QPlainTextEdit()
        self.console_edit.setReadOnly(True)
        self.console_edit.setStyleSheet("background-color: #1e222a; color: #a3be8c; font-family: monospace; font-size: 11px;")
        layout.addWidget(self.console_edit)

        # Quick Launch Actions
        action_layout = QtWidgets.QHBoxLayout()
        open_jf_btn = QtWidgets.QPushButton("🌐 Open Jellyfin Web")
        open_jf_btn.clicked.connect(lambda: subprocess.Popen(["xdg-open", self.config.get("jellyfinWebUrl", "")]))
        open_folder_btn = QtWidgets.QPushButton("📂 Open Music Folder")
        open_folder_btn.clicked.connect(lambda: subprocess.Popen(["xdg-open", self.config.get("musicFolderUrl", "")]))
        action_layout.addWidget(open_jf_btn)
        action_layout.addWidget(open_folder_btn)
        layout.addLayout(action_layout)

        self.all_log_lines = []

    def setup_settings_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_settings)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        desc = QtWidgets.QLabel("Configure the remote Jellyfin server target and SSH execution parameters.")
        desc.setStyleSheet("color: #8fbcbb;")
        layout.addWidget(desc)

        form = QtWidgets.QFormLayout()
        form.setSpacing(10)

        self.host_input = QtWidgets.QLineEdit(self.config.get("serverHost", ""))
        self.jellyfin_url_input = QtWidgets.QLineEdit(self.config.get("jellyfinWebUrl", ""))
        self.folder_url_input = QtWidgets.QLineEdit(self.config.get("musicFolderUrl", ""))
        self.script_path_input = QtWidgets.QLineEdit(self.config.get("remoteScriptPath", ""))
        self.default_user_input = QtWidgets.QLineEdit(self.config.get("defaultUser", ""))

        form.addRow("SSH Server Host / IP:", self.host_input)
        form.addRow("Jellyfin Web URL:", self.jellyfin_url_input)
        form.addRow("Music Folder URL (SFTP):", self.folder_url_input)
        form.addRow("Remote Ingest Script Path:", self.script_path_input)
        form.addRow("Default Jellyfin Account Name:", self.default_user_input)
        layout.addLayout(form)

        layout.addStretch()

        btns_layout = QtWidgets.QHBoxLayout()
        test_btn = QtWidgets.QPushButton("🔄 Test & Sync Accounts")
        test_btn.clicked.connect(self.load_users_from_server)
        save_btn = QtWidgets.QPushButton("💾 Save Settings")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self.save_config)
        btns_layout.addWidget(test_btn)
        btns_layout.addWidget(save_btn)
        layout.addLayout(btns_layout)

    def update_link_count(self):
        lines = [l.strip() for l in self.url_edit.toPlainText().split("\n") if l.strip()]
        self.link_badge.setText(f"{len(lines)} links queued")

    def clear_input(self):
        self.url_edit.clear()
        self.update_link_count()

    def update_routing_visibility(self):
        self.pl_combo.setVisible(self.radio_existing_pl.isChecked())
        self.pl_new_input.setVisible(self.radio_new_pl.isChecked())

    def load_users_from_server(self):
        self.user_status_label.setText("Connecting to server...")
        try:
            host = self.config.get("serverHost", "192.168.1.159")
            script = self.config.get("remoteScriptPath", "~/spotdl/ingest.py")
            res = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", host, f"python3 {script} --list-users"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6
            )
            if res.returncode == 0:
                data = json.loads(res.stdout)
                self.users = data.get("users", [])
                self.render_user_buttons()
                self.user_status_label.setText("")
            else:
                self.user_status_label.setText("⚠️ Failed to connect (check Settings ⚙️)")
        except Exception as e:
            self.user_status_label.setText(f"⚠️ Connection error: {e}")

    def render_user_buttons(self):
        # Clear existing buttons
        while self.user_btn_layout.count():
            item = self.user_btn_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        if not self.users:
            lbl = QtWidgets.QLabel("No Jellyfin users found.")
            self.user_btn_layout.addWidget(lbl)
            return

        default_user_name = (self.config.get("defaultUser") or "").strip().lower()
        selected_index = 0
        for i, user in enumerate(self.users):
            if default_user_name and user.get("name", "").lower() == default_user_name:
                selected_index = i
                break

        self.user_buttons = []
        for i, user in enumerate(self.users):
            is_shared = user.get("is_shared", False)
            icon = "👥" if is_shared else "👤"
            is_def = bool(default_user_name and user.get("name", "").lower() == default_user_name)
            name_label = f"{user.get('name')}{' (Default)' if is_def else ''}"
            btn = QtWidgets.QPushButton(f"{icon} {name_label}")
            btn.setCheckable(True)
            if i == selected_index:
                btn.setChecked(True)
                self.selected_user = user
                self.populate_playlists(user)

            btn.clicked.connect(lambda checked, u=user, b=btn: self.on_user_selected(u, b))
            self.user_btn_layout.addWidget(btn)
            self.user_buttons.append(btn)

    def on_user_selected(self, user, clicked_btn):
        self.selected_user = user
        for btn in self.user_buttons:
            btn.setChecked(btn == clicked_btn)
        self.populate_playlists(user)

    def populate_playlists(self, user):
        self.pl_combo.clear()
        playlists = user.get("playlists", [])
        if playlists:
            self.pl_combo.addItems(playlists)
            self.radio_existing_pl.setEnabled(True)
        else:
            self.radio_existing_pl.setEnabled(False)
            if self.radio_existing_pl.isChecked():
                self.radio_lib_only.setChecked(True)

    def start_ingestion(self):
        urls = [l.strip() for l in self.url_edit.toPlainText().split("\n") if l.strip()]
        if not urls:
            QtWidgets.QMessageBox.warning(self, "No URLs", "Please enter at least one Spotify or YouTube Music URL.")
            return

        if not self.selected_user:
            QtWidgets.QMessageBox.warning(self, "No User", "Please select a target Jellyfin user account.")
            return

        # Prepare payload
        items = [{"url": u, "type": "url"} for u in urls]
        payload = {
            "items": items,
            "urls": urls,
            "user_id": self.selected_user.get("id"),
            "user_name": self.selected_user.get("name"),
            "is_shared": self.selected_user.get("is_shared", False),
            "bitrate": self.bitrate_combo.currentData(),
            "song_action": "none",
            "song_playlist_name": "",
            "playlist_name": ""
        }

        if self.radio_existing_pl.isChecked():
            payload["song_action"] = "existing_playlist"
            payload["song_playlist_name"] = self.pl_combo.currentText()
            payload["playlist_name"] = self.pl_combo.currentText()
        elif self.radio_new_pl.isChecked():
            name = self.pl_new_input.text().strip()
            if not name:
                QtWidgets.QMessageBox.warning(self, "Playlist Name", "Please enter a name for the new playlist.")
                return
            payload["song_action"] = "new_playlist"
            payload["song_playlist_name"] = name
            payload["playlist_name"] = name

        # Switch to Progress Tab
        self.tabs.setCurrentWidget(self.tab_progress)
        self.status_lbl.setText(f"Status: Ingesting {len(urls)} items for {self.selected_user.get('name')}...")
        self.prog_bar.setValue(0)
        self.console_edit.clear()
        self.all_log_lines.clear()
        self.cancel_btn.setEnabled(True)
        self.start_btn.setEnabled(False)

        self.worker = IngestWorker(
            self.config.get("serverHost", "192.168.1.159"),
            self.config.get("remoteScriptPath", "~/spotdl/ingest.py"),
            payload
        )
        self.worker.progress_signal.connect(self.prog_bar.setValue)
        self.worker.track_signal.connect(self.track_lbl.setText)
        self.worker.counts_signal.connect(self.counts_lbl.setText)
        self.worker.status_signal.connect(self.status_lbl.setText)
        self.worker.log_signal.connect(self.append_log)
        self.worker.finished_signal.connect(self.on_ingest_finished)
        self.worker.start()

    def append_log(self, text, is_important):
        self.all_log_lines.append((text, is_important))
        if not self.filter_chk.isChecked() or is_important:
            self.console_edit.appendPlainText(text)

    def refilter_logs(self):
        self.console_edit.clear()
        for text, is_imp in self.all_log_lines:
            if not self.filter_chk.isChecked() or is_imp:
                self.console_edit.appendPlainText(text)

    def copy_log(self):
        text = self.console_edit.toPlainText()
        if text:
            QtWidgets.QApplication.clipboard().setText(text)
            QtWidgets.QMessageBox.information(self, "Copied", "Console logs copied to clipboard!")

    def cancel_ingestion(self):
        if self.worker:
            self.status_lbl.setText("Status: Cancelling download...")
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)

    def on_ingest_finished(self, success, msg):
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if success:
            self.prog_bar.setValue(100)
            self.status_lbl.setText("Status: Complete! 🎉")
            QtWidgets.QMessageBox.information(self, "Download Complete", msg)
        else:
            self.status_lbl.setText("Status: Ingestion stopped or encountered an issue.")
            QtWidgets.QMessageBox.warning(self, "Ingestion Notice", msg)

def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("Jellyfin Music Downloader")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_() if hasattr(app, 'exec_') else app.exec())

if __name__ == "__main__":
    main()
