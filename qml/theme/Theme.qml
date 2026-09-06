// qml/theme/Theme.qml - Dynamic Omarchy Theming Engine
import QtQuick
import Quickshell
import Quickshell.Io

QtObject {
    id: theme

    // Default Fallback Palette (Nord Dark)
    property string mode: "dark"
    property color background: "#2e3440"
    property color dark_background: "#222730"
    property color darker_background: "#191c23"
    property color lighter_background: "#3b4252"
    property color foreground: "#d8dee9"
    property color dark_foreground: "#667080"
    property color light_foreground: "#adb5c4"
    property color bright_foreground: "#eceff4"
    property color accent: "#81a1c1"
    property color selection: "#434c5e"
    property color muted: "#4c566a"
    property color red: "#bf616a"
    property color yellow: "#ebcb8b"
    property color orange: "#d5967a"
    property color green: "#a3be8c"
    property color cyan: "#88c0d0"
    property color blue: "#81a1c1"
    property color magenta: "#b48ead"

    readonly property string themeFilePath: (Quickshell.env("HOME") || "") + "/.local/state/omarchy/current/theme/colors.toml"

    function parseToml(text) {
        if (!text) return;
        var lines = text.split("\n");
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i].trim();
            if (!line || line.indexOf("#") === 0) continue;
            var parts = line.split("=");
            if (parts.length === 2) {
                var key = parts[0].trim();
                var val = parts[1].trim();
                if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
                    val = val.substring(1, val.length - 1);
                }
                if (theme.hasOwnProperty(key)) {
                    theme[key] = val;
                }
            }
        }
    }

    property FileView colorsFile: FileView {
        path: theme.themeFilePath
        watchChanges: true
        printErrors: false
        onLoaded: theme.parseToml(text())
        onFileChanged: reload()
    }
}
