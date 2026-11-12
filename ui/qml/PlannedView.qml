import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: planned
    required property var backend
    property bool gridMode: true
    PlannedSearchDialog { id: candidateDialog; backend: planned.backend }
    ListModel { id: presentation; dynamicRoles: true }
    property var shownEntries: []
    property string shownSearch: ""
    function updateEntries() {
        const rows = backend.plannedEntries
        const search = backend.searchTerm || ""
        const reset = shownSearch !== search
        if (!reset && JSON.stringify(rows) === JSON.stringify(shownEntries)) return
        let index = entries.indexAt(1, entries.contentY + 1)
        const delegate = index >= 0 ? entries.itemAtIndex(index) : null
        const anchor = !reset && delegate && shownEntries[index] ? {id: shownEntries[index].id,
            next: shownEntries[index + 1]?.id, index: index, offset: delegate.y - entries.contentY} : null
        const old = new Map(shownEntries.map(row => [row.id, JSON.stringify(row)]))
        const keys = reset ? [] : shownEntries.map(row => row.id)
        const wanted = rows.map(row => row.id), remaining = new Set(wanted)
        if (reset) presentation.clear()
        for (let i = keys.length - 1; i >= 0; --i)
            if (!remaining.has(keys[i])) { presentation.remove(i); keys.splice(i, 1) }
        for (let i = 0; i < rows.length; ++i) {
            if (keys[i] !== wanted[i]) {
                const found = keys.indexOf(wanted[i], i + 1)
                if (found >= 0) { presentation.move(found, i, 1); keys.splice(i, 0, keys.splice(found, 1)[0]) }
                else { presentation.insert(i, {entryData: rows[i]}); keys.splice(i, 0, wanted[i]) }
            }
            if (!reset && old.has(wanted[i]) && old.get(wanted[i]) !== JSON.stringify(rows[i]))
                presentation.setProperty(i, "entryData", rows[i])
        }
        shownEntries = rows; shownSearch = search
        if (reset) entries.positionViewAtBeginning()
        else if (anchor && rows.length) {
            let target = wanted.indexOf(anchor.id)
            if (target < 0) target = wanted.indexOf(anchor.next)
            if (target < 0) target = Math.min(anchor.index, rows.length - 1)
            entries.forceLayout(); entries.positionViewAtIndex(target, GridView.Beginning); entries.forceLayout()
            const item = entries.itemAtIndex(target)
            if (item) entries.contentY = Math.max(entries.originY, Math.min(item.y - anchor.offset, entries.originY + Math.max(0, entries.contentHeight - entries.height)))
        }
    }
    Connections { target: planned.backend; function onPlannedChanged() { planned.updateEntries() } }
    Component.onCompleted: updateEntries()

    function editEntry(entryId, title) {
        editDialog.entryId = entryId
        titleInput.text = title
        editDialog.open()
        titleInput.forceActiveFocus()
    }

    function linkEntry(entryId, animeId) {
        linkDialog.entryId = entryId
        urlInput.text = animeId ? "https://anilist.co/anime/" + animeId + "/" : ""
        linkDialog.open()
        urlInput.forceActiveFocus()
    }

    Dialog {
        id: linkDialog
        objectName: "plannedLinkDialog"
        property string entryId: ""
        title: entryId ? "Link AniList entry" : "Add to Planned from AniList"
        anchors.centerIn: Overlay.overlay
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        background: Rectangle { color: "#161a29"; radius: 14; border.color: "#262c3e" }
        contentItem: TextField {
            id: urlInput
            objectName: "plannedAniListUrl"
            implicitWidth: 380
            placeholderText: "https://anilist.co/anime/12345/"
            selectByMouse: true
            color: "#f5f6fb"
            Accessible.name: "AniList anime URL"
            background: Rectangle { radius: 8; color: "#0e111c"; border.color: "#262c3e" }
        }
        onAccepted: planned.backend.linkPlannedEntry(entryId, urlInput.text)
    }

    Dialog {
        id: editDialog
        property string entryId: ""
        title: entryId ? "Edit Planned Entry" : "Add to Planned"
        anchors.centerIn: Overlay.overlay
        modal: true
        standardButtons: Dialog.Save | Dialog.Cancel
        background: Rectangle { color: "#161a29"; radius: 14; border.color: "#262c3e" }
        contentItem: TextField {
            id: titleInput
            implicitWidth: 350
            placeholderText: "Anime title"
            color: "#f5f6fb"
            selectByMouse: true
            Accessible.name: "Planned anime title"
            background: Rectangle { radius: 8; color: "#0e111c"; border.color: "#262c3e" }
        }
        onOpened: standardButton(Dialog.Save).enabled = titleInput.text.trim().length > 0
        Connections {
            target: titleInput
            function onTextChanged() {
                if (editDialog.visible) editDialog.standardButton(Dialog.Save).enabled = titleInput.text.trim().length > 0
            }
        }
        onAccepted: planned.backend.savePlannedTitle(entryId, titleInput.text)
    }

    Dialog {
        id: removeDialog
        objectName: "plannedRemoveDialog"
        property string entryId: ""
        property string entryTitle: ""
        title: "Remove from Planned"
        anchors.centerIn: Overlay.overlay
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        background: Rectangle { color: "#161a29"; radius: 14; border.color: "#262c3e" }
        contentItem: Item {
            implicitWidth: 320
            implicitHeight: confirmationText.implicitHeight
            Text {
                id: confirmationText
                width: parent.width
                text: "Remove \"" + removeDialog.entryTitle + "\" from Planned?"
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                color: "#929bb0"
            }
        }
        onAccepted: planned.backend.removePlanned(entryId)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 12
        Flow {
            id: actionToolbar
            objectName: "plannedActionToolbar"
            Layout.fillWidth: true
            spacing: 8
            Button {
                id: addPlanned
                hoverEnabled: true
                text: "Add to Planned"
                onClicked: planned.editEntry("", "")
                contentItem: Text { text: addPlanned.text; color: addPlanned.enabled ? "#f5f6fb" : "#596176"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                background: Rectangle { color: addPlanned.enabled && addPlanned.down ? "#2c2447" : (addPlanned.enabled && addPlanned.hovered ? "#20263a" : "#161a29"); radius: 12; border.color: addPlanned.enabled && addPlanned.hovered ? "#555070" : "#262c3e" }
            }
            Button {
                id: refreshImports
                objectName: "refreshImportsButton"
                text: planned.backend.importsBusy ? "Checking imports…" : "Refresh imports"
                enabled: !planned.backend.importsBusy
                hoverEnabled: true
                onClicked: planned.backend.refreshImports()
                contentItem: Text { text: refreshImports.text; color: refreshImports.enabled ? "#f5f6fb" : "#596176"; verticalAlignment: Text.AlignVCenter }
                background: Rectangle { radius: 12; color: refreshImports.enabled && refreshImports.hovered ? "#20263a" : "#161a29"; border.color: refreshImports.enabled && refreshImports.hovered ? "#555070" : "#262c3e" }
            }
            Button {
                id: addFromAniList
                hoverEnabled: true
                text: "Add from AniList URL..."
                onClicked: planned.linkEntry("", 0)
                contentItem: Text { text: addFromAniList.text; color: addFromAniList.enabled ? "#f5f6fb" : "#596176"; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                background: Rectangle { color: addFromAniList.enabled && addFromAniList.down ? "#2c2447" : (addFromAniList.enabled && addFromAniList.hovered ? "#20263a" : "#161a29"); radius: 12; border.color: addFromAniList.enabled && addFromAniList.hovered ? "#555070" : "#262c3e" }
            }
            Item {
                visible: planned.backend.plannedBusy
                width: Math.min(240, actionToolbar.width)
                height: 26
                RowLayout {
                    anchors.fill: parent
                    BusyIndicator { running: parent.parent.visible; Layout.preferredWidth: 26; Layout.preferredHeight: 26 }
                    Text { Layout.fillWidth: true; text: "Looking up AniList entry…"; color: "#929bb0"; font.pixelSize: 12; elide: Text.ElideRight }
                }
            }
        }
        GridView {
            id: entries
            objectName: "plannedEntriesView"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: presentation
            cellWidth: planned.gridMode ? Math.max(158, Math.floor(width / Math.max(1, Math.floor(width / 176)))) : width
            cellHeight: planned.gridMode ? cellWidth * 1.48 : 64
            delegate: Item {
                required property var entryData
                readonly property var entry: entryData
                width: entries.cellWidth
                height: entries.cellHeight
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: 6
                    radius: 14
                    color: plannedMouse.containsMouse ? "#20263a" : "#161a29"
                    border.color: "#292f42"
                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 10
                        height: Math.max(0, parent.height - (entry.anilistTitle ? 84 : 70))
                        visible: planned.gridMode
                        radius: 10
                        color: "#201b35"
                        Image { id: poster; anchors.fill: parent; source: entry.localCoverUrl || ""; asynchronous: true; fillMode: Image.PreserveAspectCrop }
                        Text { anchors.centerIn: parent; visible: poster.status !== Image.Ready; text: "Planned"; color: "#c4bddc"; font.pixelSize: 14 }
                    }
                    Image {
                        visible: !planned.gridMode && status === Image.Ready
                        anchors.left: parent.left
                        anchors.leftMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        width: 30; height: 40
                        source: entry.localCoverUrl || ""
                        asynchronous: true
                        fillMode: Image.PreserveAspectCrop
                    }
                    Text {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 12
                        anchors.leftMargin: !planned.gridMode && entry.localCoverUrl ? 50 : 12
                        anchors.bottomMargin: entry.anilistTitle ? 26 : 12
                        height: planned.gridMode ? 38 : 24
                        text: entry.title
                        textFormat: Text.PlainText
                        color: "#f5f6fb"
                        font.pixelSize: 13
                        wrapMode: planned.gridMode ? Text.WordWrap : Text.NoWrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                    Text {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.bottom: parent.bottom
                        anchors.margins: 10
                        anchors.leftMargin: !planned.gridMode && entry.localCoverUrl ? 50 : 12
                        visible: !!entry.anilistTitle
                        text: entry.anilistTitle || ""
                        textFormat: Text.PlainText
                        color: "#929bb0"
                        font.pixelSize: 10
                        elide: Text.ElideRight
                    }
                }
                MouseArea {
                    id: plannedMouse
                    hoverEnabled: true
                    objectName: "plannedEntryMouse"
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    onClicked: function(mouse) { if (mouse.button === Qt.RightButton) entryMenu.popup() }
                    onDoubleClicked: planned.editEntry(entry.id, entry.title)
                }
                Menu {
                    id: entryMenu
                    objectName: "plannedEntryMenu"
                    width: 196
                    background: Rectangle { color: "#161a29"; radius: 10; border.color: "#262c3e" }
                    delegate: MenuItem {
                        id: menuAction
                        implicitHeight: 34
                        leftPadding: 12
                        rightPadding: 12
                        contentItem: Text { text: menuAction.text; color: menuAction.enabled ? "#f5f6fb" : "#596176"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight; font.pixelSize: 12 }
                        background: Rectangle { color: menuAction.enabled && menuAction.highlighted ? "#20263a" : "transparent"; radius: 6 }
                    }
                    Action { text: "Edit Planned Entry"; onTriggered: planned.editEntry(entry.id, entry.title) }
                    Action {
                        text: "Remove from Planned"
                        onTriggered: { removeDialog.entryId = entry.id; removeDialog.entryTitle = entry.title; removeDialog.open() }
                    }
                    Action { text: "Link AniList entry..."; onTriggered: planned.linkEntry(entry.id, entry.anilistId) }
                    Action { text: "Unlink AniList entry"; enabled: !!entry.anilistId; onTriggered: planned.backend.unlinkPlannedEntry(entry.id) }
                    Action { text: "Search AniList"; onTriggered: candidateDialog.searchEntry(entry.id) }
                }
            }
            Text {
                anchors.centerIn: parent
                visible: entries.count === 0
                text: planned.backend.isSearching ? "No Planned entries match your search." : "Add an anime title to plan for later."
                color: "#929bb0"
            }
            ScrollBar.vertical: ScrollBar {}
        }
    }
}
