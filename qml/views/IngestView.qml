// qml/views/IngestView.qml
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../components"
import "../theme"

Item {
    id: view
    required property Theme theme
    property var usersList: []
    property string selectedUserId: ""
    property var analysisData: null
    property string appState: "idle"

    signal userSelected(string userId)
    signal analyzeRequested(var urls, string artistMode)
    signal ingestRequested(var urls, string playlistName, string bitrate, bool embedLyrics, bool embedCover, string artistMode)

    function appendUrl(u) {
        var clean = u.trim();
        if (!clean) return;
        if (urlInput.text.trim()) urlInput.text = urlInput.text.trim() + "\n" + clean;
        else urlInput.text = clean;
    }

    readonly property var currentUrls: {
        var txt = urlInput ? urlInput.text : "";
        var lines = txt.split(/[\r\n\s,]+/);
        var res = [];
        for (var i = 0; i < lines.length; i++) {
            var l = lines[i].trim();
            if (l.indexOf("http") === 0) res.push(l);
        }
        return res;
    }

    function getUrls() {
        return view.currentUrls;
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: parent.width

        ColumnLayout {
            width: view.width
            spacing: 14

            // User Selection Header
            Text {
                text: plPicker.requiresUser ? "Target Jellyfin Account (Playlist Owner)" : "Target: 🌐 Server-Wide Music Library (All Jellyfin Users)"
                font.bold: true
                font.pixelSize: 13
                color: plPicker.requiresUser ? theme.bright_foreground : theme.accent
            }

            UserSelector {
                visible: plPicker.requiresUser
                Layout.fillWidth: true
                theme: view.theme
                users: view.usersList
                selectedUserId: view.selectedUserId
                onUserSelected: function(uid) { view.userSelected(uid); }
            }

            // URL Input Box
            Text {
                text: "Music URLs (Spotify / YouTube Music)"
                font.bold: true
                font.pixelSize: 13
                color: theme.bright_foreground
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 90
                color: theme.background
                radius: 8
                border.color: urlInput.activeFocus ? theme.accent : theme.selection
                border.width: 1

                TextArea {
                    id: urlInput
                    anchors.fill: parent
                    anchors.margins: 8
                    placeholderText: "Paste playlist, album, or track links here (one per line)..."
                    placeholderTextColor: theme.dark_foreground
                    color: theme.foreground
                    font.pixelSize: 12
                    wrapMode: TextEdit.WrapAnywhere
                }
            }

            // Ingest Options: Playlist Assignment & Bitrate
            PlaylistPicker {
                id: plPicker
                Layout.fillWidth: true
                theme: view.theme
                users: view.usersList
                activeUserId: view.selectedUserId
                urls: view.currentUrls
                detectedPlaylistName: (view.analysisData && view.analysisData.playlist_name && view.analysisData.playlist_name !== "Resolved Playlist") ? view.analysisData.playlist_name : ""
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 16

                Text {
                    text: "Bitrate:"
                    font.pixelSize: 12
                    color: theme.foreground
                }
                ComboBox {
                    id: bitrateCombo
                    model: ["320k", "256k", "192k", "128k"]
                    currentIndex: 0
                }

                CheckBox {
                    id: lyricsCheck
                    text: "Synced Lyrics (.lrc)"
                    checked: true
                }
                CheckBox {
                    id: coverCheck
                    text: "High-Res Artwork"
                    checked: true
                }
            }

            // Pre-Flight Analysis Card (When Resolved)
            AnalysisCard {
                Layout.fillWidth: true
                theme: view.theme
                visible: view.analysisData !== null
                analysisData: view.analysisData
            }

            // Action Buttons
            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                Button {
                    text: "🔍 Pre-Flight Diff Check"
                    enabled: view.getUrls().length > 0 && view.appState !== "analyzing"
                    onClicked: view.analyzeRequested(view.getUrls(), plPicker.artistMode)
                }

                Item { Layout.fillWidth: true }

                Button {
                    text: view.analysisData ? ("⬇ Ingest (" + view.analysisData.missing_tracks + " Missing Tracks)") : "⬇ Start Ingest"
                    highlighted: true
                    enabled: view.getUrls().length > 0 && view.appState !== "downloading"
                    onClicked: {
                        view.ingestRequested(
                            view.getUrls(),
                            plPicker.resolvedPlaylistName,
                            bitrateCombo.currentText,
                            lyricsCheck.checked,
                            coverCheck.checked,
                            plPicker.artistMode
                        );
                    }
                }
            }
        }
    }
}
