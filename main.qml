import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland

PanelWindow {
    id: root

    WlrLayershell.layer: WlrLayer.Top
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.OnDemand

    color: "transparent"

    implicitWidth: 680
    implicitHeight: root.activeTab === "settings" ? 600 : (root.activeTab === "download" ? 580 : (root.appState === "complete" || root.appState === "error" ? 440 : (root.countTracks > 0 ? 580 : 520)))

    Behavior on implicitHeight {
        NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
    }

    // UI States: "input", "downloading", "complete", "error"
    property string appState: "input"
    property string currentStage: "discovery" // "discovery", "downloading", "refreshing"
    property string rawInputText: ""
    property var parsedItems: []
    property int countPlaylists: 0
    property int countArtists: 0
    property int countAlbums: 0
    property int countTracks: 0

    // Configurable Server & Path Settings (loaded dynamically from config.json)
    property string serverHost: "server-ip-or-hostname"
    property string jellyfinWebUrl: "http://localhost:8096"
    property string musicFolderUrl: "sftp://server-ip-or-hostname/mnt/media/music"
    property string remoteScriptPath: "~/spotdl/ingest.py"
    property string defaultUser: ""

    // UX Enhancements
    property string toastMessage: ""
    property bool toastVisible: false
    property bool filterErrorsOnly: false
    property string audioBitrate: "auto" // "auto", "320k", "flac"
    property bool isDraggingOver: false

    Timer {
        id: toastTimer
        interval: 3200
        onTriggered: root.toastVisible = false
    }

    function showToast(msg) {
        root.toastMessage = msg;
        root.toastVisible = true;
        toastTimer.restart();
    }

    // Dynamic Users & Playlists (fetched from Jellyfin via ingest.py --list-users)
    property var usersList: []
    property int selectedUserIndex: 0
    readonly property var currentUser: (usersList && usersList.length > selectedUserIndex) ? usersList[selectedUserIndex] : {"name": "No User Selected", "id": "", "is_shared": false, "playlists": []}

    // Song Routing
    property string songAction: "none" // "none", "existing_playlist", "new_playlist"
    property string selectedExistingPlaylist: ""
    property string newPlaylistName: ""

    // Ingest Progress Tracking
    property int batchTotal: 0
    property int batchCurrentIndex: 0
    property string batchCurrentType: ""
    property string batchCurrentUrl: ""

    property int itemProgressPct: 0
    property int itemCurrentTrackIndex: 0
    property int itemTotalTracksCount: 0
    property int itemSkippedCount: 0
    property int itemDownloadedCount: 0
    property string currentTrackName: "Scanning Spotify catalog..."
    property string statusMessage: "Initializing connection to server..."
    property string completionMessage: ""
    property string errorMessage: ""
    property var logLines: []
    property int elapsedSeconds: 0
    property bool isServerTaskActive: false
    property string activeTab: "input" // "input" or "download"

    // Process to copy text to Wayland clipboard
    Process {
        id: copyProc
        property string copyText: ""
        command: ["bash", "-c", "printf '%s' \"$1\" | wl-copy", "_", copyText]
    }

    function copyLogsToClipboard() {
        var str = "";
        for (var i = 0; i < logModel.count; i++) {
            var item = logModel.get(i);
            if (!root.filterErrorsOnly || item.important) {
                str += (str ? "\n" : "") + item.message;
            }
        }
        if (str) {
            copyProc.copyText = str;
            copyProc.running = true;
            showToast("📋 Logs copied to clipboard!");
        }
    }

    // Preferences Persistence (loads/saves user selection & bitrate)
    Process {
        id: loadPrefProc
        command: ["bash", "-c", "cat ~/.local/state/omarchy/extensions/jellyfin-music-app/preferences.json 2>/dev/null || echo '{}'"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    var data = JSON.parse(this.text);
                    if (data) {
                        if (data.user_name && root.usersList) {
                            for (var i = 0; i < root.usersList.length; i++) {
                                if (root.usersList[i].name === data.user_name) {
                                    root.selectedUserIndex = i;
                                    break;
                                }
                            }
                        }
                        if (data.song_action) root.songAction = data.song_action;
                        if (data.bitrate) root.audioBitrate = data.bitrate;
                    }
                } catch (e) {}
            }
        }
    }

    Process {
        id: savePrefProc
        property string prefJson: ""
        command: [
            "bash", "-c",
            "mkdir -p ~/.local/state/omarchy/extensions/jellyfin-music-app && echo \"$1\" > ~/.local/state/omarchy/extensions/jellyfin-music-app/preferences.json",
            "_",
            prefJson
        ]
    }

    function savePreferences() {
        if (!root.currentUser || !root.currentUser.name || root.currentUser.name === "No User Selected") return;
        var data = {
            "user_name": root.currentUser.name,
            "song_action": root.songAction,
            "bitrate": root.audioBitrate
        };
        savePrefProc.prefJson = JSON.stringify(data);
        savePrefProc.running = true;
    }

    // Config Persistence (loads/saves server host, URLs, paths)
    Process {
        id: loadConfigProc
        command: [
            "bash", "-c",
            "cat ~/.config/omarchy/extensions/jellyfin-music-app/config.json 2>/dev/null || cat ~/.local/state/omarchy/extensions/jellyfin-music-app/config.json 2>/dev/null || echo '{}'"
        ]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    var cfg = JSON.parse(this.text);
                    if (cfg) {
                        if (cfg.serverHost) root.serverHost = cfg.serverHost;
                        if (cfg.jellyfinWebUrl) root.jellyfinWebUrl = cfg.jellyfinWebUrl;
                        if (cfg.musicFolderUrl) root.musicFolderUrl = cfg.musicFolderUrl;
                        if (cfg.remoteScriptPath) root.remoteScriptPath = cfg.remoteScriptPath;
                        if (cfg.defaultUser) root.defaultUser = cfg.defaultUser;

                        if (root.serverHost && root.serverHost !== "server-ip-or-hostname") {
                            root.fetchUsers();
                            checkActiveJobProc.running = true;
                        }
                    }
                } catch (e) {}
            }
        }
    }

    Process {
        id: saveConfigProc
        property string configJson: ""
        command: [
            "bash", "-c",
            "mkdir -p ~/.config/omarchy/extensions/jellyfin-music-app ~/.local/state/omarchy/extensions/jellyfin-music-app && echo \"$1\" > ~/.config/omarchy/extensions/jellyfin-music-app/config.json && cp ~/.config/omarchy/extensions/jellyfin-music-app/config.json ~/.local/state/omarchy/extensions/jellyfin-music-app/config.json",
            "_",
            configJson
        ]
    }

    function saveConfig() {
        var cfg = {
            "serverHost": root.serverHost.trim(),
            "jellyfinWebUrl": root.jellyfinWebUrl.trim(),
            "musicFolderUrl": root.musicFolderUrl.trim(),
            "remoteScriptPath": root.remoteScriptPath.trim(),
            "defaultUser": root.defaultUser.trim()
        };
        saveConfigProc.configJson = JSON.stringify(cfg, null, 2);
        saveConfigProc.running = true;
        showToast("⚙️ Settings saved successfully!");
    }

    Timer {
        id: elapsedTimer
        interval: 1000
        repeat: true
        running: root.appState === "downloading"
        onTriggered: root.elapsedSeconds += 1
    }

    function formatTime(s) {
        var m = Math.floor(s / 60);
        var sec = s % 60;
        return (m < 10 ? "0" : "") + m + ":" + (sec < 10 ? "0" : "") + sec;
    }

    ListModel {
        id: logModel
    }

    function addLog(line) {
        var isImp = (line.indexOf("⚠️") !== -1 || line.indexOf("ERROR") !== -1 || line.indexOf("🛑") !== -1 || line.indexOf("✔") !== -1 || line.indexOf("▶") !== -1 || line.indexOf("COMPLETE") !== -1 || line.indexOf("FATAL") !== -1 || line.indexOf("Finished") !== -1);
        logModel.append({ "message": line, "important": isImp });
        if (logModel.count > 250) logModel.remove(0);
        logList.positionViewAtEnd();
    }

    function parseInput(text) {
        root.rawInputText = text;
        var lines = text.split(/[\r\n\s,]+/);
        var items = [];
        var plCount = 0;
        var artCount = 0;
        var albCount = 0;
        var trkCount = 0;

        for (var i = 0; i < lines.length; i++) {
            var url = lines[i].trim();
            if (!url || url.indexOf("http") !== 0) continue;

            var type = "track";
            if (url.indexOf("/playlist") !== -1 || url.indexOf("playlist?list") !== -1) {
                type = "playlist";
                plCount++;
            } else if (url.indexOf("/artist") !== -1) {
                type = "artist";
                artCount++;
            } else if (url.indexOf("/album") !== -1) {
                type = "album";
                albCount++;
            } else {
                type = "track";
                trkCount++;
            }
            items.push({ "url": url, "type": type });
        }

        root.parsedItems = items;
        root.countPlaylists = plCount;
        root.countArtists = artCount;
        root.countAlbums = albCount;
        root.countTracks = trkCount;
    }

    function resetForm() {
        root.appState = "input";
        root.currentStage = "discovery";
        root.rawInputText = "";
        urlTextArea.text = "";
        root.parsedItems = [];
        root.songAction = "none";
        root.newPlaylistName = "";
        root.batchTotal = 0;
        root.batchCurrentIndex = 0;
        root.itemProgressPct = 0;
        root.itemCurrentTrackIndex = 0;
        root.itemTotalTracksCount = 0;
        root.itemSkippedCount = 0;
        root.itemDownloadedCount = 0;
        root.currentTrackName = "Scanning Spotify catalog...";
        root.statusMessage = "Ready";
        root.elapsedSeconds = 0;
        root.logLines = [];
        fetchUsers();
        readClipboard();
    }

    function startIngestion() {
        if (root.parsedItems.length === 0) return;

        root.activeTab = "download";
        root.appState = "downloading";
        root.currentStage = "discovery";
        root.batchTotal = root.parsedItems.length;
        root.batchCurrentIndex = 1;
        root.itemProgressPct = 0;
        root.itemCurrentTrackIndex = 0;
        root.itemTotalTracksCount = 0;
        root.itemSkippedCount = 0;
        root.itemDownloadedCount = 0;
        root.elapsedSeconds = 0;
        root.currentTrackName = "Contacting Spotify metadata service...";
        root.statusMessage = "🔍 Resolving albums and tracklist...";
        root.logLines = [];

        if ((!root.currentUser || !root.currentUser.id || root.currentUser.name === "No User Selected") && root.usersList && root.usersList.length > 0) {
            var foundIdx = 0;
            if (root.defaultUser) {
                for (var u = 0; u < root.usersList.length; u++) {
                    if (root.usersList[u].name.toLowerCase() === root.defaultUser.toLowerCase()) {
                        foundIdx = u;
                        break;
                    }
                }
            }
            root.selectedUserIndex = foundIdx;
        }

        var songPl = "";
        if (root.songAction === "new_playlist") songPl = root.newPlaylistName.trim();
        else if (root.songAction === "existing_playlist") songPl = root.selectedExistingPlaylist;

        var payload = {
            "user_id": root.currentUser.id,
            "user_name": root.currentUser.name,
            "items": root.parsedItems,
            "song_action": root.songAction,
            "song_playlist_name": songPl,
            "is_shared": (root.currentUser && root.currentUser.is_shared === true),
            "bitrate": root.audioBitrate
        };
        root.savePreferences();

        addLog("▶ Batch Ingest job started on " + root.serverHost + " for " + root.currentUser.name.toUpperCase());
        addLog("▶ Queueing " + root.parsedItems.length + " items for download...");

        batchProc.payloadJson = JSON.stringify(payload);
        batchProc.running = true;
    }

    function cancelIngestion() {
        if (batchProc.running) batchProc.running = false;
        stopRemoteProc.running = true;
        root.appState = "input";
        root.statusMessage = "Download cancelled by user.";
        root.addLog("🛑 Ingestion cancelled by user. Terminating download cleanly...");
        root.showToast("🛑 Download cancelled");
    }

    // Process to gracefully terminate remote ingest processes and containers
    Process {
        id: stopRemoteProc
        command: [
            "bash", "-c",
            "ssh -o ConnectTimeout=4 \"$1\" \"pkill -INT -f 'spotdl/ingest.py' || pkill -TERM -f 'spotdl/ingest.py' || true; sleep 1; docker stop $(docker ps -q --filter ancestor=spotdl-custom:latest) 2>/dev/null || true\"",
            "_",
            root.serverHost
        ]
    }

    function readClipboard() {
        clipProc.running = true;
    }

    function fetchUsers() {
        if (usersProc.running) usersProc.running = false;
        usersProc.running = true;
    }

    // Process to fetch dynamic users & playlists from server
    Process {
        id: usersProc
        command: [
            "bash", "-c",
            "ssh -o ConnectTimeout=5 \"$1\" \"python3 $2 --list-users\"",
            "_",
            root.serverHost,
            root.remoteScriptPath
        ]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                try {
                    var data = JSON.parse(this.text);
                    if (data && data.users && data.users.length > 0) {
                        root.usersList = data.users;
                        loadPrefProc.running = true;

                        if (root.defaultUser) {
                            for (var i = 0; i < root.usersList.length; i++) {
                                if (root.usersList[i].name.toLowerCase() === root.defaultUser.toLowerCase()) {
                                    root.selectedUserIndex = i;
                                    break;
                                }
                            }
                        }

                        if (root.currentUser.playlists && root.currentUser.playlists.length > 0) {
                            root.selectedExistingPlaylist = root.currentUser.playlists[0];
                        }
                    }
                } catch (e) {}
            }
        }
    }

    // Process to read clipboard
    Process {
        id: clipProc
        command: ["wl-paste"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var text = (this.text || "").trim();
                if (text.indexOf("spotify.com") !== -1 || text.indexOf("youtube.com") !== -1 || text.indexOf("soundcloud.com") !== -1 || text.indexOf("bandcamp.com") !== -1) {
                    urlTextArea.text = text;
                    root.parseInput(text);
                    root.showToast("📋 Auto-loaded links from clipboard");
                }
            }
        }
    }

    // Batch download runner
    Process {
        id: batchProc
        property string payloadJson: ""

        command: [
            "bash", "-c",
            "mkdir -p ~/.local/state/omarchy/extensions/jellyfin-music-app && ssh -o ConnectTimeout=10 \"$1\" \"python3 -u $2 --batch '$3'\" 2>&1 | python3 -u ~/.local/state/omarchy/extensions/jellyfin-music-app/log-filter.py",
            "_",
            root.serverHost,
            root.remoteScriptPath,
            payloadJson
        ]

        stdout: SplitParser {
            onRead: function(line) {
                var s = String(line).trim();
                if (!s) return;

                if (s.indexOf("BATCH_ITEM:") === 0) {
                    var parts = s.substring(11).trim().split("|");
                    if (parts.length >= 2) {
                        root.batchCurrentIndex = parseInt(parts[0]) || 1;
                        root.batchTotal = parseInt(parts[1]) || 1;
                    }
                    if (parts.length >= 3) root.batchCurrentType = parts[2];
                    if (parts.length >= 4) root.batchCurrentUrl = parts[3];
                    root.currentStage = "discovery";
                    root.itemProgressPct = 0;
                    root.itemCurrentTrackIndex = 0;
                    root.itemTotalTracksCount = 0;
                    root.currentTrackName = "Scanning Spotify catalog for " + root.batchCurrentType + "...";
                } else if (s.indexOf("STAGE:") === 0) {
                    root.currentStage = s.substring(6).trim();
                } else if (s.indexOf("PROGRESS:") === 0) {
                    var pParts = s.substring(9).trim().split("|");
                    if (pParts.length >= 1) root.itemProgressPct = Math.min(Math.max(parseInt(pParts[0]) || 0, 0), 100);
                    if (pParts.length >= 2) root.itemCurrentTrackIndex = parseInt(pParts[1]) || 0;
                    if (pParts.length >= 3 && parseInt(pParts[2]) > 0) root.itemTotalTracksCount = parseInt(pParts[2]);
                    root.currentStage = "downloading";
                } else if (s.indexOf("COUNTS:") === 0) {
                    var cParts = s.substring(7).trim().split("|");
                    if (cParts.length >= 2) {
                        root.itemSkippedCount = parseInt(cParts[0]) || 0;
                        root.itemDownloadedCount = parseInt(cParts[1]) || 0;
                    }
                } else if (s.indexOf("TRACK:") === 0) {
                    root.currentTrackName = s.substring(6).trim();
                    root.currentStage = "downloading";
                } else if (s.indexOf("STATUS:") === 0) {
                    root.statusMessage = s.substring(7).trim();
                } else if (s.indexOf("TOTAL:") === 0) {
                    root.itemTotalTracksCount = parseInt(s.substring(6).trim()) || 0;
                    root.currentStage = "downloading";
                } else if (s.indexOf("LOG:") === 0) {
                    root.addLog(s.substring(4).trim());
                } else if (s.indexOf("COMPLETE:") === 0) {
                    root.completionMessage = s.substring(9).trim();
                    root.itemProgressPct = 100;
                    root.appState = "complete";
                    notifyProc.notifyTitle = "Jellyfin Music Ingest Complete";
                    notifyProc.notifyBody = (root.completionMessage || "Imported batch to Jellyfin") + "\n🎶 Synced karaoke lyrics ready!";
                    notifyProc.running = true;
                } else if (s.indexOf("FATAL_ERROR:") === 0 || (s.indexOf("ERROR:") === 0 && (s.indexOf("Invalid arguments") !== -1 || s.indexOf("Traceback") !== -1))) {
                    root.errorMessage = s.replace(/^(FATAL_)?ERROR:\s*/, "");
                    root.appState = "error";
                } else if (s.indexOf("ERROR:") === 0) {
                    root.addLog("⚠️ " + s.substring(6).trim());
                } else {
                    root.addLog(s);
                }
            }
        }

        onExited: function(code, status) {
            if (root.appState === "downloading") {
                if (code === 0) {
                    root.itemProgressPct = 100;
                    root.appState = "complete";
                    notifyProc.notifyTitle = "Jellyfin Music Ingest Complete";
                    notifyProc.notifyBody = (root.completionMessage || "Imported batch to Jellyfin") + "\n🎶 Synced karaoke lyrics ready!";
                    notifyProc.running = true;
                } else {
                    root.errorMessage = "Ingest process exited with status " + code + "\n(Logs: ~/.local/state/omarchy/extensions/jellyfin-music-app/ingest.log)";
                    root.appState = "error";
                }
            }
        }
    }

    // Desktop Notification Process
    Process {
        id: notifyProc
        property string notifyTitle: "Jellyfin Music Ingest Complete"
        property string notifyBody: ""
        command: ["notify-send", "-i", "audio-headphones", notifyTitle, notifyBody]
    }

    // Process to open Jellyfin Web in default browser
    Process {
        id: openJellyfinProc
        command: ["bash", "-c", "xdg-open \"$1\"", "_", root.jellyfinWebUrl]
    }

    // Process to open remote Music folder via SFTP
    Process {
        id: openFolderProc
        command: ["bash", "-c", "xdg-open \"$1\"", "_", root.musicFolderUrl]
    }

    // Process to open local ingest log file
    Process {
        id: openLogProc
        command: ["bash", "-c", "xdg-open ~/.local/state/omarchy/extensions/jellyfin-music-app/ingest.log"]
    }

    // Unified process to query server active state and live logs simultaneously
    Process {
        id: checkActiveJobProc
        command: [
            "bash", "-c",
            "ssh -o ConnectTimeout=3 -o BatchMode=yes \"$1\" \"cat ~/spotdl/active_state.json 2>/dev/null || echo '{\\\"running\\\":false}'; echo '---LOGS---'; tail -n 25 ~/spotdl/ingest.log 2>/dev/null\"",
            "_",
            root.serverHost
        ]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var raw = (this.text || "").trim();
                var parts = raw.split("---LOGS---");
                var stateJson = (parts[0] || "").trim();
                var logText = parts.length > 1 ? (parts[1] || "").trim() : "";

                try {
                    var d = JSON.parse(stateJson);
                    if (d && d.running === true) {
                        root.isServerTaskActive = true;
                        root.appState = "downloading";
                        if (d.total) root.itemTotalTracksCount = d.total;
                        if (d.current) root.itemCurrentTrackIndex = d.current;
                        if (d.skipped !== undefined) root.itemSkippedCount = d.skipped;
                        if (d.downloaded !== undefined) root.itemDownloadedCount = d.downloaded;
                        if (d.pct !== undefined) root.itemProgressPct = d.pct;
                        if (d.track) root.currentTrackName = d.track;
                        if (d.status) root.statusMessage = d.status;
                        root.currentStage = "downloading";

                        if (root.activeTab === "input" && urlTextArea.text.trim() === "") {
                            root.activeTab = "download";
                        }
                    } else {
                        root.isServerTaskActive = false;
                        if (d && d.status && d.status.indexOf("Finished") !== -1 && root.appState === "downloading") {
                            root.appState = "complete";
                            root.completionMessage = d.status;
                        }
                    }
                } catch (e) {}

                if (logText && !batchProc.running) {
                    var lines = logText.split("\n");
                    if (logModel.count === 0) {
                        for (var i = 0; i < lines.length; i++) {
                            var s = lines[i].trim();
                            if (!s) continue;
                            if (s.indexOf("TRACK:") === 0 || s.indexOf("COUNTS:") === 0 || s.indexOf("PROGRESS:") === 0 || s.indexOf("STATUS:") === 0 || s.indexOf("STAGE:") === 0 || s.indexOf("TOTAL:") === 0 || s.indexOf("BATCH_ITEM:") === 0) {
                                continue;
                            }
                            var clean = s.indexOf("LOG:") === 0 ? s.substring(4).trim() : s;
                            logModel.append({ "message": clean });
                        }
                    } else {
                        for (var i = 0; i < lines.length; i++) {
                            var s = lines[i].trim();
                            if (!s) continue;
                            if (s.indexOf("TRACK:") === 0 || s.indexOf("COUNTS:") === 0 || s.indexOf("PROGRESS:") === 0 || s.indexOf("STATUS:") === 0 || s.indexOf("STAGE:") === 0 || s.indexOf("TOTAL:") === 0 || s.indexOf("BATCH_ITEM:") === 0) {
                                continue;
                            }
                            var clean = s.indexOf("LOG:") === 0 ? s.substring(4).trim() : s;
                            var exists = false;
                            for (var j = Math.max(0, logModel.count - 25); j < logModel.count; j++) {
                                if (logModel.get(j).message === clean) {
                                    exists = true;
                                    break;
                                }
                            }
                            if (!exists) {
                                logModel.append({ "message": clean });
                                if (logModel.count > 150) logModel.remove(0);
                            }
                        }
                    }
                    logList.positionViewAtEnd();
                }
            }
        }
    }

    // Periodic poller to detect background server tasks
    Timer {
        id: serverPoller
        interval: 2500
        repeat: true
        running: true
        onTriggered: {
            if (!batchProc.running && !checkActiveJobProc.running) {
                checkActiveJobProc.running = true;
            }
        }
    }

    Component.onCompleted: {
        loadConfigProc.running = true;
        loadPrefProc.running = true;
        readClipboard();
    }

    Item {
        focus: true
        Keys.onEscapePressed: {
            Qt.quit();
        }
    }

    // Main Dialog Card
    Rectangle {
        id: card
        anchors.fill: parent
        color: "#242933"
        radius: 16
        border.color: root.isDraggingOver ? "#88c0d0" : "#3b4252"
        border.width: root.isDraggingOver ? 2 : 1

        DropArea {
            anchors.fill: parent
            onEntered: function(drag) {
                root.isDraggingOver = true;
                drag.acceptProposedAction();
            }
            onExited: {
                root.isDraggingOver = false;
            }
            onDropped: function(drop) {
                root.isDraggingOver = false;
                var textToAppend = "";
                if (drop.hasUrls) {
                    for (var i = 0; i < drop.urls.length; i++) {
                        var u = drop.urls[i].toString();
                        if (u.indexOf("file://") === 0) continue;
                        textToAppend += (textToAppend ? "\n" : "") + u;
                    }
                }
                if (drop.hasText && !textToAppend) {
                    textToAppend = drop.text;
                }
                if (textToAppend) {
                    if (urlTextArea.text.trim().length > 0) {
                        urlTextArea.text = urlTextArea.text.trim() + "\n" + textToAppend.trim();
                    } else {
                        urlTextArea.text = textToAppend.trim();
                    }
                    root.parseInput(urlTextArea.text);
                    root.showToast("📥 Links added via Drag & Drop!");
                    root.activeTab = "input";
                }
            }
        }

        // Inline Toast Notification Banner
        Rectangle {
            id: toastBanner
            anchors.top: parent.top
            anchors.topMargin: 12
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(card.width - 60, toastContent.width + 28)
            height: 30
            radius: 15
            color: "#3b4252"
            border.color: "#88c0d0"
            border.width: 1
            z: 100
            opacity: root.toastVisible ? 1.0 : 0.0
            visible: opacity > 0.0

            Behavior on opacity {
                NumberAnimation { duration: 200 }
            }

            RowLayout {
                id: toastContent
                anchors.centerIn: parent
                spacing: 6
                Text {
                    text: root.toastMessage
                    font.pixelSize: 11
                    font.bold: true
                    color: "#eceff4"
                }
            }
        }

        ColumnLayout {
                anchors.fill: parent
                anchors.margins: 26
                spacing: 16

                // -------------------------------------------------------------
                // Header
                // -------------------------------------------------------------
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14

                    Rectangle {
                        width: 42
                        height: 42
                        radius: 12
                        color: "#2e3440"
                        border.color: "#434c5e"
                        border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: "󰝚"
                            font.pixelSize: 22
                            color: "#88c0d0"
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            text: "Jellyfin Music Ingest"
                            font.pixelSize: 18
                            font.bold: true
                            color: "#eceff4"
                        }

                        Text {
                            text: "Bulk Spotify & YouTube Music Ingestion Engine"
                            font.pixelSize: 12
                            color: "#8fbcbb"
                        }
                    }

                    Rectangle {
                        width: 32
                        height: 32
                        radius: 16
                        color: closeBtnMa.containsMouse ? "#4c566a" : "transparent"

                        Text {
                            anchors.centerIn: parent
                            text: "✕"
                            font.pixelSize: 14
                            color: "#d8dee9"
                        }

                        MouseArea {
                            id: closeBtnMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: Qt.quit()
                        }
                    }
                }

                // Tab Navigation Switcher
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    Rectangle {
                        height: 32
                        radius: 6
                        color: root.activeTab === "input" ? "#3b4252" : "#242933"
                        border.color: root.activeTab === "input" ? "#88c0d0" : "#3b4252"
                        border.width: 1
                        width: addTabTxt.width + 24

                        RowLayout {
                            id: addTabTxt
                            anchors.centerIn: parent
                            spacing: 6
                            Text {
                                text: "📥"
                                font.pixelSize: 12
                            }
                            Text {
                                text: "Add Music"
                                font.pixelSize: 11
                                font.bold: root.activeTab === "input"
                                color: root.activeTab === "input" ? "#eceff4" : "#8fbcbb"
                            }
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.activeTab = "input";
                            }
                        }
                    }

                    Rectangle {
                        height: 32
                        radius: 6
                        color: root.activeTab === "download" ? "#3b4252" : "#242933"
                        border.color: root.activeTab === "download" ? "#88c0d0" : "#3b4252"
                        border.width: 1
                        width: dlTabTxt.width + 24

                        RowLayout {
                            id: dlTabTxt
                            anchors.centerIn: parent
                            spacing: 6
                            Rectangle {
                                width: 8
                                height: 8
                                radius: 4
                                color: (root.isServerTaskActive || batchProc.running) ? "#a3be8c" : "#4c566a"
                            }
                            Text {
                                text: (root.isServerTaskActive || batchProc.running) ? "⚡ Active Task" : "📊 Task Progress"
                                font.pixelSize: 11
                                font.bold: root.activeTab === "download"
                                color: root.activeTab === "download" ? "#eceff4" : ((root.isServerTaskActive || batchProc.running) ? "#a3be8c" : "#6c7a96")
                            }
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.activeTab = "download";
                            }
                        }
                    }

                    Rectangle {
                        height: 32
                        radius: 6
                        color: root.activeTab === "settings" ? "#3b4252" : "#242933"
                        border.color: root.activeTab === "settings" ? "#88c0d0" : "#3b4252"
                        border.width: 1
                        width: cfgTabTxt.width + 24

                        RowLayout {
                            id: cfgTabTxt
                            anchors.centerIn: parent
                            spacing: 6
                            Text {
                                text: "⚙️"
                                font.pixelSize: 12
                            }
                            Text {
                                text: "Settings"
                                font.pixelSize: 11
                                font.bold: root.activeTab === "settings"
                                color: root.activeTab === "settings" ? "#eceff4" : "#8fbcbb"
                            }
                        }
                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                root.activeTab = "settings";
                            }
                        }
                    }

                    Item { Layout.fillWidth: true }
                }

                Rectangle {
                    Layout.fillWidth: true
                    height: 1
                    color: "#3b4252"
                }

                // -------------------------------------------------------------
                // View 1: Input & Bulk Queue
                // -------------------------------------------------------------
                ColumnLayout {
                    visible: root.activeTab === "input" && root.appState !== "complete" && root.appState !== "error"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 14

                    // Bulk URL Text Area
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Text {
                                text: "Paste or Drag Links (Playlists, Artists, Albums, Songs)"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#d8dee9"
                            }

                            Item { Layout.fillWidth: true }

                            Rectangle {
                                visible: urlTextArea.text.trim().length > 0
                                height: 20
                                radius: 4
                                color: clearMa.containsMouse ? "#bf616a" : "#3b4252"
                                width: clearTxt.width + 12

                                Text {
                                    id: clearTxt
                                    anchors.centerIn: parent
                                    text: "✕ Clear"
                                    font.pixelSize: 10
                                    font.bold: true
                                    color: "#eceff4"
                                }

                                MouseArea {
                                    id: clearMa
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        urlTextArea.text = "";
                                        root.parseInput("");
                                    }
                                }
                            }

                            Text {
                                text: root.parsedItems.length > 0 ? (root.parsedItems.length + " links queued") : ""
                                font.pixelSize: 11
                                color: "#88c0d0"
                                font.bold: true
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            height: 80
                            radius: 10
                            color: "#2e3440"
                            border.color: urlTextArea.activeFocus ? "#88c0d0" : "#434c5e"
                            border.width: urlTextArea.activeFocus ? 2 : 1

                            Flickable {
                                id: flick
                                anchors.fill: parent
                                anchors.margins: 10
                                contentWidth: width - 8
                                contentHeight: urlTextArea.contentHeight
                                clip: true

                                ScrollBar.vertical: ScrollBar {
                                    parent: flick.parent
                                    anchors.top: flick.top
                                    anchors.bottom: flick.bottom
                                    anchors.right: flick.right
                                    anchors.rightMargin: 4
                                    policy: ScrollBar.AsNeeded
                                    width: 5
                                }

                                TextArea.flickable: TextArea {
                                    id: urlTextArea
                                    wrapMode: TextEdit.WrapAnywhere
                                    font.pixelSize: 12
                                    color: "#eceff4"
                                    selectByMouse: true
                                    placeholderText: "Paste or drag 1 or more links (one per line)...\nhttps://open.spotify.com/artist/...\nhttps://open.spotify.com/playlist/..."
                                    placeholderTextColor: "#4c566a"
                                    onTextChanged: root.parseInput(text)
                                }
                            }
                        }

                        // Badges Row
                        RowLayout {
                            visible: root.parsedItems.length > 0
                            spacing: 8

                            Rectangle {
                                visible: root.countPlaylists > 0
                                height: 22
                                radius: 4
                                color: "#3b4252"
                                width: plBadgeText.width + 12
                                Text { id: plBadgeText; anchors.centerIn: parent; text: "📋 " + root.countPlaylists + " Playlists"; font.pixelSize: 10; color: "#88c0d0" }
                            }

                            Rectangle {
                                visible: root.countArtists > 0
                                height: 22
                                radius: 4
                                color: "#3b4252"
                                width: artBadgeText.width + 12
                                Text { id: artBadgeText; anchors.centerIn: parent; text: "👤 " + root.countArtists + " Artists"; font.pixelSize: 10; color: "#b48ead" }
                            }

                            Rectangle {
                                visible: root.countAlbums > 0
                                height: 22
                                radius: 4
                                color: "#3b4252"
                                width: albBadgeText.width + 12
                                Text { id: albBadgeText; anchors.centerIn: parent; text: "💿 " + root.countAlbums + " Albums"; font.pixelSize: 10; color: "#ebcb8b" }
                            }

                            Rectangle {
                                visible: root.countTracks > 0
                                height: 22
                                radius: 4
                                color: "#3b4252"
                                width: trkBadgeText.width + 12
                                Text { id: trkBadgeText; anchors.centerIn: parent; text: "🎶 " + root.countTracks + " Loose Tracks"; font.pixelSize: 10; color: "#a3be8c" }
                            }
                        }
                    }

                    // Dynamic User Selection
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6

                        Text {
                            text: "Target Jellyfin Account (Owner):"
                            font.pixelSize: 12
                            font.bold: true
                            color: "#d8dee9"
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                visible: !root.usersList || root.usersList.length === 0
                                height: 34
                                radius: 6
                                color: "#2e3440"
                                border.color: emptyRetryMa.containsMouse ? "#88c0d0" : "#434c5e"
                                border.width: 1
                                width: emptyTxtRow.width + 20

                                RowLayout {
                                    id: emptyTxtRow
                                    anchors.centerIn: parent
                                    spacing: 6
                                    Text {
                                        text: usersProc.running ? "🔄 Connecting to server (" + root.serverHost + ")..." : "⚠️ Offline / Not Connected — Tap to Open Settings ⚙️"
                                        font.pixelSize: 11
                                        color: usersProc.running ? "#8fbcbb" : "#ebcb8b"
                                    }
                                }

                                MouseArea {
                                    id: emptyRetryMa
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        if (root.serverHost && root.serverHost !== "server-ip-or-hostname") {
                                            root.fetchUsers();
                                            root.showToast("🔄 Retrying connection to " + root.serverHost + "...");
                                        } else {
                                            root.activeTab = "settings";
                                        }
                                    }
                                }
                            }

                            Repeater {
                                model: root.usersList

                                Rectangle {
                                    required property var modelData
                                    required property int index
                                    Layout.fillWidth: true
                                    height: 42
                                    radius: 8
                                    color: root.selectedUserIndex === index ? "#3b4252" : "#2e3440"
                                    border.color: root.selectedUserIndex === index ? "#88c0d0" : "#434c5e"
                                    border.width: root.selectedUserIndex === index ? 2 : 1

                                    RowLayout {
                                        anchors.centerIn: parent
                                        spacing: 6

                                        Text {
                                            text: root.selectedUserIndex === index ? "✔" : (modelData.is_shared ? "👥" : "👤")
                                            font.pixelSize: 12
                                            color: root.selectedUserIndex === index ? (modelData.is_shared ? "#ebcb8b" : "#88c0d0") : "#d8dee9"
                                        }

                                        Text {
                                            text: modelData.name + (root.defaultUser && modelData.name.toLowerCase() === root.defaultUser.toLowerCase() ? " (Default)" : "")
                                            font.pixelSize: 12
                                            font.bold: root.selectedUserIndex === index
                                            color: root.selectedUserIndex === index ? (modelData.is_shared ? "#ebcb8b" : "#88c0d0") : "#eceff4"
                                        }
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: {
                                            root.selectedUserIndex = index;
                                            if (modelData.playlists && modelData.playlists.length > 0) {
                                                root.selectedExistingPlaylist = modelData.playlists[0];
                                            } else {
                                                root.selectedExistingPlaylist = "";
                                            }
                                            root.savePreferences();
                                        }
                                    }
                                }
                            }
                        }
                    }

                    // Audio Bitrate / Quality Selector
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Text {
                            text: "Audio Quality:"
                            font.pixelSize: 11
                            font.bold: true
                            color: "#d8dee9"
                        }

                        Repeater {
                            model: [
                                {"label": "Auto (High)", "val": "auto"},
                                {"label": "320 kbps MP3", "val": "320k"},
                                {"label": "Lossless / FLAC", "val": "flac"}
                            ]

                            Rectangle {
                                required property var modelData
                                height: 26
                                radius: 5
                                color: root.audioBitrate === modelData.val ? "#3b4252" : "#242933"
                                border.color: root.audioBitrate === modelData.val ? "#88c0d0" : "#434c5e"
                                border.width: root.audioBitrate === modelData.val ? 2 : 1
                                width: brTxt.width + 16

                                Text {
                                    id: brTxt
                                    anchors.centerIn: parent
                                    text: modelData.label
                                    font.pixelSize: 10
                                    font.bold: root.audioBitrate === modelData.val
                                    color: root.audioBitrate === modelData.val ? "#88c0d0" : "#d8dee9"
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        root.audioBitrate = modelData.val;
                                        root.savePreferences();
                                    }
                                }
                            }
                        }

                        Item { Layout.fillWidth: true }
                    }

                    // Song Routing Options
                    ColumnLayout {
                        visible: root.countTracks > 0
                        Layout.fillWidth: true
                        spacing: 6

                        Text {
                            text: "🎶 Routing for Loose Songs in this batch:"
                            font.pixelSize: 12
                            font.bold: true
                            color: "#a3be8c"
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                Layout.fillWidth: true
                                height: 36
                                radius: 6
                                color: root.songAction === "none" ? "#3b4252" : "#2e3440"
                                border.color: root.songAction === "none" ? "#a3be8c" : "#434c5e"
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: "📁 Library Only"
                                    font.pixelSize: 11
                                    color: root.songAction === "none" ? "#a3be8c" : "#d8dee9"
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.songAction = "none"
                                }
                            }

                            Rectangle {
                                visible: root.currentUser.playlists && root.currentUser.playlists.length > 0
                                Layout.fillWidth: true
                                height: 36
                                radius: 6
                                color: root.songAction === "existing_playlist" ? "#3b4252" : "#2e3440"
                                border.color: root.songAction === "existing_playlist" ? "#88c0d0" : "#434c5e"
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: "➕ Add to Playlist"
                                    font.pixelSize: 11
                                    color: root.songAction === "existing_playlist" ? "#88c0d0" : "#d8dee9"
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.songAction = "existing_playlist"
                                }
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                height: 36
                                radius: 6
                                color: root.songAction === "new_playlist" ? "#3b4252" : "#2e3440"
                                border.color: root.songAction === "new_playlist" ? "#b48ead" : "#434c5e"
                                border.width: 1

                                Text {
                                    anchors.centerIn: parent
                                    text: "✨ New Playlist"
                                    font.pixelSize: 11
                                    color: root.songAction === "new_playlist" ? "#b48ead" : "#d8dee9"
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.songAction = "new_playlist"
                                }
                            }
                        }

                        // Existing Playlist Selector
                        RowLayout {
                            visible: root.songAction === "existing_playlist" && root.currentUser.playlists && root.currentUser.playlists.length > 0
                            Layout.fillWidth: true
                            spacing: 8

                            Text {
                                text: "Select Playlist:"
                                font.pixelSize: 11
                                color: "#d8dee9"
                            }

                            Repeater {
                                model: root.currentUser.playlists || []

                                Rectangle {
                                    required property var modelData
                                    height: 28
                                    radius: 4
                                    color: root.selectedExistingPlaylist === modelData ? "#88c0d0" : "#2e3440"
                                    border.color: "#434c5e"
                                    border.width: 1
                                    width: plBtnText.width + 16

                                    Text {
                                        id: plBtnText
                                        anchors.centerIn: parent
                                        text: "🎵 " + modelData
                                        font.pixelSize: 11
                                        font.bold: root.selectedExistingPlaylist === modelData
                                        color: root.selectedExistingPlaylist === modelData ? "#242933" : "#eceff4"
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: root.selectedExistingPlaylist = modelData
                                    }
                                }
                            }
                        }

                        // New Playlist Name Input
                        Rectangle {
                            visible: root.songAction === "new_playlist"
                            Layout.fillWidth: true
                            height: 36
                            radius: 6
                            color: "#2e3440"
                            border.color: newPlInput.activeFocus ? "#b48ead" : "#434c5e"
                            border.width: 1

                            TextInput {
                                id: newPlInput
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 12
                                color: "#eceff4"
                                selectByMouse: true
                                onTextChanged: root.newPlaylistName = text

                                Text {
                                    anchors.fill: parent
                                    verticalAlignment: Text.AlignVCenter
                                    text: "Enter new playlist title (e.g., Roadtrip 2026)..."
                                    color: "#4c566a"
                                    font.pixelSize: 12
                                    visible: !newPlInput.text && !newPlInput.activeFocus
                                }
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    // Active Background Task Mini-Banner
                    Rectangle {
                        visible: (root.isServerTaskActive || batchProc.running)
                        Layout.fillWidth: true
                        height: 38
                        radius: 8
                        color: "#2e3440"
                        border.color: "#88c0d0"
                        border.width: 1

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 12
                            anchors.rightMargin: 12
                            spacing: 8

                            Rectangle {
                                width: 8
                                height: 8
                                radius: 4
                                color: "#a3be8c"
                            }

                            Text {
                                text: "Downloading: " + (root.currentTrackName || "Active Task")
                                font.pixelSize: 11
                                color: "#eceff4"
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }

                            Text {
                                text: root.itemProgressPct > 0 ? (root.itemProgressPct + "%") : ""
                                font.pixelSize: 11
                                font.bold: true
                                color: "#a3be8c"
                            }

                            Rectangle {
                                height: 24
                                radius: 4
                                color: "#434c5e"
                                width: miniViewTxt.width + 16

                                Text {
                                    id: miniViewTxt
                                    anchors.centerIn: parent
                                    text: "📊 View Details"
                                    font.pixelSize: 10
                                    color: "#88c0d0"
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        root.activeTab = "download";
                                        root.appState = "downloading";
                                    }
                                }
                            }
                        }
                    }

                    // Action Buttons Row
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        Rectangle {
                            width: 100
                            height: 40
                            radius: 8
                            color: cancelBtnMa.containsMouse ? "#3b4252" : "transparent"
                            border.color: "#434c5e"
                            border.width: 1

                            Text {
                                anchors.centerIn: parent
                                text: "Cancel"
                                font.pixelSize: 12
                                color: "#d8dee9"
                            }

                            MouseArea {
                                id: cancelBtnMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: Qt.quit()
                            }
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            height: 40
                            radius: 8
                            color: root.parsedItems.length === 0 ? "#3b4252" : (startBtnMa.containsMouse ? "#81a1c1" : "#88c0d0")
                            opacity: root.parsedItems.length === 0 ? 0.5 : 1.0

                            Text {
                                anchors.centerIn: parent
                                text: root.parsedItems.length > 1 ? ("🚀 Ingest Batch (" + root.parsedItems.length + " items)") : ((root.isServerTaskActive || batchProc.running) ? "➕ Add to Active Queue" : "🚀 Download to Server")
                                font.pixelSize: 13
                                font.bold: true
                                color: "#242933"
                            }

                            MouseArea {
                                id: startBtnMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: root.parsedItems.length > 0 ? Qt.PointingHandCursor : Qt.ArrowCursor
                                onClicked: root.startIngestion()
                            }
                        }
                    }
                }

                // -------------------------------------------------------------
                // View 2: Live Download Progress (Highly Informative)
                // -------------------------------------------------------------
                ColumnLayout {
                    visible: root.activeTab === "download" && (root.appState === "downloading" || root.isServerTaskActive || batchProc.running) && root.appState !== "complete"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 14

                    // Batch Header Info Box
                    Rectangle {
                        Layout.fillWidth: true
                        height: 74
                        radius: 10
                        color: "#2e3440"
                        border.color: "#434c5e"
                        border.width: 1

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 12

                            Rectangle {
                                width: 38
                                height: 38
                                radius: 8
                                color: root.currentStage === "discovery" ? "#3b4252" : "#88c0d0"

                                Text {
                                    anchors.centerIn: parent
                                    text: root.currentStage === "discovery" ? "🔍" : "🎵"
                                    font.pixelSize: 18
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 2

                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        text: root.currentTrackName || "Processing catalog..."
                                        font.pixelSize: 13
                                        font.bold: true
                                        color: "#eceff4"
                                        elide: Text.ElideRight
                                    }
                                    Item { Layout.fillWidth: true }
                                    Rectangle {
                                        visible: root.batchTotal > 0
                                        height: 20
                                        radius: 4
                                        color: "#3b4252"
                                        width: batchBadgeText.width + 10
                                        Text {
                                            id: batchBadgeText
                                            anchors.centerIn: parent
                                            text: "Batch " + root.batchCurrentIndex + " of " + root.batchTotal
                                            font.pixelSize: 10
                                            color: "#88c0d0"
                                            font.bold: true
                                        }
                                    }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        text: root.statusMessage
                                        font.pixelSize: 11
                                        color: "#88c0d0"
                                        elide: Text.ElideRight
                                    }
                                    Item { Layout.fillWidth: true }
                                    Text {
                                        text: "⏱ " + root.formatTime(root.elapsedSeconds)
                                        font.pixelSize: 11
                                        color: "#8fbcbb"
                                    }
                                }
                            }
                        }
                    }

                    // Progress Bar Section
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6

                        Rectangle {
                            Layout.fillWidth: true
                            height: 10
                            radius: 5
                            color: "#2e3440"
                            clip: true

                            // Active progress fill
                            Rectangle {
                                visible: root.itemProgressPct > 0
                                width: parent.width * (root.itemProgressPct / 100.0)
                                height: parent.height
                                radius: 5
                                color: "#88c0d0"

                                Behavior on width {
                                    NumberAnimation { duration: 250; easing.type: Easing.OutQuad }
                                }
                            }

                            // Discovery animated pulse shimmer
                            Rectangle {
                                id: shimmer
                                visible: root.itemProgressPct === 0 && root.appState === "downloading"
                                width: 120
                                height: parent.height
                                radius: 5
                                color: "#88c0d0"
                                opacity: 0.7

                                SequentialAnimation on x {
                                    running: root.itemProgressPct === 0 && root.appState === "downloading"
                                    loops: Animation.Infinite
                                    NumberAnimation { from: -120; to: card.width - 52; duration: 1400; easing.type: Easing.InOutQuad }
                                }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            Text {
                                text: root.currentStage === "discovery" ? "🔍 Scanning catalog..." : (root.itemProgressPct + "%")
                                font.pixelSize: 11
                                font.bold: true
                                color: "#88c0d0"
                            }
                            Item { Layout.fillWidth: true }
                            Text {
                                text: root.itemTotalTracksCount > 0 ? ("Track " + root.itemCurrentTrackIndex + " of " + root.itemTotalTracksCount) : "Querying metadata API..."
                                font.pixelSize: 11
                                color: "#d8dee9"
                            }
                        }
                        // Progress breakdown badges
                        RowLayout {
                            visible: root.itemTotalTracksCount > 0
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                height: 22
                                radius: 4
                                color: "#2e3440"
                                border.color: "#434c5e"
                                border.width: 1
                                width: skippedBadgeText.width + 12
                                Text {
                                    id: skippedBadgeText
                                    anchors.centerIn: parent
                                    text: "⏩ " + root.itemSkippedCount + " in Library"
                                    font.pixelSize: 10
                                    color: "#81a1c1"
                                }
                            }

                            Rectangle {
                                height: 22
                                radius: 4
                                color: "#2e3440"
                                border.color: "#434c5e"
                                border.width: 1
                                width: downloadedBadgeText.width + 12
                                Text {
                                    id: downloadedBadgeText
                                    anchors.centerIn: parent
                                    text: "⬇ " + root.itemDownloadedCount + " Downloaded"
                                    font.pixelSize: 10
                                    color: "#88c0d0"
                                }
                            }

                            Item { Layout.fillWidth: true }

                            Text {
                                text: "🎶 " + (root.itemSkippedCount + root.itemDownloadedCount) + " / " + root.itemTotalTracksCount + " Total"
                                font.pixelSize: 11
                                font.bold: true
                                color: "#eceff4"
                            }
                        }
                    }

                    // Console Output Toolbar
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        Text {
                            text: "Console Output"
                            font.pixelSize: 11
                            font.bold: true
                            color: "#8fbcbb"
                        }

                        Item { Layout.fillWidth: true }

                        Rectangle {
                            height: 22
                            radius: 4
                            color: root.filterErrorsOnly ? "#bf616a" : "#2e3440"
                            border.color: "#434c5e"
                            border.width: 1
                            width: filterTxt.width + 14

                            Text {
                                id: filterTxt
                                anchors.centerIn: parent
                                text: root.filterErrorsOnly ? "⚠️ Errors & Milestones" : "📄 All Messages"
                                font.pixelSize: 10
                                font.bold: true
                                color: root.filterErrorsOnly ? "#eceff4" : "#88c0d0"
                            }

                            MouseArea {
                                anchors.fill: parent
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.filterErrorsOnly = !root.filterErrorsOnly
                            }
                        }

                        Rectangle {
                            height: 22
                            radius: 4
                            color: copyLogMa.containsMouse ? "#434c5e" : "#2e3440"
                            border.color: "#434c5e"
                            border.width: 1
                            width: copyLogTxt.width + 14

                            Text {
                                id: copyLogTxt
                                anchors.centerIn: parent
                                text: "📋 Copy Log"
                                font.pixelSize: 10
                                color: "#88c0d0"
                            }

                            MouseArea {
                                id: copyLogMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.copyLogsToClipboard()
                            }
                        }
                    }

                    // Live Log Console
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 160
                        radius: 10
                        color: "#1c2028"
                        border.color: "#3b4252"
                        border.width: 1
                        clip: true

                        ListView {
                            id: logList
                            anchors.fill: parent
                            anchors.margins: 10
                            anchors.rightMargin: 16
                            model: logModel
                            spacing: 4
                            clip: true

                            ScrollBar.vertical: ScrollBar {
                                parent: logList.parent
                                anchors.top: logList.top
                                anchors.bottom: logList.bottom
                                anchors.right: logList.right
                                anchors.rightMargin: 4
                                policy: ScrollBar.AsNeeded
                                width: 5
                            }

                            delegate: Text {
                                visible: !root.filterErrorsOnly || (model.important !== undefined ? model.important : true)
                                height: visible ? implicitHeight : 0
                                width: logList.width
                                text: message
                                font.family: "Monospace"
                                font.pixelSize: 11
                                color: (message.indexOf("🎤") !== -1 || message.indexOf("✔") !== -1) ? "#a3be8c" :
                                       (message.indexOf("⏩") !== -1 ? "#81a1c1" :
                                       (message.indexOf("⚡") !== -1 || message.indexOf("⬇") !== -1) ? "#ebcb8b" :
                                       (message.indexOf("▶") !== -1) ? "#88c0d0" :
                                       (message.indexOf("🔒") !== -1 || message.indexOf("✂") !== -1) ? "#b48ead" :
                                       (message.indexOf("⚠️") !== -1) ? "#d08770" :
                                       (message.indexOf("ERROR") !== -1 || message.indexOf("🛑") !== -1 || message.indexOf("FATAL") !== -1) ? "#bf616a" : "#d8dee9")
                                wrapMode: Text.WrapAnywhere
                            }

                            onCountChanged: {
                                logList.positionViewAtEnd();
                            }
                        }
                    }

                    // Bottom Controls Row
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        Rectangle {
                            height: 36
                            radius: 8
                            color: addMoreMa.containsMouse ? "#434c5e" : "#3b4252"
                            width: addMoreTxt.width + 24

                            Text {
                                id: addMoreTxt
                                anchors.centerIn: parent
                                text: "📥 Add More Music"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#88c0d0"
                            }

                            MouseArea {
                                id: addMoreMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.activeTab = "input"
                            }
                        }

                        Item { Layout.fillWidth: true }

                        Rectangle {
                            width: 110
                            height: 36
                            radius: 8
                            color: cancelDlMa.containsMouse ? "#bf616a" : "#3b4252"

                            Text {
                                anchors.centerIn: parent
                                text: "✕ Stop Task"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#eceff4"
                            }

                            MouseArea {
                                id: cancelDlMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.cancelIngestion()
                            }
                        }
                    }
                }

                // -------------------------------------------------------------
                // View 2.5: Idle State (on Active Task tab but nothing running)
                // -------------------------------------------------------------
                ColumnLayout {
                    visible: root.activeTab === "download" && !root.isServerTaskActive && !batchProc.running && root.appState !== "complete" && root.appState !== "error"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 16

                    Item { Layout.fillHeight: true }

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        width: 56
                        height: 56
                        radius: 28
                        color: "#2e3440"
                        border.color: "#434c5e"
                        border.width: 1

                        Text {
                            anchors.centerIn: parent
                            text: "💤"
                            font.pixelSize: 24
                        }
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "No Active Downloads"
                        font.pixelSize: 16
                        font.bold: true
                        color: "#eceff4"
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "All background downloads are complete. Paste a Spotify playlist or song in 'Add Music' to start!"
                        font.pixelSize: 12
                        color: "#8fbcbb"
                    }

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        height: 36
                        radius: 8
                        color: goAddMa.containsMouse ? "#81a1c1" : "#88c0d0"
                        width: goAddTxt.width + 24

                        Text {
                            id: goAddTxt
                            anchors.centerIn: parent
                            text: "📥 Go to Add Music"
                            font.pixelSize: 12
                            color: "#242933"
                            font.bold: true
                        }

                        MouseArea {
                            id: goAddMa
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.activeTab = "input"
                        }
                    }

                    Item { Layout.fillHeight: true }
                }

                // -------------------------------------------------------------
                // View 3: Complete State
                // -------------------------------------------------------------
                ColumnLayout {
                    visible: root.appState === "complete" && !root.isServerTaskActive && !batchProc.running
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 16

                    Item { Layout.fillHeight: true }

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        width: 64
                        height: 64
                        radius: 32
                        color: "#2e3440"
                        border.color: "#a3be8c"
                        border.width: 2

                        Text {
                            anchors.centerIn: parent
                            text: "✔"
                            font.pixelSize: 32
                            color: "#a3be8c"
                        }
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Batch Ingestion Complete!"
                        font.pixelSize: 18
                        font.bold: true
                        color: "#eceff4"
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: root.completionMessage || ("Successfully imported to Jellyfin for " + root.currentUser.name)
                        font.pixelSize: 13
                        color: "#8fbcbb"
                    }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 8

                        Rectangle {
                            height: 28
                            radius: 6
                            color: "#2e3440"
                            border.color: "#434c5e"
                            border.width: 1
                            width: refreshBadgeText.width + 20

                            Text {
                                id: refreshBadgeText
                                anchors.centerIn: parent
                                text: "🔄 Music catalog refreshed"
                                font.pixelSize: 11
                                color: "#a3be8c"
                            }
                        }

                        Rectangle {
                            height: 28
                            radius: 6
                            color: "#2e3440"
                            border.color: "#434c5e"
                            border.width: 1
                            width: lyricsBadgeText.width + 20

                            Text {
                                id: lyricsBadgeText
                                anchors.centerIn: parent
                                text: "🎶 Synced lyrics (.lrc) ready"
                                font.pixelSize: 11
                                color: "#ebcb8b"
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 10

                        Rectangle {
                            width: 130
                            height: 40
                            radius: 8
                            color: openJellyfinMa.containsMouse ? "#434c5e" : "#2e3440"
                            border.color: "#88c0d0"
                            border.width: 1

                            Text {
                                anchors.centerIn: parent
                                text: "🍿 Jellyfin Web"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#88c0d0"
                            }

                            MouseArea {
                                id: openJellyfinMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: openJellyfinProc.running = true
                            }
                        }

                        Rectangle {
                            width: 130
                            height: 40
                            radius: 8
                            color: openFolderMa.containsMouse ? "#434c5e" : "#2e3440"
                            border.color: "#434c5e"
                            border.width: 1

                            Text {
                                anchors.centerIn: parent
                                text: "📁 Music Folder"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#eceff4"
                            }

                            MouseArea {
                                id: openFolderMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: openFolderProc.running = true
                            }
                        }

                        Rectangle {
                            width: 130
                            height: 40
                            radius: 8
                            color: dlAnotherMa.containsMouse ? "#434c5e" : "#3b4252"

                            Text {
                                anchors.centerIn: parent
                                text: "🎵 Ingest More"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#eceff4"
                            }

                            MouseArea {
                                id: dlAnotherMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.resetForm()
                            }
                        }

                        Rectangle {
                            width: 100
                            height: 40
                            radius: 8
                            color: doneBtnMa.containsMouse ? "#81a1c1" : "#88c0d0"

                            Text {
                                anchors.centerIn: parent
                                text: "Done"
                                font.pixelSize: 13
                                font.bold: true
                                color: "#242933"
                            }

                            MouseArea {
                                id: doneBtnMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: Qt.quit()
                            }
                        }
                    }
                }

                // -------------------------------------------------------------
                // View 4: Error State
                // -------------------------------------------------------------
                ColumnLayout {
                    visible: root.appState === "error"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 16

                    Item { Layout.fillHeight: true }

                    Rectangle {
                        Layout.alignment: Qt.AlignHCenter
                        width: 64
                        height: 64
                        radius: 32
                        color: "#2e3440"
                        border.color: "#bf616a"
                        border.width: 2

                        Text {
                            anchors.centerIn: parent
                            text: "✕"
                            font.pixelSize: 30
                            color: "#bf616a"
                        }
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: "Ingestion Encountered an Issue"
                        font.pixelSize: 17
                        font.bold: true
                        color: "#eceff4"
                    }

                    Text {
                        Layout.alignment: Qt.AlignHCenter
                        text: root.errorMessage || "Failed to process some items in the batch."
                        font.pixelSize: 12
                        color: "#bf616a"
                    }

                    Item { Layout.fillHeight: true }

                    RowLayout {
                        Layout.alignment: Qt.AlignHCenter
                        spacing: 12

                        Rectangle {
                            width: 120
                            height: 40
                            radius: 8
                            color: retryMa.containsMouse ? "#81a1c1" : "#88c0d0"

                            Text {
                                anchors.centerIn: parent
                                text: "🔄 Try Again"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#242933"
                            }

                            MouseArea {
                                id: retryMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: root.startIngestion()
                            }
                        }

                        Rectangle {
                            width: 100
                            height: 40
                            radius: 8
                            color: errLogMa.containsMouse ? "#4c566a" : "#434c5e"

                            Text {
                                anchors.centerIn: parent
                                text: "📄 View Log"
                                font.pixelSize: 12
                                color: "#88c0d0"
                            }

                            MouseArea {
                                id: errLogMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: openLogProc.running = true
                            }
                        }

                        Rectangle {
                            width: 100
                            height: 40
                            radius: 8
                            color: errCloseMa.containsMouse ? "#434c5e" : "#3b4252"

                            Text {
                                anchors.centerIn: parent
                                text: "Close"
                                font.pixelSize: 12
                                color: "#d8dee9"
                            }

                            MouseArea {
                                id: errCloseMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: Qt.quit()
                            }
                        }
                    }
                }

                // -------------------------------------------------------------
                // View 5: Settings & Connection Configuration
                // -------------------------------------------------------------
                ColumnLayout {
                    visible: root.activeTab === "settings"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 12

                    Text {
                        text: "Connection & Server Configuration"
                        font.pixelSize: 14
                        font.bold: true
                        color: "#eceff4"
                    }

                    Text {
                        text: "Configure the remote Jellyfin server target and SSH execution parameters."
                        font.pixelSize: 11
                        color: "#8fbcbb"
                    }

                    // Server Host Field
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text { text: "SSH Server Host / IP:"; font.pixelSize: 11; font.bold: true; color: "#d8dee9" }
                        Rectangle {
                            Layout.fillWidth: true
                            height: 36
                            radius: 6
                            color: "#2e3440"
                            border.color: hostInput.activeFocus ? "#88c0d0" : "#434c5e"
                            border.width: 1
                            TextInput {
                                id: hostInput
                                anchors.fill: parent
                                anchors.leftMargin: 10; anchors.rightMargin: 10
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 12; color: "#eceff4"; selectByMouse: true
                                text: root.serverHost
                                onTextChanged: root.serverHost = text
                            }
                        }
                    }

                    // Jellyfin Web URL Field
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text { text: "Jellyfin Web URL:"; font.pixelSize: 11; font.bold: true; color: "#d8dee9" }
                        Rectangle {
                            Layout.fillWidth: true
                            height: 36
                            radius: 6
                            color: "#2e3440"
                            border.color: webUrlInput.activeFocus ? "#88c0d0" : "#434c5e"
                            border.width: 1
                            TextInput {
                                id: webUrlInput
                                anchors.fill: parent
                                anchors.leftMargin: 10; anchors.rightMargin: 10
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 12; color: "#eceff4"; selectByMouse: true
                                text: root.jellyfinWebUrl
                                onTextChanged: root.jellyfinWebUrl = text
                            }
                        }
                    }

                    // Music Folder URL / SFTP Path Field
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text { text: "Music Folder URL (SFTP or Local):"; font.pixelSize: 11; font.bold: true; color: "#d8dee9" }
                        Rectangle {
                            Layout.fillWidth: true
                            height: 36
                            radius: 6
                            color: "#2e3440"
                            border.color: folderInput.activeFocus ? "#88c0d0" : "#434c5e"
                            border.width: 1
                            TextInput {
                                id: folderInput
                                anchors.fill: parent
                                anchors.leftMargin: 10; anchors.rightMargin: 10
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 12; color: "#eceff4"; selectByMouse: true
                                text: root.musicFolderUrl
                                onTextChanged: root.musicFolderUrl = text
                            }
                        }
                    }

                    // Remote Ingest Script Path Field
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text { text: "Remote Ingest Script Path:"; font.pixelSize: 11; font.bold: true; color: "#d8dee9" }
                        Rectangle {
                            Layout.fillWidth: true
                            height: 36
                            radius: 6
                            color: "#2e3440"
                            border.color: scriptInput.activeFocus ? "#88c0d0" : "#434c5e"
                            border.width: 1
                            TextInput {
                                id: scriptInput
                                anchors.fill: parent
                                anchors.leftMargin: 10; anchors.rightMargin: 10
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 12; color: "#eceff4"; selectByMouse: true
                                text: root.remoteScriptPath
                                onTextChanged: root.remoteScriptPath = text
                            }
                        }
                    }

                    // Default Jellyfin User Field
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text { text: "Default Jellyfin Account Name (e.g. Kendon or Tennison):"; font.pixelSize: 11; font.bold: true; color: "#d8dee9" }
                        Rectangle {
                            Layout.fillWidth: true
                            height: 36
                            radius: 6
                            color: "#2e3440"
                            border.color: defaultUserInput.activeFocus ? "#88c0d0" : "#434c5e"
                            border.width: 1
                            TextInput {
                                id: defaultUserInput
                                anchors.fill: parent
                                anchors.leftMargin: 10; anchors.rightMargin: 10
                                verticalAlignment: Text.AlignVCenter
                                font.pixelSize: 12; color: "#eceff4"; selectByMouse: true
                                text: root.defaultUser
                                onTextChanged: root.defaultUser = text
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    // Settings Action Buttons
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 12

                        Rectangle {
                            Layout.fillWidth: true
                            height: 40
                            radius: 8
                            color: testConnMa.containsMouse ? "#434c5e" : "#2e3440"
                            border.color: "#434c5e"
                            border.width: 1

                            Text {
                                anchors.centerIn: parent
                                text: "🔄 Test & Sync Accounts"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#88c0d0"
                            }

                            MouseArea {
                                id: testConnMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    root.saveConfig();
                                    root.fetchUsers();
                                    root.showToast("🔄 Connecting to " + root.serverHost + "...");
                                }
                            }
                        }

                        Rectangle {
                            width: 130
                            height: 40
                            radius: 8
                            color: saveCfgMa.containsMouse ? "#81a1c1" : "#88c0d0"

                            Text {
                                anchors.centerIn: parent
                                text: "💾 Save"
                                font.pixelSize: 12
                                font.bold: true
                                color: "#242933"
                            }

                            MouseArea {
                                id: saveCfgMa
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: {
                                    root.saveConfig();
                                    root.activeTab = "input";
                                }
                            }
                        }
                    }
                }
            }
        }
    }
