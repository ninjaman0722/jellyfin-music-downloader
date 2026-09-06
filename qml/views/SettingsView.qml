// qml/views/SettingsView.qml
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

Item {
    id: view
    required property Theme theme
    property string daemonUrl: "http://127.0.0.1:8095"
    property string defaultUserName: ""
    property string jellyfinWebUrl: "http://127.0.0.1:8096"
    property string musicFolderUrl: "/mnt/media/music"
    property bool autoClipboardDetect: true
    property var usersList: []
    property string healthStatus: "Not tested"
    property bool isHealthy: false

    signal saveConfig(var configMap)

    readonly property var userModel: {
        var names = ["(None - Ask Every Time)"];
        for (var i = 0; i < view.usersList.length; i++) {
            if (view.usersList[i] && view.usersList[i].name) {
                names.push(view.usersList[i].name);
            }
        }
        return names;
    }

    function probeHealth() {
        var xhr = new XMLHttpRequest();
        xhr.open("GET", daemonInput.text.trim() + "/health", true);
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                if (xhr.status === 200) {
                    try {
                        var res = JSON.parse(xhr.responseText);
                        view.healthStatus = "Online (v" + (res.version || "2.0") + ", " + (res.active_jobs !== undefined ? res.active_jobs : 0) + " active jobs)";
                        view.isHealthy = true;
                    } catch (e) {
                        view.healthStatus = "Invalid JSON response";
                        view.isHealthy = false;
                    }
                } else {
                    view.healthStatus = "HTTP Error " + xhr.status;
                    view.isHealthy = false;
                }
            }
        };
        xhr.send();
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: parent.width
        clip: true

        ColumnLayout {
            width: view.width
            spacing: 14

            // --- Section 1: Backend Daemon ---
            Text {
                text: "Backend Daemon Configuration"
                font.bold: true
                font.pixelSize: 13
                color: theme.bright_foreground
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                TextField {
                    id: daemonInput
                    Layout.fillWidth: true
                    text: view.daemonUrl
                    placeholderText: "http://127.0.0.1:8095"
                }

                Button {
                    text: "Probe /health"
                    onClicked: view.probeHealth()
                }
            }

            Text {
                text: "Status: " + view.healthStatus
                font.pixelSize: 11
                color: view.isHealthy ? theme.green : (view.healthStatus === "Not tested" ? theme.light_foreground : theme.red)
            }

            // --- Section 2: Jellyfin Integration ---
            Text {
                text: "Jellyfin Integration & Accounts"
                font.bold: true
                font.pixelSize: 13
                color: theme.bright_foreground
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6

                Text {
                    text: "Default Jellyfin Account (Pre-selected on launch):"
                    font.pixelSize: 11
                    color: theme.foreground
                }

                ComboBox {
                    id: userCombo
                    Layout.fillWidth: true
                    model: view.userModel
                    currentIndex: {
                        if (!view.defaultUserName) return 0;
                        for (var i = 0; i < view.userModel.length; i++) {
                            if (view.userModel[i].toLowerCase() === view.defaultUserName.toLowerCase()) return i;
                        }
                        return 0;
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6

                Text {
                    text: "Jellyfin Web Portal URL:"
                    font.pixelSize: 11
                    color: theme.foreground
                }

                TextField {
                    id: jfWebInput
                    Layout.fillWidth: true
                    text: view.jellyfinWebUrl
                    placeholderText: "http://127.0.0.1:8096"
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6

                Text {
                    text: "Music Library Path (Storage directory):"
                    font.pixelSize: 11
                    color: theme.foreground
                }

                TextField {
                    id: musicFolderInput
                    Layout.fillWidth: true
                    text: view.musicFolderUrl
                    placeholderText: "/mnt/media/music"
                }
            }

            // --- Section 3: Preferences & Clipboard ---
            Text {
                text: "Client Preferences"
                font.bold: true
                font.pixelSize: 13
                color: theme.bright_foreground
            }

            CheckBox {
                id: clipCheck
                text: "Auto-detect music URLs from system clipboard (wl-paste)"
                checked: view.autoClipboardDetect
            }

            // --- Section 4: Omarchy Theme Preview ---
            Text {
                text: "Active Omarchy Theme Palette"
                font.bold: true
                font.pixelSize: 13
                color: theme.bright_foreground
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Repeater {
                    model: [
                        { name: "Background", color: theme.background },
                        { name: "Accent", color: theme.accent },
                        { name: "Green", color: theme.green },
                        { name: "Red", color: theme.red },
                        { name: "Cyan", color: theme.cyan }
                    ]
                    Rectangle {
                        width: 70
                        height: 32
                        radius: 6
                        color: modelData.color
                        border.color: theme.selection
                        border.width: 1
                        Text {
                            anchors.centerIn: parent
                            text: modelData.name
                            font.pixelSize: 9
                            color: theme.bright_foreground
                        }
                    }
                }
            }

            Item { Layout.preferredHeight: 8 }

            // --- Save Button ---
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                Button {
                    text: "Save Preferences"
                    highlighted: true
                    onClicked: {
                        var selectedUser = userCombo.currentIndex > 0 ? userCombo.currentText : "";
                        var cfg = {
                            "daemonUrl": daemonInput.text.trim(),
                            "defaultUser": selectedUser,
                            "jellyfinWebUrl": jfWebInput.text.trim(),
                            "musicFolderUrl": musicFolderInput.text.trim(),
                            "autoClipboardDetect": clipCheck.checked
                        };
                        view.saveConfig(cfg);
                    }
                }
            }
        }
    }
}
