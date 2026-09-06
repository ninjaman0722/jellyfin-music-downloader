// qml/views/ProgressView.qml
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"
import Quickshell

Item {
    id: view
    required property Theme theme
    property var activeJob: ({})
    property real percentage: (activeJob && activeJob.percentage !== undefined) ? activeJob.percentage : 0
    property int currentTrack: (activeJob && activeJob.current_track !== undefined) ? activeJob.current_track : 0
    property int totalTracks: (activeJob && activeJob.total_tracks !== undefined) ? activeJob.total_tracks : 0
    property string currentTitle: (activeJob && activeJob.current_title) ? activeJob.current_title : "Ready"
    property string speed: (activeJob && activeJob.speed) ? activeJob.speed : "0.0 MB/s"
    property int etaSeconds: (activeJob && activeJob.eta_seconds !== undefined) ? activeJob.eta_seconds : 0
    property string stageName: (activeJob && activeJob.stage_name) ? activeJob.stage_name : "Ready"
    property var logModel
    property var completedTracksModel
    property string appState: "idle"
    property bool showLogs: false

    signal cancelRequested()
    signal doneClicked()

    ColumnLayout {
        anchors.fill: parent
        spacing: 14

        // Top Status Header
        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Rectangle {
                width: 10
                height: 10
                radius: 5
                color: view.appState === "downloading" ? theme.green : (view.appState === "error" ? theme.red : theme.cyan)
            }
            Text {
                text: view.stageName || "Active Ingest"
                font.bold: true
                font.pixelSize: 14
                color: theme.bright_foreground
            }
            Item { Layout.fillWidth: true }
            Text {
                text: "Speed: " + (view.speed || "0.0 MB/s") + " | ETA: " + Math.floor((view.etaSeconds || 0) / 60) + "m " + ((view.etaSeconds || 0) % 60) + "s"
                font.pixelSize: 11
                color: theme.light_foreground
            }
        }

        // Animated Progress Bar
        ProgressBar {
            Layout.fillWidth: true
            Layout.preferredHeight: 12
            value: Math.min(Math.max((view.percentage || 0) / 100.0, 0.0), 1.0)
        }

        // Current Track Indicator
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            radius: 8
            color: theme.background
            border.color: theme.selection
            border.width: 1

            RowLayout {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 8
                Text {
                    text: "▶ " + (view.currentTitle || "Waiting for download...")
                    font.pixelSize: 12
                    font.bold: true
                    color: theme.foreground
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                Text {
                    text: "[" + view.currentTrack + " / " + view.totalTracks + "]"
                    font.pixelSize: 11
                    color: theme.accent
                }
            }
        }

        // Completed Tracks List
        Text {
            text: "Finished Tracks (" + (view.completedTracksModel ? view.completedTracksModel.count : 0) + ")"
            font.bold: true
            font.pixelSize: 12
            color: theme.bright_foreground
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: !view.showLogs
            Layout.preferredHeight: view.showLogs ? 120 : 220
            radius: 8
            color: theme.background

            ListView {
                anchors.fill: parent
                anchors.margins: 8
                model: view.completedTracksModel
                clip: true
                delegate: RowLayout {
                    width: parent.width
                    spacing: 8
                    Text { text: "✔"; color: theme.green; font.pixelSize: 11 }
                    Text { text: model.title + " - " + model.artist; color: theme.foreground; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
                    Text { text: model.lyrics ? "[LRC]" : ""; color: theme.cyan; font.pixelSize: 9; font.bold: true }
                }
            }
        }

        // Expandable Log Console
        RowLayout {
            Layout.fillWidth: true
            Button {
                text: view.showLogs ? "▲ Hide Logs" : "▼ Show Real-Time Logs (" + (view.logModel ? view.logModel.count : 0) + " lines)"
                flat: true
                onClicked: view.showLogs = !view.showLogs
            }
            Item { Layout.fillWidth: true }
            Button {
                text: "📋 Copy Logs"
                visible: view.showLogs
                flat: true
                onClicked: {
                    var str = "";
                    if (view.logModel) {
                        for (var i = 0; i < view.logModel.count; i++) str += view.logModel.get(i).message + "\n";
                    }
                    Quickshell.execDetached(["wl-copy", "--", str]);
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 140
            visible: view.showLogs
            radius: 8
            color: theme.darker_background
            border.color: theme.selection
            border.width: 1

            ListView {
                id: logListView
                anchors.fill: parent
                anchors.margins: 8
                model: view.logModel
                clip: true
                onCountChanged: logListView.positionViewAtEnd()
                delegate: Text {
                    width: parent.width
                    text: "[" + model.time + "] " + model.message
                    font.family: "JetBrains Mono, monospace"
                    font.pixelSize: 10
                    color: model.level === "ERROR" ? theme.red : (model.level === "WARNING" ? theme.yellow : theme.light_foreground)
                    wrapMode: Text.WrapAnywhere
                }
            }
        }

        // Job Action Controls
        RowLayout {
            Layout.fillWidth: true
            Button {
                text: "🛑 Cancel Download"
                visible: view.appState === "downloading"
                onClicked: view.cancelRequested()
            }
            Item { Layout.fillWidth: true }
            Button {
                text: "✔ Return to Ingest"
                highlighted: true
                visible: view.appState === "finished" || view.appState === "error"
                onClicked: view.doneClicked()
            }
        }
    }
}
