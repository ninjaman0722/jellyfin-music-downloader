// qml/components/PlaylistPicker.qml
import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "../theme"

ColumnLayout {
    id: comp
    required property Theme theme
    property var users: []
    property string activeUserId: ""
    property var urls: []
    property string detectedPlaylistName: ""
    property string mode: isPlaylistMode ? "auto" : "none" // "auto", "existing", "new", "none"

    spacing: 8

    readonly property bool hasUrls: comp.urls && comp.urls.length > 0

    readonly property bool hasArtistUrl: {
        if (!comp.urls) return false;
        for (var i = 0; i < comp.urls.length; i++) {
            if (comp.urls[i].toLowerCase().indexOf("artist") !== -1) return true;
        }
        return false;
    }

    readonly property bool hasAlbumUrl: {
        if (!comp.urls) return false;
        for (var i = 0; i < comp.urls.length; i++) {
            if (comp.urls[i].toLowerCase().indexOf("album") !== -1) return true;
        }
        return false;
    }

    property string artistMode: "discography" // "discography" or "top_tracks"

    readonly property int truePlaylistUrlCount: {
        if (!comp.urls) return 0;
        var c = 0;
        for (var i = 0; i < comp.urls.length; i++) {
            var u = comp.urls[i].toLowerCase();
            if (u.indexOf("playlist") !== -1 || u.indexOf("list=") !== -1) {
                c++;
            }
        }
        return c;
    }

    readonly property int playlistUrlCount: comp.truePlaylistUrlCount

    readonly property int trackUrlCount: {
        if (!comp.urls) return 0;
        var c = 0;
        for (var i = 0; i < comp.urls.length; i++) {
            var u = comp.urls[i].toLowerCase();
            if (u.indexOf("playlist") === -1 && u.indexOf("album") === -1 && u.indexOf("artist") === -1 && u.indexOf("list=") === -1) {
                c++;
            }
        }
        return c;
    }

    readonly property bool isPlaylistMode: comp.truePlaylistUrlCount > 0
    readonly property bool requiresUser: comp.mode === "auto" || comp.mode === "existing" || comp.mode === "new"

    onPlaylistUrlCountChanged: {
        if (comp.playlistUrlCount > 0 && comp.mode === "none") {
            comp.mode = "auto";
        }
    }
    onTrackUrlCountChanged: {
        if (comp.playlistUrlCount === 0 && comp.mode === "auto") {
            comp.mode = "none";
        }
    }

    readonly property var activeUserObj: {
        if (!comp.users || !comp.users.length) return null;
        for (var i = 0; i < comp.users.length; i++) {
            if (comp.users[i].id === comp.activeUserId) return comp.users[i];
        }
        return comp.users[0] || null;
    }

    readonly property var availablePlaylists: (activeUserObj && activeUserObj.playlists) ? activeUserObj.playlists : []

    readonly property var availablePlaylistNames: {
        var names = [];
        if (comp.availablePlaylists && comp.availablePlaylists.length) {
            for (var i = 0; i < comp.availablePlaylists.length; i++) {
                var p = comp.availablePlaylists[i];
                var n = (p && typeof p === "object") ? (p.name || "") : String(p || "");
                if (n) names.push(n);
            }
        }
        return names;
    }

    // Dynamic reactive resolved playlist destination
    readonly property string resolvedPlaylistName: {
        if (comp.playlistUrlCount > 0 && comp.mode === "auto") {
            return "AUTO";
        }
        if (comp.mode === "existing") {
            return plCombo.currentText || (comp.availablePlaylistNames.length > 0 ? comp.availablePlaylistNames[0] : "");
        } else if (comp.mode === "new") {
            return newPlInput.text.trim();
        } else {
            return "__NO_PLAYLIST__";
        }
    }

    // Header & Options Row
    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Text {
            text: {
                if (comp.playlistUrlCount > 0) {
                    if (comp.mode === "auto") {
                        if (comp.playlistUrlCount === 1) {
                            return comp.detectedPlaylistName
                                ? ("Playlist: 🎵 \"" + comp.detectedPlaylistName + "\" (Auto-Sync)")
                                : "Playlist: 🎵 Auto-syncing from link";
                        } else {
                            return "🎵 " + comp.playlistUrlCount + " Playlists: Each will auto-create its own Jellyfin playlist";
                        }
                    } else if (comp.mode === "existing") {
                        return "Playlist Destination: Merge into existing playlist";
                    } else if (comp.mode === "new") {
                        return "Playlist Destination: Custom new playlist";
                    } else {
                        return "Playlist Destination: Library only (no playlist)";
                    }
                } else if (comp.trackUrlCount > 0) {
                    return comp.trackUrlCount === 1 ? "Destination for 1 loose song:" : ("Destination for " + comp.trackUrlCount + " loose songs:");
                } else if (comp.hasArtistUrl) {
                    return comp.artistMode === "discography"
                        ? "🎤 Artist: Downloading full discography to server library"
                        : "🎤 Artist: Downloading popular top tracks to server library";
                } else if (comp.hasAlbumUrl) {
                    return "💿 Album: Downloading full album to server library";
                } else {
                    return "Ready for music links (albums, artists, playlists, tracks)...";
                }
            }
            font.bold: true
            font.pixelSize: 12
            color: (comp.hasUrls) ? theme.bright_foreground : theme.accent
        }

        Item { Layout.fillWidth: true }

        // Mode Radio Buttons: shown when playlists OR loose tracks are present
        RowLayout {
            visible: comp.playlistUrlCount > 0 || comp.trackUrlCount > 0
            spacing: 6

            RadioButton {
                visible: comp.playlistUrlCount > 0
                text: "Auto-Sync"
                checked: comp.mode === "auto"
                font.pixelSize: 11
                onClicked: comp.mode = "auto"
            }
            RadioButton {
                text: comp.availablePlaylistNames.length > 0 ? ("Existing (" + comp.availablePlaylistNames.length + ")") : "Existing Playlist"
                checked: comp.mode === "existing"
                enabled: comp.availablePlaylistNames.length > 0
                font.pixelSize: 11
                onClicked: comp.mode = "existing"
            }
            RadioButton {
                text: "New Playlist"
                checked: comp.mode === "new"
                font.pixelSize: 11
                onClicked: comp.mode = "new"
            }
            RadioButton {
                text: "Library Only"
                checked: comp.mode === "none"
                font.pixelSize: 11
                onClicked: comp.mode = "none"
            }
        }
    }

    // Artist Resolution Mode Toggle (When artist link is entered)
    RowLayout {
        visible: comp.hasArtistUrl
        Layout.fillWidth: true
        spacing: 12

        Text {
            text: "🎤 Artist Ingest:"
            font.bold: true
            font.pixelSize: 11
            color: theme.accent
        }

        RadioButton {
            text: "Full Discography (All Albums)"
            checked: comp.artistMode === "discography"
            font.pixelSize: 11
            onClicked: comp.artistMode = "discography"
        }

        RadioButton {
            text: "Top Tracks (10 Popular)"
            checked: comp.artistMode === "top_tracks"
            font.pixelSize: 11
            onClicked: comp.artistMode = "top_tracks"
        }

        Item { Layout.fillWidth: true }
    }

    // Subtitle note when both playlists AND loose songs are present
    Text {
        visible: comp.playlistUrlCount > 0 && comp.trackUrlCount > 0
        text: comp.mode === "auto"
            ? ("🎵 " + comp.playlistUrlCount + " playlist(s) and " + comp.trackUrlCount + " loose song(s). Playlists auto-sync, loose songs route to library.")
            : ("🎵 All " + (comp.playlistUrlCount + comp.trackUrlCount) + " item(s) will route to selected destination.")
        font.pixelSize: 11
        color: theme.cyan
    }

    // Informational note for loose songs, albums, and artist discography
    Text {
        visible: comp.mode === "none" || comp.hasAlbumUrl || comp.hasArtistUrl
        text: comp.hasArtistUrl
            ? "ℹ️ Saves artist releases directly to /mnt/media/music/ for all Jellyfin users."
            : (comp.hasAlbumUrl
                ? "ℹ️ Saves album directly to /mnt/media/music/ for all Jellyfin users."
                : (comp.trackUrlCount === 1
                    ? "ℹ️ Saves 1 song directly to your Jellyfin library without a playlist."
                    : "ℹ️ Ingests audio directly into server-wide Jellyfin library without creating a playlist."))
        font.pixelSize: 11
        color: theme.light_foreground
        font.italic: true
    }

    ComboBox {
        id: plCombo
        visible: (comp.playlistUrlCount > 0 || comp.trackUrlCount > 0) && comp.mode === "existing"
        Layout.fillWidth: true
        model: comp.availablePlaylistNames
    }

    TextField {
        id: newPlInput
        visible: (comp.playlistUrlCount > 0 || comp.trackUrlCount > 0) && comp.mode === "new"
        Layout.fillWidth: true
        placeholderText: comp.playlistUrlCount > 0 ? "Enter target playlist name..." : "Enter playlist name for loose songs..."
    }
}
