// qml/views/SettingsView.qml
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

Item {
    id: view
    required property Theme theme
    property string daemonUrl: "http://127.0.0.1:8095"
    property var usersList: []
    property string defaultBitrate: "320k"
    property string healthStatus: "Not tested"
    property bool isHealthy: false

    signal saveConfig(string newUrl, string newBitrate)

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

    ColumnLayout {
        anchors.fill: parent
        spacing: 16

        Text {
            text: "Backend Daemon Configuration"
            font.bold: true
            font.pixelSize: 14
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

        Text {
            text: "Active Omarchy Theme Palette"
            font.bold: true
            font.pixelSize: 14
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
                    height: 36
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

        Item { Layout.fillHeight: true }

        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            Button {
                text: "Save Preferences"
                highlighted: true
                onClicked: view.saveConfig(daemonInput.text.trim(), "320k")
            }
        }
    }
}
