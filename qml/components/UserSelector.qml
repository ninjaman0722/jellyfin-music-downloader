// qml/components/UserSelector.qml
import QtQuick
import QtQuick.Layouts
import "../theme"

Flow {
    id: comp
    required property Theme theme
    property var users: []
    property string selectedUserId: ""
    signal userSelected(string userId)

    spacing: 8

    Text {
        visible: comp.users.length === 0
        text: "⚠️ No user profiles loaded — configure daemon connection in Settings ⚙️"
        color: theme.yellow
        font.pixelSize: 12
        font.italic: true
    }

    Repeater {
        model: comp.users
        delegate: Rectangle {
            id: userPill
            readonly property bool isSelected: modelData.id === comp.selectedUserId
            width: pillRow.width + 20
            height: 32
            radius: 16
            color: isSelected ? theme.lighter_background : (pillMa.containsMouse ? theme.background : "transparent")
            border.color: isSelected ? theme.accent : theme.selection
            border.width: isSelected ? 2 : 1

            MouseArea {
                id: pillMa
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: comp.userSelected(modelData.id)
            }

            RowLayout {
                id: pillRow
                anchors.centerIn: parent
                spacing: 6

                Text {
                    text: modelData.is_admin ? "󰋋" : "󰋊"
                    font.pixelSize: 12
                    color: userPill.isSelected ? theme.accent : theme.light_foreground
                }
                Text {
                    text: modelData.name + ((modelData.playlists && modelData.playlists.length) ? (" (" + modelData.playlists.length + ")") : "")
                    font.pixelSize: 12
                    font.bold: userPill.isSelected
                    color: userPill.isSelected ? theme.bright_foreground : theme.foreground
                }
            }
        }
    }
}
