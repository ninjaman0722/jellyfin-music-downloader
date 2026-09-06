// qml/components/AnalysisCard.qml
import QtQuick
import QtQuick.Layouts
import "../theme"

Rectangle {
    id: comp
    required property Theme theme
    property var analysisData: null
    property alias analysis: comp.analysisData
    property int totalTracks: analysisData ? (analysisData.total_tracks || 0) : 0
    property int existingTracks: analysisData ? (analysisData.existing_tracks || 0) : 0
    property int missingTracks: analysisData ? (analysisData.missing_tracks || 0) : 0

    radius: 10
    color: theme.background
    border.color: theme.selection
    border.width: 1
    implicitHeight: (comp.analysisData && comp.analysisData.detected_playlists && comp.analysisData.detected_playlists.length > 1) ? 90 : 85

    RowLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 16

        ColumnLayout {
            spacing: 2
            Layout.fillWidth: true
            Layout.maximumWidth: 340

            Text {
                text: {
                    if (!comp.analysisData) return "Playlist Summary";
                    var pls = comp.analysisData.detected_playlists;
                    if (pls && pls.length > 1) {
                        return "🎵 " + pls.length + " Playlists Detected";
                    }
                    return comp.analysisData.playlist_name || "Playlist Summary";
                }
                font.bold: true
                font.pixelSize: 13
                color: theme.bright_foreground
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            Text {
                text: {
                    if (!comp.analysisData) return "";
                    var pls = comp.analysisData.detected_playlists;
                    if (pls && pls.length > 1) {
                        return pls.join(" • ");
                    }
                    return "";
                }
                visible: text.length > 0
                font.pixelSize: 10
                color: theme.accent
                elide: Text.ElideRight
                Layout.fillWidth: true
            }

            Text {
                text: comp.analysisData && comp.analysisData.resolve_time_ms !== undefined ? ("Diff computed in " + comp.analysisData.resolve_time_ms.toFixed(1) + "ms") : ""
                font.pixelSize: 10
                color: theme.light_foreground
            }
        }

        Item { Layout.fillWidth: true }

        // Stat 1: Total
        ColumnLayout {
            spacing: 2
            Text {
                text: String(comp.totalTracks)
                font.bold: true
                font.pixelSize: 16
                color: theme.bright_foreground
            }
            Text {
                text: "Total Tracks"
                font.pixelSize: 10
                color: theme.light_foreground
            }
        }

        // Stat 2: In Library
        ColumnLayout {
            spacing: 2
            Text {
                text: String(comp.existingTracks)
                font.bold: true
                font.pixelSize: 16
                color: theme.green
            }
            Text {
                text: "In Library"
                font.pixelSize: 10
                color: theme.green
            }
        }

        // Stat 3: To Download
        ColumnLayout {
            spacing: 2
            Text {
                text: String(comp.missingTracks)
                font.bold: true
                font.pixelSize: 16
                color: theme.accent
            }
            Text {
                text: "To Download"
                font.pixelSize: 10
                color: theme.accent
            }
        }
    }
}
