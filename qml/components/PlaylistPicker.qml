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
    readonly property bool requiresUser: comp.isPlaylistMode || comp.mode === "existing" || comp.mode === "new"

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

    // Dynamic reactive resolved playlist name for loose songs
    readonly property string resolvedPlaylistName: {
        if (comp.truePlaylistUrlCount > 0) {
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
                if (comp.trackUrlCount > 0) {
                    return comp.trackUrlCount === 1 ? "Destination for 1 loose song:" : ("Destination for " + comp.trackUrlCount + " loose songs:")
                } else if (comp.hasArtistUrl) {
                    return comp.artistMode === "discography"
                        ? "🎤 Artist: Downloading full discography to server library"
                        : "🎤 Artist: Downloading popular top tracks to server library"
                } else if (comp.hasAlbumUrl) {
                    return "💿 Album: Downloading full album to server library"
                } else if (comp.playlistUrlCount > 1) {
                    return "🎵 " + comp.playlistUrlCount + " Playlists: Each will auto-create its own Jellyfin playlist"
                } else if (comp.playlistUrlCount === 1) {
                    return comp.detectedPlaylistName
                        ? ("Playlist: 🎵 \"" + comp.detectedPlaylistName + "\" (Auto-Sync)")
                        : "Playlist: 🎵 Auto-syncing from link"
                } else {
                    return "Ready for music links (albums, artists, playlists, tracks)..."
                }
            }
            font.bold: true
            font.pixelSize: 12
            color: (comp.trackUrlCount > 0 || comp.hasArtistUrl || comp.hasAlbumUrl) ? theme.bright_foreground : theme.accent
        }

        Item { Layout.fillWidth: true }

        // Mode Radio Buttons: ONLY shown when there are loose songs needing destination routing!
        RowLayout {
            visible: comp.trackUrlCount > 0
            spacing: 6

            RadioButton {
                text: "Library Only"
                checked: comp.mode === "none"
                font.pixelSize: 11
                onClicked: comp.mode = "none"
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
        text: "🎵 " + comp.playlistUrlCount + (comp.playlistUrlCount === 1 ? " playlist" : " playlists") + " in queue will automatically create " + (comp.playlistUrlCount === 1 ? "its own playlist." : "their own separate playlists.")
        font.pixelSize: 11
        color: theme.cyan
    }

    // Informational note for loose songs, albums, and artist discography
    Text {
        visible: (comp.trackUrlCount > 0 && comp.mode === "none") || comp.hasAlbumUrl || comp.hasArtistUrl
        text: comp.hasArtistUrl
            ? "ℹ️ Saves artist releases directly to /mnt/media/music/ for all Jellyfin users."
            : (comp.hasAlbumUrl
                ? "ℹ️ Saves album directly to /mnt/media/music/ for all Jellyfin users."
                : (comp.trackUrlCount === 1
                    ? "ℹ️ Saves 1 song directly to your Jellyfin library without a playlist."
                    : ("ℹ️ Saves " + comp.trackUrlCount + " songs directly to your Jellyfin library without a playlist.")))
        font.pixelSize: 11
        color: theme.light_foreground
        font.italic: true
    }

    ComboBox {
        id: plCombo
        visible: comp.trackUrlCount > 0 && comp.mode === "existing"
        Layout.fillWidth: true
        model: comp.availablePlaylistNames
    }

    TextField {
        id: newPlInput
        visible: comp.trackUrlCount > 0 && comp.mode === "new"
        Layout.fillWidth: true
        placeholderText: "Enter playlist name for loose songs..."
    }
}
