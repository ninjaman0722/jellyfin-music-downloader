// qml/components/ToastBanner.qml
import QtQuick
import QtQuick.Layouts
import "../theme"

Rectangle {
    id: toast
    required property Theme theme
    property string message: ""
    property string level: "info" // "info", "success", "warning", "error"

    anchors.top: parent.top
    anchors.topMargin: 12
    anchors.horizontalCenter: parent.horizontalCenter
    width: Math.min(parent.width - 40, contentRow.width + 30)
    height: 32
    radius: 16
    color: theme.lighter_background
    border.color: level === "error" ? theme.red : (level === "success" ? theme.green : (level === "warning" ? theme.yellow : theme.accent))
    border.width: 1
    z: 100
    opacity: 0.0
    visible: opacity > 0.0

    Behavior on opacity { NumberAnimation { duration: 180 } }

    Timer {
        id: dismissTimer
        interval: 3400
        onTriggered: toast.opacity = 0.0
    }

    function show(msg, lvl) {
        toast.message = msg;
        toast.level = lvl || "info";
        toast.opacity = 1.0;
        dismissTimer.restart();
    }

    RowLayout {
        id: contentRow
        anchors.centerIn: parent
        spacing: 8
        Text {
            text: toast.level === "error" ? "🛑" : (toast.level === "success" ? "✔" : "ℹ")
            font.pixelSize: 11
        }
        Text {
            text: toast.message
            font.pixelSize: 11
            font.bold: true
            color: theme.bright_foreground
        }
    }
}
