// main.qml - Root Controller & State Machine (<300 lines)
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import "qml/theme"
import "qml/views"
import "qml/components"

PanelWindow {
    id: root

    WlrLayershell.layer: WlrLayer.Top
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.OnDemand
    color: "transparent"

    implicitWidth: 700
    implicitHeight: activeView === "settings" ? 620 : (activeView === "progress" ? 640 : (analysisData ? 620 : 560))

    Behavior on implicitHeight { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }

    property string appState: "idle"
    property string activeView: "ingest"
    property var usersList: []
    property string selectedUserId: ""
    property var analysisData: null
    property var activeJob: ({
        job_id: "", status: "idle", percentage: 0, current_track: 0,
        total_tracks: 0, current_title: "Ready", speed: "0.0 MB/s",
        eta_seconds: 0, stage: 1, stage_name: "Ready"
    })
    property string daemonUrl: Quickshell.env("DAEMON_URL") || "http://127.0.0.1:8095"
    property string daemonWsUrl: root.daemonUrl.replace("http://", "ws://").replace("https://", "wss://") + "/ws/events"
    property string defaultUserName: Quickshell.env("DEFAULT_USER") || ""
    property string audioBitrate: "320k"
    property string lastClipboardUrl: ""

    ListModel { id: logModel }
    ListModel { id: completedTracksModel }
    Theme { id: theme }

    // --- REST Client Engine ---
    function apiRequest(method, endpoint, body, onSuccess, onError) {
        var xhr = new XMLHttpRequest();
        xhr.open(method, root.daemonUrl + endpoint, true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status >= 200 && xhr.status < 300) {
                    try {
                        var res = xhr.responseText ? JSON.parse(xhr.responseText) : {};
                        if (onSuccess) onSuccess(res);
                    } catch (e) { if (onError) onError("JSON error: " + e.message); }
                } else {
                    if (onError) onError("HTTP " + xhr.status + ": " + (xhr.responseText || "Request failed"));
                }
            }
        };
        xhr.send(body ? JSON.stringify(body) : null);
    }

    function fetchUsers() {
        apiRequest("GET", "/api/users", null, function(res) {
            root.usersList = res.users || [];
            if (root.usersList.length > 0) {
                var match = root.defaultUserName ? root.usersList.find(function(u) { return u.name.toLowerCase() === root.defaultUserName.toLowerCase(); }) : null;
                root.selectedUserId = match ? match.id : (root.selectedUserId || root.usersList[0].id);
            }
        }, function(err) { toast.show("Failed to fetch users: " + err, "warning"); });
    }

    function triggerAnalysis(urls, artistMode) {
        root.appState = "analyzing";
        toast.show("Analyzing tracks against library index...", "info");
        apiRequest("POST", "/api/resolve", { urls: urls, target_user_id: root.selectedUserId, artist_mode: artistMode || "discography" }, function(res) {
            root.analysisData = res;
            root.appState = "idle";
            toast.show("Playlist resolved in " + (res.resolve_time_ms ? res.resolve_time_ms.toFixed(1) : "0") + "ms", "success");
        }, function(err) {
            root.appState = "error";
            toast.show("Analysis failed: " + err, "error");
        });
    }

    function startIngestion(urls, playlistName, bitrate, embedLyrics, embedCover, artistMode) {
        root.appState = "downloading";
        root.activeView = "progress";
        logModel.clear();
        completedTracksModel.clear();
        root.activeJob = {
            job_id: "", status: "downloading", percentage: 0, current_track: 0,
            total_tracks: 0, current_title: "Starting download...", speed: "0.0 MB/s",
            eta_seconds: 0, stage: 1, stage_name: "Downloading Tracks"
        };
        var payload = {
            urls: urls, user_id: (playlistName && playlistName !== "__NO_PLAYLIST__") ? root.selectedUserId : null,
            playlist_name: playlistName || "Downloads",
            bitrate: bitrate || root.audioBitrate,
            embed_lyrics: embedLyrics, embed_cover: embedCover,
            artist_mode: artistMode || "discography"
        };
        apiRequest("POST", "/api/ingest", payload, function(res) {
            var j = Object.assign({}, root.activeJob);
            j.job_id = res.job_id;
            root.activeJob = j;
            toast.show("Job queued successfully", "success");
        }, function(err) {
            root.appState = "error";
            toast.show("Failed to queue ingest: " + err, "error");
        });
    }

    function cancelActiveJob() {
        if (!root.activeJob.job_id) return;
        apiRequest("POST", "/api/cancel", { job_id: root.activeJob.job_id }, function(res) {
            toast.show("Download cancelled cleanly", "info");
            root.appState = "idle";
        }, function(err) { toast.show("Cancellation error: " + err, "error"); });
    }

    // --- WebSocket Event Router ---
    function handleWsEvent(ev) {
        if (!ev || !ev.event) return;
        var t = ev.event, j = Object.assign({}, root.activeJob);
        if (t === "job_started") {
            j.job_id = ev.job_id || j.job_id; j.total_tracks = ev.total_tracks || 0;
            j.current_track = ev.already_present || 0;
            j.percentage = j.total_tracks > 0 ? ((j.current_track / j.total_tracks) * 100) : 0;
            j.stage_name = "Downloading Tracks"; j.current_title = "Starting downloads...";
            root.activeJob = j;
        } else if (t === "progress") {
            j.percentage = ev.pct !== undefined ? ev.pct : (ev.percentage !== undefined ? ev.percentage : 0);
            j.current_track = ev.current !== undefined ? ev.current : (ev.current_track !== undefined ? ev.current_track : 0);
            j.total_tracks = ev.total !== undefined ? ev.total : (ev.total_tracks !== undefined ? ev.total_tracks : 0);
            j.current_title = ev.current_title || j.current_title;
            j.speed = ev.speed || j.speed;
            j.eta_seconds = ev.eta_seconds !== undefined ? ev.eta_seconds : j.eta_seconds;
            root.activeJob = j;
        } else if (t === "stage_transition") {
            j.stage = ev.stage; j.stage_name = ev.stage_name + (ev.description ? " - " + ev.description : "");
            root.activeJob = j;
        } else if (t === "track_completed") {
            completedTracksModel.append({ title: ev.track, artist: ev.artist, duration: ev.duration, lyrics: ev.lyrics_synced, cover: ev.cover_embedded });
        } else if (t === "log") {
            logModel.append({ level: ev.level, message: ev.message, time: Qt.formatTime(new Date(), "hh:mm:ss") });
            if (logModel.count > 500) logModel.remove(0);
        } else if (t === "job_completed") {
            root.appState = "finished";
            j.percentage = 100; j.current_track = j.total_tracks; j.speed = "0.0 MB/s"; j.eta_seconds = 0; j.stage_name = "Completed";
            root.activeJob = j;
            toast.show("Ingest completed! " + (ev.downloaded || 0) + " downloaded, " + (ev.skipped || 0) + " skipped.", "success");
            notifyProc.body = "Imported " + (ev.downloaded || 0) + " tracks to Jellyfin!";
            notifyProc.running = true;
        } else if (t === "job_error") {
            root.appState = "error"; j.stage_name = "Failed"; root.activeJob = j;
            toast.show("Job failed: " + ev.error, "error");
        } else if (t === "job_cancelled") {
            root.appState = "idle"; toast.show("Job was cancelled", "info");
        }
    }

    // --- Background Subprocesses ---
    Process {
        id: wsProc
        command: ["python3", "-u", Qt.resolvedUrl("scripts/ws_listener.py").toString().replace("file://", ""), root.daemonWsUrl]
        running: true
        stdout: SplitParser {
            onRead: function(line) {
                var s = String(line || "").trim();
                if (!s) return;
                try { root.handleWsEvent(JSON.parse(s)); } catch (e) {}
            }
        }
    }

    Process {
        id: clipProc
        command: ["wl-paste"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: {
                var txt = (this.text || "").trim();
                if ((txt.indexOf("spotify.com") !== -1 || txt.indexOf("music.youtube.com") !== -1) && txt !== root.lastClipboardUrl) {
                    root.lastClipboardUrl = txt;
                    ingestView.appendUrl(txt);
                    toast.show("Auto-loaded URL from clipboard", "info");
                }
            }
        }
    }

    Process {
        id: notifyProc
        property string body: ""
        command: ["notify-send", "-i", "audio-headphones", "Jellyfin Music Downloader", body]
    }

    Process {
        id: saveConfigProc
        property string newUrl: ""
        property string newBitrate: ""
        property string newUser: ""
        command: ["python3", "-c", "import json, os, sys; p = os.path.expanduser('~/.config/omarchy/extensions/jellyfin-music-app/config.json'); data = json.load(open(p)) if os.path.exists(p) else {}; data['daemonUrl'] = sys.argv[1]; data['bitrate'] = sys.argv[2]; data['defaultUser'] = sys.argv[3]; os.makedirs(os.path.dirname(p), exist_ok=True); open(p, 'w').write(json.dumps(data, indent=2))", newUrl, newBitrate, newUser]
    }

    Component.onCompleted: { root.fetchUsers(); clipProc.running = true; }

    // --- Main Shell Container ---
    Rectangle {
        id: mainCard
        anchors.fill: parent
        color: theme.dark_background
        radius: 14
        border.color: dragArea.containsDrag ? theme.accent : theme.selection
        border.width: dragArea.containsDrag ? 2 : 1

        DropArea {
            id: dragArea
            anchors.fill: parent
            onDropped: function(drop) {
                var textToAppend = "";
                if (drop.hasUrls) {
                    for (var i = 0; i < drop.urls.length; i++) {
                        var u = drop.urls[i].toString();
                        if (u.indexOf("file://") === -1) textToAppend += (textToAppend ? "\n" : "") + u;
                    }
                }
                if (drop.hasText && !textToAppend) textToAppend = drop.text;
                if (textToAppend) { ingestView.appendUrl(textToAppend); root.activeView = "ingest"; toast.show("Added links from Drag & Drop", "info"); }
            }
        }

        ToastBanner { id: toast; theme: theme; z: 200 }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Text { text: "󰝚"; font.pixelSize: 22; color: theme.accent }
                Text { text: "Jellyfin Music Downloader"; font.pixelSize: 16; font.bold: true; color: theme.bright_foreground }
                Item { Layout.fillWidth: true }
                RowLayout {
                    spacing: 6
                    Button { text: "Ingest"; flat: true; highlighted: root.activeView === "ingest"; onClicked: root.activeView = "ingest" }
                    Button { text: "Progress (" + root.activeJob.percentage.toFixed(0) + "%)"; flat: true; highlighted: root.activeView === "progress"; onClicked: root.activeView = "progress" }
                    Button { text: "Settings"; flat: true; highlighted: root.activeView === "settings"; onClicked: root.activeView = "settings" }
                    Button { text: "✕"; flat: true; onClicked: Qt.quit() }
                }
            }

            StackLayout {
                Layout.fillWidth: true; Layout.fillHeight: true
                currentIndex: root.activeView === "ingest" ? 0 : (root.activeView === "progress" ? 1 : 2)

                IngestView {
                    id: ingestView
                    theme: theme; usersList: root.usersList; selectedUserId: root.selectedUserId
                    analysisData: root.analysisData; appState: root.appState
                    onUserSelected: function(uid) { root.selectedUserId = uid; }
                    onAnalyzeRequested: function(urls, mode) { root.triggerAnalysis(urls, mode); }
                    onIngestRequested: function(urls, plName, bitrate, lrc, cov, mode) { root.startIngestion(urls, plName, bitrate, lrc, cov, mode); }
                }

                ProgressView {
                    id: progressView
                    theme: theme; activeJob: root.activeJob; appState: root.appState
                    percentage: root.activeJob.percentage || 0; currentTrack: root.activeJob.current_track || 0
                    totalTracks: root.activeJob.total_tracks || 0; currentTitle: root.activeJob.current_title || "Ready"
                    speed: root.activeJob.speed || "0.0 MB/s"; etaSeconds: root.activeJob.eta_seconds || 0
                    stageName: root.activeJob.stage_name || "Ready"
                    logModel: logModel; completedTracksModel: completedTracksModel
                    onCancelRequested: root.cancelActiveJob(); onDoneClicked: root.activeView = "ingest"
                }

                SettingsView {
                    id: settingsView
                    theme: theme; daemonUrl: root.daemonUrl; usersList: root.usersList; defaultBitrate: root.audioBitrate
                    onSaveConfig: function(url, br) {
                        root.daemonUrl = url; root.daemonWsUrl = url.replace("http://", "ws://").replace("https://", "wss://") + "/ws/events"; root.audioBitrate = br;
                        saveConfigProc.newUrl = url; saveConfigProc.newBitrate = br; saveConfigProc.newUser = root.defaultUserName;
                        saveConfigProc.running = true; root.fetchUsers(); toast.show("Settings saved", "success");
                    }
                }
            }
        }
    }
}
