import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

ApplicationWindow {
    id: window
    width: 1280
    height: 800
    minimumWidth: 900
    minimumHeight: 600
    visible: true
    title: "JAnimeVault"
    color: "#0b0d16"
    PlannedSearchDialog { id: librarySearchDialog; backend: controller }

    // Theme tokens are kept together so a future light theme can replace them.
    readonly property color page: "#0b0d16"
    readonly property color sidebar: "#10131f"
    readonly property color surface: "#161a29"
    readonly property color surfaceHover: "#20263a"
    readonly property color line: "#262c3e"
    readonly property color primary: "#f5f6fb"
    readonly property color muted: "#929bb0"
    readonly property color accent: "#aa8cff"
    readonly property color accentSoft: "#2c2447"
    readonly property color green: "#55d69b"
    property bool gridView: true
    // Client-height only: independent of the layout sizes this mode changes.
    readonly property bool compactLayout: height < 840
    readonly property int contentInset: compactLayout ? 12 : 18
    property int selectedItemIndex: -1
    // Keep the presentation model alive across metadata snapshots from Python.
    ListModel { id: libraryPresentation; dynamicRoles: true }
    property var presentedItems: []
    property var folderStructure: []
    property string presentedContext: ""

    function itemIdentity(item) {
        return item.folderKey || JSON.stringify([item.sourceId, item.path.replace(/\\/g, "/").toLowerCase()])
    }
    function browsingContext() {
        const source = controller.currentView === "source"
        return JSON.stringify([controller.currentView, source ? controller.selectedDrive : "", source ? controller.currentPath : "", controller.searchTerm])
    }
    function activeLibraryView() {
        if (controller.currentView === "planned" || controller.currentView === "to_archive") return null
        return mixedFolderGrid ? folderContentsGrid : (gridView ? mediaGrid : mediaList)
    }
    function updatePresentation() {
        const items = controller.items
        const context = browsingContext()
        const reset = context !== presentedContext
        const view = activeLibraryView()
        let anchor = null
        const selectedKey = selectedItemIndex >= 0 && selectedItemIndex < presentedItems.length ? itemIdentity(presentedItems[selectedItemIndex]) : ""
        if (!reset && view && presentedItems.length) {
            let row = -1
            for (let offset = 1; row < 0 && offset <= 9; offset += 2)
                row = view.indexAt(1, Math.max(view.originY, view.contentY) + offset)
            const delegate = row >= 0 ? view.itemAtIndex(row) : null
            const original = view === folderContentsGrid && row >= 0 ? view.model[row].indices[0] : row
            if (delegate && original >= 0 && original < presentedItems.length) {
                anchor = {key: itemIdentity(presentedItems[original]), index: original,
                    next: original + 1 < presentedItems.length ? itemIdentity(presentedItems[original + 1]) : "",
                    previous: original > 0 ? itemIdentity(presentedItems[original - 1]) : "",
                    offset: delegate.y - view.contentY}
            }
        }
        const oldKeys = presentedItems.map(itemIdentity)
        const previousData = new Map(presentedItems.map(item => [itemIdentity(item), JSON.stringify(item)]))
        const newKeys = items.map(itemIdentity)
        const structureChanged = reset || JSON.stringify(oldKeys) !== JSON.stringify(newKeys) ||
            presentedItems.some((item, i) => i < items.length && item.isFolder !== items[i].isFolder)
        if (reset) libraryPresentation.clear()
        const keys = reset ? [] : oldKeys.slice()
        const remaining = new Set(newKeys)
        for (let i = keys.length - 1; i >= 0; --i) {
            if (!remaining.has(keys[i])) {
                libraryPresentation.remove(i)
                keys.splice(i, 1)
            }
        }
        for (let i = 0; i < items.length; ++i) {
            if (keys[i] !== newKeys[i]) {
                const found = keys.indexOf(newKeys[i], i + 1)
                if (found >= 0) {
                    libraryPresentation.move(found, i, 1)
                    keys.splice(i, 0, keys.splice(found, 1)[0])
                } else {
                    libraryPresentation.insert(i, {entry: items[i]})
                    keys.splice(i, 0, newKeys[i])
                }
            }
            if (!reset && previousData.has(newKeys[i]) && previousData.get(newKeys[i]) !== JSON.stringify(items[i]))
                libraryPresentation.setProperty(i, "entry", items[i])
        }
        if (libraryPresentation.count > items.length)
            libraryPresentation.remove(items.length, libraryPresentation.count - items.length)
        presentedItems = items
        presentedContext = context
        if (structureChanged) folderStructure = items.map((item, index) => ({isFolder: item.isFolder, index: index}))
        if (reset) {
            selectedItemIndex = -1
            mediaGrid.positionViewAtBeginning(); mediaList.positionViewAtBeginning(); folderContentsGrid.positionViewAtBeginning()
        } else {
            if (selectedKey) selectedItemIndex = newKeys.indexOf(selectedKey)
            if (anchor && items.length && structureChanged) {
                let index = newKeys.indexOf(anchor.key)
                if (index < 0) index = newKeys.indexOf(anchor.next)
                if (index < 0) index = newKeys.indexOf(anchor.previous)
                if (index < 0) index = Math.min(anchor.index, items.length - 1)
                let row = index
                if (view === folderContentsGrid) row = view.model.findIndex(r => r.indices.indexOf(index) >= 0)
                if (row >= 0) {
                    view.forceLayout()
                    view.positionViewAtIndex(row, ListView.Beginning)
                    view.forceLayout()
                    const delegate = view.itemAtIndex(row)
                    if (delegate) view.contentY = Math.max(view.originY, Math.min(delegate.y - anchor.offset, view.originY + Math.max(0, view.contentHeight - view.height)))
                }
            }
        }
        if (view === mediaGrid) viewportCoverTimer.restart()
        else if (view === folderContentsGrid) view.scheduleCovers()
    }
    Connections { target: controller; function onItemsChanged() { window.updatePresentation() } }
    Component.onCompleted: updatePresentation()
    readonly property bool mixedFolderGrid: gridView && controller.currentView === "source" && controller.selectedDrive.length > 0 && controller.currentPath.length > 0 && !controller.isSearching

    component LibraryCard: Item {
        objectName: "libraryCoverCard"
        required property var modelData
        required property int index
        readonly property var item: modelData
        width: mediaGrid.cellWidth
        height: mediaGrid.cellHeight
        property bool selected: index === window.selectedItemIndex

        Rectangle {
            id: card
            objectName: "libraryCardBackground"
            anchors.fill: parent
            anchors.margins: 6
            radius: 14
            color: cardMouse.containsMouse ? window.surfaceHover : window.surface
            border.width: parent.selected ? 2 : 1
            border.color: parent.selected ? window.accent : "#292f42"
            Behavior on color { ColorAnimation { duration: 120 } }
            Behavior on border.color { ColorAnimation { duration: 120 } }

            Column {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 10
                Rectangle {
                    width: parent.width
                    // Keep the text area inside every fixed-height grid cell.
                    // The cover remains as tall as possible without displacing its labels.
                    height: Math.max(0, Math.min(width * 1.26,
                        parent.height - titleText.height - subtitleText.height - parent.spacing * 2))
                    radius: 10
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: item.isFolder ? "#413569" : "#26324c" }
                        GradientStop { position: 1.0; color: item.isFolder ? "#201b35" : "#182134" }
                    }
                    // A future cover image can replace this placeholder without changing card layout.
                    Image {
                        id: coverImage
                            objectName: "libraryCoverImage"
                        anchors.fill: parent
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        cache: true
                        source: item.manualId ? item.coverUrl : (item.coverUrl || controller.coverUrlFor(item.coverKey))
                        visible: item.isFolder && status === Image.Ready
                    }
                    Column {
                        anchors.centerIn: parent
                        spacing: 7
                        visible: !coverImage.visible
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: item.isFolder ? "▰" : "◌"; color: item.isFolder ? "#d5c8ff" : "#a9c6ff"; font.pixelSize: 34 }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: item.isFolder ? "FOLDER" : "FILE"; color: "#c4bddc"; font.pixelSize: 10; font.weight: Font.DemiBold; font.letterSpacing: 1.2 }
                    }
                }
                Text {
                    id: titleText
                    width: parent.width
                    height: 34
                    text: item.name
                    color: window.primary
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                    maximumLineCount: 2
                    wrapMode: Text.WordWrap
                    verticalAlignment: Text.AlignTop
                }
                Text {
                    id: subtitleText
                    width: parent.width - (item.isFolder ? 54 : 0)
                    height: 16
                    text: controller.currentView !== "source" ? item.sourceLabel + " · " + item.sourceStatus : (item.isFolder ? "Folder" : (item.size.length > 0 ? item.size : "File"))
                    color: window.muted
                    font.pixelSize: 11
                    elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                }
            }
        }
        MouseArea {
            id: cardMouse
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onClicked: function(mouse) {
                window.selectedItemIndex = index
                if (window.mixedFolderGrid) folderContentsGrid.forceActiveFocus()
                if (mouse.button === Qt.RightButton && item.isFolder)
                    cardFolderMenu.popup()
            }
            onDoubleClicked: controller.openCardItem(item.sourceId, item.path, item.isFolder)
        }
        FavoriteHeart {
            visible: item.isFolder
            width: 24
            height: 18
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.rightMargin: 14
            anchors.bottomMargin: 15
            z: 1
            favorited: !!item.isFavorite
            onClicked: controller.setFavorite(item.sourceId, item.path, !item.isFavorite)
        }
        WatchBookmark {
            visible: item.isFolder
            width: 24; height: 18
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.rightMargin: 40
            anchors.bottomMargin: 15
            z: 1
            favorited: !!item.isToWatch
            onClicked: controller.setToWatch(item.sourceId, item.path, !item.isToWatch)
        }
        Menu {
            id: cardFolderMenu
            objectName: "libraryCardMenu"
            width: 188
            background: Rectangle { color: window.surface; radius: 10; border.color: window.line; border.width: 1 }
            SourceMenuItem { text: "Open folder"; onTriggered: controller.openCardInExplorer(item.sourceId, item.path) }
            SourceMenuItem { text: "Copy name"; onTriggered: controller.copyFolderName(item.name) }
            SourceMenuItem { text: "Search on the web"; onTriggered: controller.searchFolderOnWeb(item.name, item.sourceId, item.path) }
            MenuSeparator { contentItem: Rectangle { implicitHeight: 1; color: window.line } }
            SourceMenuItem {
                text: "Exclude from indexer"
                onTriggered: {
                    excludeFolderDialog.relativePath = item.path
                    excludeFolderDialog.sourceId = item.sourceId
                    excludeFolderDialog.folderName = item.name
                    excludeFolderDialog.open()
                }
            }
            SourceMenuItem { text: "Link AniList entry..."; onTriggered: window.openAniListLink(item) }
            SourceMenuItem { text: "Unlink AniList entry"; enabled: !!item.manualId; onTriggered: controller.unlinkAniListEntry(item.sourceId, item.path) }
            SourceMenuItem { text: item.isFavorite ? "Unfavorite" : "Favorite"; onTriggered: controller.setFavorite(item.sourceId, item.path, !item.isFavorite) }
            SourceMenuItem { text: item.isToWatch ? "Mark as Watched" : "Mark as To Watch"; onTriggered: controller.setToWatch(item.sourceId, item.path, !item.isToWatch) }
            SourceMenuItem { text: "Search AniList"; onTriggered: librarySearchDialog.searchFolder(item.sourceId, item.path) }
        }
        Connections {
            target: controller
            function onCoverReady(coverKey, localUrl) {
                if (!item.manualId && coverKey === item.coverKey)
                    coverImage.source = Qt.binding(function() {
                        return item.manualId ? item.coverUrl : (item.coverUrl || controller.coverUrlFor(item.coverKey))
                    })
            }
        }
    }

    // A single virtualized list of folder-grid rows followed by file rows.
    component FolderContentsGrid: ListView {
        id: mixed
        objectName: "folderContentsGrid"
        clip: true
        activeFocusOnTab: true
        // Virtualize rows without pooling cards whose image source may be updated
        // imperatively by coverReady; every newly visible card gets fresh bindings.
        reuseItems: false
        readonly property int columns: Math.max(1, Math.floor(width / mediaGrid.cellWidth))
        function makeRows(items, columns) {
            const folders = [], files = [], rows = []
            for (let i = 0; i < items.length; ++i)
                (items[i].isFolder ? folders : files).push(i)
            for (let i = 0; i < folders.length; i += columns)
                rows.push({folders: true, indices: folders.slice(i, i + columns), gap: false})
            for (let i = 0; i < files.length; ++i)
                rows.push({folders: false, indices: [files[i]], gap: i === 0 && folders.length > 0})
            return rows
        }
        model: visible ? makeRows(window.folderStructure, columns) : []
        onModelChanged: mixedCoverTimer.restart()
        function requestCovers() {
            if (!visible || !count) return
            const row = Math.max(0, Math.floor(contentY / mediaGrid.cellHeight))
            const last = Math.min(count - 1, row + Math.ceil(height / mediaGrid.cellHeight))
            if (row >= count || !model[row].folders) return
            let lastFolder = row
            for (let i = row; i <= last && model[i].folders; ++i) lastFolder = i
            const indices = model[lastFolder].indices
            controller.requestCoversForRange(model[row].indices[0], indices[indices.length - 1])
        }
        Timer { id: mixedCoverTimer; interval: 120; onTriggered: mixed.requestCovers() }
        function scheduleCovers() { mixedCoverTimer.restart() }
        onContentYChanged: mixedCoverTimer.restart()
        onHeightChanged: mixedCoverTimer.restart()
        onWidthChanged: mixedCoverTimer.restart()
        ScrollBar.vertical: ScrollBar {}
        Keys.onPressed: function(event) {
            const order = []
            let folderCount = 0
            for (const row of model) {
                if (row.folders) folderCount += row.indices.length
                for (const i of row.indices) order.push(i)
            }
            if (!order.length) return
            let pos = order.indexOf(window.selectedItemIndex)
            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                if (pos < 0) return
                const item = controller.items[order[pos]]
                controller.openCardItem(item.sourceId, item.path, item.isFolder)
            } else {
                const step = pos >= 0 && controller.items[order[pos]].isFolder ? columns : 1
                if (event.key === Qt.Key_Down) pos = pos >= 0 && pos < folderCount ? Math.min(folderCount, pos + step) : pos + 1
                else if (event.key === Qt.Key_Up) pos -= step
                else if (event.key === Qt.Key_Right) pos++
                else if (event.key === Qt.Key_Left) pos--
                else if (event.key === Qt.Key_Home) pos = 0
                else if (event.key === Qt.Key_End) pos = order.length - 1
                else return
                pos = Math.max(0, Math.min(order.length - 1, pos))
                window.selectedItemIndex = order[pos]
                for (let r = 0; r < model.length; ++r)
                    if (model[r].indices.indexOf(order[pos]) >= 0) { positionViewAtIndex(r, ListView.Contain); break }
            }
            event.accepted = true
        }
        delegate: Item {
            id: mixedRow
            required property var modelData
            width: mixed.width
            height: modelData.folders ? mediaGrid.cellHeight : (modelData.gap ? 74 : 58)
            Rectangle { visible: mixedRow.modelData.gap; width: parent.width; height: 1; color: window.line }
            Row {
                visible: mixedRow.modelData.folders
                Repeater {
                    model: mixedRow.modelData.folders ? mixedRow.modelData.indices : []
                    delegate: Item {
                        required property var modelData
                        width: mediaGrid.cellWidth; height: mediaGrid.cellHeight
                        LibraryCard {
                            modelData: controller.items[parent.modelData]
                            index: parent.modelData
                        }
                    }
                }
            }
            Rectangle {
                id: fileRow
                objectName: "folderFileRow"
                readonly property int originalIndex: mixedRow.modelData.indices[0]
                readonly property var fileItem: controller.items[originalIndex]
                anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
                anchors.margins: 3
                height: 52
                visible: !mixedRow.modelData.folders
                radius: 10
                color: originalIndex === window.selectedItemIndex ? window.accentSoft : (fileMouse.containsMouse ? window.surfaceHover : window.surface)
                border.color: originalIndex === window.selectedItemIndex ? window.accent : window.line
                RowLayout {
                    anchors.fill: parent; anchors.margins: 12; spacing: 12
                    Canvas {
                        Layout.preferredWidth: 24; Layout.preferredHeight: 28
                        readonly property bool video: /\.(mkv|mp4|avi|webm|mov|m4v|wmv|ts)$/i.test(parent.parent.fileItem?.name || "")
                        onVideoChanged: requestPaint()
                        onPaint: {
                            const c = getContext("2d"); c.clearRect(0, 0, width, height)
                            c.strokeStyle = "#a9c6ff"; c.fillStyle = "#a9c6ff"; c.lineWidth = 1.5
                            c.beginPath(); c.moveTo(3, 2); c.lineTo(15, 2); c.lineTo(21, 8); c.lineTo(21, 26); c.lineTo(3, 26); c.closePath(); c.stroke()
                            if (video) { c.beginPath(); c.moveTo(9, 11); c.lineTo(16, 16); c.lineTo(9, 21); c.closePath(); c.fill() }
                            else { for (let y = 13; y <= 21; y += 4) { c.beginPath(); c.moveTo(7, y); c.lineTo(17, y); c.stroke() } }
                        }
                    }
                    Text { Layout.fillWidth: true; text: parent.parent.fileItem?.name || ""; textFormat: Text.PlainText; color: window.primary; elide: Text.ElideRight; font.pixelSize: 13 }
                    Text { Layout.preferredWidth: 100; text: parent.parent.fileItem?.size || ""; color: window.muted; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight; font.pixelSize: 12 }
                }
                MouseArea {
                    id: fileMouse
                    anchors.fill: parent; hoverEnabled: true
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    onClicked: { window.selectedItemIndex = parent.originalIndex; mixed.forceActiveFocus() }
                    onDoubleClicked: controller.openCardItem(parent.fileItem.sourceId, parent.fileItem.path, false)
                }
                ToolTip {
                    id: fileTip
                    visible: fileMouse.containsMouse
                    delay: 700
                    width: Math.max(100, Math.min(600, mixed.width - 24))
                    text: fileRow.fileItem?.name || ""
                    contentItem: Text { text: fileTip.text; textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: window.primary }
                    background: Rectangle { color: window.surfaceHover; radius: 6; border.color: window.line }
                }
            }
        }
    }

    FolderDialog {
        id: addFolderDialog
        title: "Add library folder"
        // FolderDialog provides a URL value. Let Python convert it to the
        // platform-native filesystem path instead of invoking QUrl methods here.
        onAccepted: controller.addFolderSource(selectedFolder.toString())
    }

    Dialog {
        id: aniListLinkDialog
        property string sourceId: ""
        property string folderPath: ""
        title: "Link AniList entry"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: Overlay.overlay
        background: Rectangle { color: window.surface; radius: 14; border.color: window.line; border.width: 1 }
        contentItem: TextField {
            id: aniListUrlInput
            implicitWidth: 380
            placeholderText: "https://anilist.co/anime/12345/"
            selectByMouse: true
            color: window.primary
            background: Rectangle { radius: 8; color: "#0e111c"; border.color: window.line; border.width: 1 }
        }
        onAccepted: controller.linkAniListEntry(sourceId, folderPath, aniListUrlInput.text)
    }

    function openAniListLink(item) {
        aniListLinkDialog.sourceId = item.sourceId
        aniListLinkDialog.folderPath = item.path
        aniListUrlInput.text = item.manualId ? "https://anilist.co/anime/" + item.manualId + "/" : ""
        aniListLinkDialog.open()
    }

    Dialog {
        id: renameSourceDialog
        property string sourceId: ""
        title: "Rename library source"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: parent
        background: Rectangle { color: window.surface; radius: 14; border.color: window.line; border.width: 1 }
        contentItem: TextField {
            id: renameSourceInput
            selectByMouse: true
            color: window.primary
            font.pixelSize: 14
            background: Rectangle { radius: 8; color: "#0e111c"; border.color: window.line; border.width: 1 }
        }
        onAccepted: controller.renameSource(sourceId, renameSourceInput.text)
    }

    Dialog {
    id: removeSourceDialog
    property string sourceId: ""
    property string sourceLabel: ""
    title: "Remove from Library"
    modal: true
    standardButtons: Dialog.Ok | Dialog.Cancel
    anchors.centerIn: Overlay.overlay
    background: Rectangle { color: window.surface; radius: 14; border.color: window.line; border.width: 1 }
    
    contentItem: Item {
        implicitWidth: 320
        implicitHeight: textItem.implicitHeight
        
        Text {
            id: textItem
            width: 320
            text: "Remove \"" + removeSourceDialog.sourceLabel + "\" from the library? Your files and existing index records will be kept."
            color: window.muted
            wrapMode: Text.WordWrap
        }
    }

    onAccepted: controller.removeSource(sourceId)
}

    Dialog {
        id: excludeFolderDialog
        property string sourceId: ""
        property string relativePath: ""
        property string folderName: ""
        title: "Exclude from Indexer"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: parent
        background: Rectangle { color: window.surface; radius: 14; border.color: window.line; border.width: 1 }
        contentItem: Item {
            implicitWidth: 320
            implicitHeight: excludeFolderText.implicitHeight

            Text {
                id: excludeFolderText
                width: 320
                text: "Exclude \"" + excludeFolderDialog.folderName + "\" and everything inside it? Its files will not be changed."
                color: window.muted
                wrapMode: Text.WordWrap
            }
        }
        onAccepted: controller.excludeCardFolder(sourceId, relativePath)
    }

    Dialog {
        id: excludedFoldersDialog
        property var entries: []
        title: "Excluded folders"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Close
        background: Rectangle { color: window.surface; radius: 14; border.color: window.line; border.width: 1 }
        onOpened: {
            entries = controller.refreshExcludedFolders()
        }
        Connections {
            target: controller
            function onExclusionsChanged() {
                excludedFoldersDialog.entries = controller.excludedFolders
            }
        }
        contentItem: ListView {
            implicitWidth: 390
            implicitHeight: Math.min(400, Math.max(72, count * 63))
            clip: true
            spacing: 5
            model: excludedFoldersDialog.entries
            delegate: Rectangle {
                required property var modelData
                readonly property var exclusion: modelData
                width: ListView.view.width
                height: 58
                radius: 8
                color: "#0e111c"
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    spacing: 8
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2
                        Text { text: exclusion.name; color: window.primary; elide: Text.ElideRight; Layout.fillWidth: true; font.pixelSize: 12 }
                        Text { text: exclusion.sourceLabel + "  ·  " + exclusion.path; color: window.muted; elide: Text.ElideRight; Layout.fillWidth: true; font.pixelSize: 10 }
                    }
                    CapsuleButton { text: "Restore"; onClicked: controller.restoreExcludedFolder(exclusion.sourceId, exclusion.path) }
                }
            }
            Text {
                anchors.centerIn: parent
                visible: parent.count === 0
                text: "No folders are excluded."
                color: window.muted
            }
        }
    }

    function currentDrive() {
        for (let i = 0; i < controller.drives.length; ++i) {
            if (controller.drives[i].id === controller.selectedDrive)
                return controller.drives[i]
        }
        return null
    }

    function currentDriveTitle() {
        if (controller.currentView === "to_archive") return "To Archive"
        if (controller.currentView === "planned") return "Planned"
        if (controller.currentView === "favorites") return "Favorites"
        if (controller.currentView === "to_watch") return "To Watch"
        const drive = currentDrive()
        return drive ? drive.label : "Your anime library"
    }

    function currentDriveSubtitle() {
        if (controller.currentView === "to_archive") return "Compare saved episode indexes — no files are changed"
        if (controller.currentView === "planned") return "Anime to obtain in the future"
        if (controller.currentView === "favorites") return "Favorite anime across all library sources"
        if (controller.currentView === "to_watch") return "Unwatched anime across all library sources"
        const drive = currentDrive()
        if (!drive)
            return "Connect an external drive to begin"
        return (drive.kind === "folder" ? drive.sourcePath : drive.letter) + "  ·  " + drive.fileCount.toLocaleString() + " indexed items"
    }

    component CapsuleButton: Button {
        id: control
        hoverEnabled: true
        implicitHeight: 36
        leftPadding: 13
        rightPadding: 13
        font.pixelSize: 12
        font.weight: Font.DemiBold
        contentItem: Text {
            text: control.text
            color: control.enabled ? window.primary : "#596176"
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            font: control.font
        }
        background: Rectangle {
            radius: height / 2
            color: control.enabled && control.down ? "#28213d" : (control.enabled && control.hovered ? window.surfaceHover : window.surface)
            border.width: 1
            border.color: control.enabled && control.hovered ? "#555070" : window.line
        }
    }

    component FavoriteHeart: Button {
        id: heart
        property bool favorited: false
        width: 30
        height: 30
        focusPolicy: Qt.StrongFocus
        hoverEnabled: true
        text: favorited ? "♥" : "♡"
        Accessible.name: favorited ? "Remove from Favorites" : "Add to Favorites"
        Accessible.checkable: true
        Accessible.checked: favorited
        ToolTip.visible: hovered || activeFocus
        ToolTip.text: Accessible.name
        contentItem: Text {
            text: heart.text
            color: heart.favorited ? "#ed8ba9" : window.muted
            font.pixelSize: 23
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 8
            color: heart.hovered || heart.down ? window.surfaceHover : "transparent"
            border.width: heart.activeFocus ? 1 : 0
            border.color: window.accent
        }
        // Non-primary clicks on the heart must not reach the card underneath.
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.RightButton | Qt.MiddleButton }
    }

    component BookmarkIcon: Canvas {
        property bool marked: false
        implicitWidth: 24
        implicitHeight: 24
        onMarkedChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            const ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            const size = Math.min(width, height, 24)
            const x = (width - size) / 2
            const y = (height - size) / 2
            ctx.beginPath()
            ctx.moveTo(x + size * 0.12, y + size * 0.06)
            ctx.lineTo(x + size * 0.88, y + size * 0.06)
            ctx.lineTo(x + size * 0.88, y + size * 0.94)
            ctx.lineTo(x + size * 0.5, y + size * 0.7)
            ctx.lineTo(x + size * 0.12, y + size * 0.94)
            ctx.closePath()
            ctx.lineWidth = 1.6
            ctx.lineJoin = "round"
            ctx.strokeStyle = marked ? "#7ed9c3" : "#929bb0"
            if (marked) {
                ctx.fillStyle = "#7ed9c3"
                ctx.fill()
            }
            ctx.stroke()
        }
    }

    component WatchBookmark: FavoriteHeart {
        id: bookmark
        Accessible.name: favorited ? "Mark as Watched" : "Mark as To Watch"
        contentItem: Item {
            // Use the full button area, not the style's padded content area.
            BookmarkIcon {
                anchors.centerIn: parent
                width: bookmark.width
                height: bookmark.height
                marked: bookmark.favorited
            }
        }
    }

    component SourceMenuItem: MenuItem {
        id: sourceMenuItem
        implicitHeight: 34
        leftPadding: 12
        rightPadding: 12
        contentItem: Text {
            text: sourceMenuItem.text
            elide: Text.ElideRight
            color: sourceMenuItem.enabled ? window.primary : "#596176"
            verticalAlignment: Text.AlignVCenter
            font.pixelSize: 12
        }
        background: Rectangle {
            color: sourceMenuItem.enabled && sourceMenuItem.highlighted ? window.surfaceHover : "transparent"
            radius: 6
        }
    }

    header: Rectangle {
        height: window.compactLayout ? 60 : 76
        color: window.sidebar

        Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: window.line }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 28
            anchors.rightMargin: 28
            spacing: 16

            Rectangle {
                Layout.preferredWidth: 40
                Layout.preferredHeight: 40
                radius: 13
                gradient: Gradient {
                    GradientStop { position: 0.0; color: "#b399ff" }
                    GradientStop { position: 1.0; color: "#7161d9" }
                }
                Text { anchors.centerIn: parent; text: "✦"; color: "#ffffff"; font.pixelSize: 20 }
            }

            ColumnLayout {
                spacing: 1
                Text { text: "JAnimeVault"; color: window.primary; font.pixelSize: 18; font.weight: Font.DemiBold }
                Text { text: "Personal anime library"; color: window.muted; font.pixelSize: 12 }
            }

            Item { Layout.fillWidth: true }

            CapsuleButton {
                text: "↻  Refresh"
                ToolTip.visible: hovered
                ToolTip.text: "Refresh connected drives"
                onClicked: controller.refreshLibrary()
            }
        }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.preferredWidth: 258
            Layout.fillHeight: true
            color: window.sidebar

            Rectangle { anchors.top: parent.top; anchors.bottom: parent.bottom; anchors.right: parent.right; width: 1; color: window.line }

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 20
                spacing: 15

                RowLayout {
                    Layout.fillWidth: true
                    Button {
                        id: libraryNavigation
                        objectName: "libraryNavigation"
                        text: "LIBRARY"
                        padding: 0
                        implicitWidth: contentItem.implicitWidth
                        implicitHeight: 24
                        hoverEnabled: true
                        focusPolicy: Qt.StrongFocus
                        Accessible.name: "Library"
                        ToolTip.visible: hovered
                        ToolTip.text: "Return to Library"
                        contentItem: Text {
                            text: libraryNavigation.text
                            color: libraryNavigation.hovered || libraryNavigation.activeFocus ? window.primary : window.muted
                            verticalAlignment: Text.AlignVCenter
                            font.pixelSize: 11
                            font.weight: Font.DemiBold
                            font.letterSpacing: 1.5
                        }
                        background: Rectangle {
                            color: libraryNavigation.hovered ? window.surfaceHover : "transparent"
                            radius: 3
                            border.color: libraryNavigation.activeFocus ? window.accent : "transparent"
                        }
                        onClicked: controller.selectLibrary()
                    }
                    Item { Layout.fillWidth: true }
                    CapsuleButton {
                        text: "+ Folder"
                        onClicked: addFolderDialog.open()
                    }
                    CapsuleButton {
                        text: "Excluded"
                        onClicked: excludedFoldersDialog.open()
                    }
                }

                CapsuleButton {
                    id: favoritesNavigation
                    text: "♥  Favorites"
                    contentItem: Row {
                        spacing: 8
                        Text { text: controller.currentView === "favorites" ? "♥" : "♡"; color: controller.currentView === "favorites" ? "#ed8ba9" : window.muted; font.pixelSize: 23; width: 20; height: 20; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                        Text { text: "Favorites"; color: window.primary; font.pixelSize: 12; height: 20; verticalAlignment: Text.AlignVCenter }
                    }
                    Layout.fillWidth: true
                    highlighted: controller.currentView === "favorites"
                    background: Rectangle {
                        radius: height / 2
                        color: favoritesNavigation.highlighted ? window.accentSoft : (favoritesNavigation.enabled && favoritesNavigation.hovered ? window.surfaceHover : window.surface)
                        border.color: favoritesNavigation.enabled && favoritesNavigation.hovered ? "#555070" : window.line
                        border.width: 1
                    }
                    onClicked: controller.selectFavorites()
                }
                CapsuleButton {
                    id: toWatchNavigation
                    text: "To Watch"
                    contentItem: Row {
                        spacing: 8
                        BookmarkIcon { width: 20; height: 20; marked: controller.currentView === "to_watch" }
                        Text { text: "To Watch"; color: window.primary; font.pixelSize: 12; height: 20; verticalAlignment: Text.AlignVCenter }
                    }
                    Layout.fillWidth: true
                    highlighted: controller.currentView === "to_watch"
                    background: Rectangle {
                        radius: height / 2
                        color: toWatchNavigation.highlighted ? window.accentSoft : (toWatchNavigation.enabled && toWatchNavigation.hovered ? window.surfaceHover : window.surface)
                        border.color: toWatchNavigation.enabled && toWatchNavigation.hovered ? "#555070" : window.line
                        border.width: 1
                    }
                    onClicked: controller.selectToWatch()
                }
                CapsuleButton {
                    id: plannedNavigation
                    text: "Planned"
                    Layout.fillWidth: true
                    highlighted: controller.currentView === "planned"
                    background: Rectangle {
                        radius: height / 2
                        color: plannedNavigation.highlighted ? window.accentSoft : (plannedNavigation.enabled && plannedNavigation.hovered ? window.surfaceHover : window.surface)
                        border.color: plannedNavigation.enabled && plannedNavigation.hovered ? "#555070" : window.line
                        border.width: 1
                    }
                    onClicked: controller.selectPlanned()
                }

                CapsuleButton {
                    id: archiveNavigation
                    objectName: "archiveNavigation"
                    text: "To Archive"
                    Layout.fillWidth: true
                    highlighted: controller.currentView === "to_archive"
                    background: Rectangle {
                        radius: height / 2
                        color: archiveNavigation.highlighted ? window.accentSoft : (archiveNavigation.hovered ? window.surfaceHover : window.surface)
                        border.color: archiveNavigation.hovered ? "#555070" : window.line
                    }
                    onClicked: controller.selectToArchive()
                }

                ListView {
                    id: driveList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 8
                    model: controller.drives

                    delegate: Rectangle {
                        required property var modelData
                        readonly property var drive: modelData
                        width: driveList.width
                        height: 83
                        radius: 14
                        color: controller.currentView === "source" && controller.selectedDrive === drive.id ? window.accentSoft : (driveMouse.containsMouse ? window.surface : "transparent")
                        border.width: controller.currentView === "source" && controller.selectedDrive === drive.id ? 1 : 0
                        border.color: "#5e4c9b"

                        Behavior on color { ColorAnimation { duration: 130 } }

                        MouseArea {
                            id: driveMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            onClicked: function(mouse) {
                                controller.selectDrive(drive.id)
                                if (mouse.button === Qt.RightButton)
                                    sourceMenu.popup()
                            }
                        }

                        Menu {
                            id: sourceMenu
                            width: 196
                            background: Rectangle { color: window.surface; radius: 10; border.color: window.line; border.width: 1 }
                            SourceMenuItem {
                                text: "Open in File Explorer"
                                enabled: drive.connected
                                onTriggered: controller.openSourceInExplorer(drive.id)
                            }
                            SourceMenuItem {
                                text: "Re-index"
                                enabled: drive.connected && !drive.indexing
                                onTriggered: {
                                    controller.selectDrive(drive.id)
                                    controller.reindexSelected()
                                }
                            }
                            SourceMenuItem {
                                text: "Rename"
                                onTriggered: {
                                    renameSourceDialog.sourceId = drive.id
                                    renameSourceInput.text = drive.label
                                    renameSourceDialog.open()
                                }
                            }
                            MenuSeparator { contentItem: Rectangle { implicitHeight: 1; color: window.line } }
                            SourceMenuItem {
                                text: "Remove from Library"
                                onTriggered: {
                                    removeSourceDialog.sourceId = drive.id
                                    removeSourceDialog.sourceLabel = drive.label
                                    removeSourceDialog.open()
                                }
                            }
                            SourceMenuItem {
                                objectName: "archiveSourceAction"
                                text: drive.id === (controller.archiveSource || "") ? "Clear To Archive source" : "Use as To Archive source"
                                visible: drive.kind === "folder"
                                height: visible ? implicitHeight : 0
                                enabled: drive.kind === "folder" && drive.lastIndexed !== "Not indexed yet" && !drive.indexing
                                onTriggered: controller.designateArchiveSource(drive.id === controller.archiveSource ? "" : drive.id)
                            }
                        }

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 13
                            spacing: 10
                            Rectangle {
                                Layout.preferredWidth: 35
                                Layout.preferredHeight: 35
                                radius: 11
                                color: controller.selectedDrive === drive.id ? "#4a3b78" : window.surfaceHover
                                Text { anchors.centerIn: parent; text: drive.kind === "folder" ? "▱" : "▰"; color: window.accent; font.pixelSize: 16 }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                Text {
                                    text: drive.label + (drive.id === (controller.archiveSource || "") ? "  · To Archive" : "")
                                    color: window.primary
                                    font.pixelSize: 13
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: (drive.kind === "folder" ? drive.sourcePath : drive.letter) + "  ·  " + drive.fileCount.toLocaleString() + " items"
                                    color: window.muted
                                    font.pixelSize: 11
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                                Text {
                                    text: drive.indexing ? "● Indexing" : (drive.connected ? (drive.kind === "folder" ? "● Available" : "● Connected") : (drive.kind === "folder" ? "○ Unavailable" : "○ Disconnected"))
                                    color: drive.indexing ? window.accent : (drive.connected ? window.green : window.muted)
                                    font.pixelSize: 10
                                }
                            }
                        }
                    }

                    Item {
                        anchors.centerIn: parent
                        width: parent.width - 18
                        height: emptyDrivesText.implicitHeight
                        visible: driveList.count === 0
                        Text {
                            id: emptyDrivesText
                            anchors.centerIn: parent
                            width: parent.width
                            text: "Add a folder or connect an external drive to start building your library."
                            color: window.muted
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            lineHeight: 1.35
                        }
                    }
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: window.page

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: window.compactLayout ? 16 : 32
                spacing: window.compactLayout ? 9 : 19

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text { text: window.currentDriveTitle(); color: window.primary; font.pixelSize: 28; font.weight: Font.DemiBold }
                        Text { text: window.currentDriveSubtitle(); color: window.muted; font.pixelSize: 13 }
                    }
                    CapsuleButton {
                        text: "↻  Re-index"
                        enabled: controller.currentView === "source" && controller.selectedDrive.length > 0 && !controller.isIndexing
                        onClicked: controller.reindexSelected()
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 11

                    CapsuleButton {
                        text: "←"
                        enabled: controller.canNavigateUp
                        onClicked: controller.navigateUp()
                    }

                    Row {
                        visible: controller.currentView === "source"
                        Layout.fillWidth: true
                        spacing: 2
                        Repeater {
                            model: controller.breadcrumbs
                            delegate: Row {
                                required property var modelData
                                required property int index
                                CapsuleButton {
                                    text: modelData.label
                                    enabled: index < controller.breadcrumbs.length - 1
                                    onClicked: controller.navigateTo(modelData.path)
                                }
                                Text {
                                    text: index < controller.breadcrumbs.length - 1 ? "›" : ""
                                    color: window.muted
                                    anchors.verticalCenter: parent.verticalCenter
                                    font.pixelSize: 18
                                }
                            }
                        }
                    }

                    Rectangle {
                        Layout.preferredWidth: 76
                        Layout.preferredHeight: 36
                        radius: 18
                        color: window.surface
                        border.color: window.line
                        border.width: 1
                        Row {
                            anchors.centerIn: parent
                            spacing: 2
                            Rectangle {
                                width: 34; height: 28; radius: 14
                                color: window.gridView ? window.accentSoft : "transparent"
                                Text { anchors.centerIn: parent; text: "▦"; color: window.gridView ? window.accent : window.muted; font.pixelSize: 16 }
                                MouseArea { anchors.fill: parent; onClicked: window.gridView = true }
                            }
                            Rectangle {
                                width: 34; height: 28; radius: 14
                                color: !window.gridView ? window.accentSoft : "transparent"
                                Text { anchors.centerIn: parent; text: "☷"; color: !window.gridView ? window.accent : window.muted; font.pixelSize: 16 }
                                MouseArea { anchors.fill: parent; onClicked: window.gridView = false }
                            }
                        }
                    }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 48
                    radius: 14
                    color: window.surface
                    border.color: searchInput.activeFocus ? "#6753a5" : window.line
                    border.width: 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 15
                        anchors.rightMargin: 9
                        spacing: 10
                        Text { text: "⌕"; color: window.muted; font.pixelSize: 24 }
                        TextField {
                            id: searchInput
                            Layout.fillWidth: true
                            placeholderText: "Search your anime..."
                            placeholderTextColor: "#6f7890"
                            color: window.primary
                            font.pixelSize: 14
                            selectByMouse: true
                            onAccepted: controller.search(text)
                            text: controller.searchTerm
                            background: Item {}
                        }
                        CapsuleButton { text: "Search"; onClicked: controller.search(searchInput.text) }
                        CapsuleButton {
                            text: "Clear"
                            visible: controller.isSearching
                            onClicked: { searchInput.clear(); controller.clearSearch() }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    visible: controller.currentView !== "source" || controller.selectedDrive.length > 0
                    Text {
                        text: controller.isSearching ? "Search results" : (controller.currentView !== "source" ? window.currentDriveTitle() : (controller.currentPath.length > 0 ? "Folder contents" : "Anime library"))
                        color: window.primary
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                    }
                    Item { Layout.fillWidth: true }
                    Text { visible: controller.currentView !== "to_archive"; text: (controller.currentView === "planned" ? controller.plannedEntries.length : controller.items.length).toLocaleString() + " shown"; color: window.muted; font.pixelSize: 12 }
                }

                Rectangle {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: 18
                    color: "#0e111c"
                    border.color: window.line
                    border.width: 1
                    clip: true

                    Loader {
                        anchors.fill: parent
                        anchors.margins: window.contentInset
                        active: controller.currentView === "to_archive"
                        sourceComponent: Component { ArchiveView { backend: controller } }
                    }

                    PlannedView {
                        anchors.fill: parent
                        anchors.margins: window.contentInset
                        visible: controller.currentView === "planned"
                        backend: controller
                        gridMode: window.gridView
                    }

                    GridView {
                        id: mediaGrid
                        anchors.fill: parent
                        anchors.margins: window.contentInset
                        visible: !window.mixedFolderGrid && controller.currentView !== "planned" && controller.currentView !== "to_archive" && window.gridView && (controller.currentView !== "source" || controller.selectedDrive.length > 0)
                        clip: true
                        model: libraryPresentation
                        cellWidth: Math.max(158, Math.floor((width - 10) / Math.max(1, Math.floor(width / 176))))
                        cellHeight: cellWidth * 1.48
                        currentIndex: window.selectedItemIndex

                        function requestViewportCovers() {
                            if (!visible || count === 0 || cellWidth <= 0 || cellHeight <= 0)
                                return
                            const columns = Math.max(1, Math.floor(width / cellWidth))
                            const firstRow = Math.max(0, Math.floor(contentY / cellHeight))
                            const visibleRows = Math.max(1, Math.ceil(height / cellHeight))
                            const first = firstRow * columns
                            const last = Math.min(count - 1, (firstRow + visibleRows) * columns - 1)
                            controller.requestCoversForRange(first, last)
                        }

                        Timer {
                            id: viewportCoverTimer
                            interval: 120
                            repeat: false
                            onTriggered: mediaGrid.requestViewportCovers()
                        }

                        onContentYChanged: viewportCoverTimer.restart()
                        onWidthChanged: viewportCoverTimer.restart()
                        onHeightChanged: viewportCoverTimer.restart()
                        onCountChanged: viewportCoverTimer.restart()
                        Component.onCompleted: viewportCoverTimer.restart()

                        delegate: LibraryCard { required property var entry; modelData: entry }
                    }

                    FolderContentsGrid {
                        id: folderContentsGrid
                        anchors.fill: parent
                        anchors.margins: window.contentInset
                        visible: window.mixedFolderGrid
                    }

                    ListView {
                        id: mediaList
                        anchors.fill: parent
                        anchors.margins: 12
                        visible: controller.currentView !== "planned" && controller.currentView !== "to_archive" && !window.gridView && (controller.currentView !== "source" || controller.selectedDrive.length > 0)
                        clip: true
                        spacing: 3
                        model: libraryPresentation
                        currentIndex: window.selectedItemIndex

                        delegate: Rectangle {
                            required property var entry
                            required property int index
                            readonly property var item: entry
                            width: mediaList.width
                            height: 58
                            radius: 10
                            color: index === window.selectedItemIndex ? window.accentSoft : (listMouse.containsMouse ? window.surfaceHover : "transparent")
                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 15
                                anchors.rightMargin: item.isFolder ? 87 : 15
                                spacing: 12
                                Rectangle {
                                    Layout.preferredWidth: 34; Layout.preferredHeight: 34; radius: 10
                                    color: item.isFolder ? "#33294f" : "#1e2a42"
                                    Text { anchors.centerIn: parent; text: item.isFolder ? "▰" : "◌"; color: item.isFolder ? "#d5c8ff" : "#a9c6ff" }
                                }
                                Text { text: item.name; color: window.primary; Layout.fillWidth: true; elide: Text.ElideRight; font.pixelSize: 13 }
                                Text { text: controller.currentView !== "source" ? item.sourceLabel + " · " + item.sourceStatus : (item.isFolder ? "Folder" : item.size); color: window.muted; Layout.preferredWidth: controller.currentView !== "source" ? 220 : 100; elide: Text.ElideRight; horizontalAlignment: Text.AlignRight; font.pixelSize: 12 }
                            }
                            MouseArea {
                                id: listMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton | Qt.RightButton
                                onClicked: function(mouse) {
                                    window.selectedItemIndex = index
                                    if (mouse.button === Qt.RightButton && item.isFolder)
                                        listFolderMenu.popup()
                                }
                                onDoubleClicked: controller.openCardItem(item.sourceId, item.path, item.isFolder)
                            }
                            FavoriteHeart {
                                visible: item.isFolder
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.rightMargin: 12
                                z: 1
                                favorited: !!item.isFavorite
                                onClicked: controller.setFavorite(item.sourceId, item.path, !item.isFavorite)
                            }
                            WatchBookmark {
                                visible: item.isFolder
                                anchors.right: parent.right
                                anchors.verticalCenter: parent.verticalCenter
                                anchors.rightMargin: 46
                                z: 1
                                favorited: !!item.isToWatch
                                onClicked: controller.setToWatch(item.sourceId, item.path, !item.isToWatch)
                            }
                            Menu {
                                id: listFolderMenu
                                width: 188
                                background: Rectangle { color: window.surface; radius: 10; border.color: window.line; border.width: 1 }
                                SourceMenuItem { text: "Open folder"; onTriggered: controller.openCardInExplorer(item.sourceId, item.path) }
                                SourceMenuItem { text: "Copy name"; onTriggered: controller.copyFolderName(item.name) }
                                SourceMenuItem { text: "Search on the web"; onTriggered: controller.searchFolderOnWeb(item.name, item.sourceId, item.path) }
                                MenuSeparator { contentItem: Rectangle { implicitHeight: 1; color: window.line } }
                                SourceMenuItem {
                                    text: "Exclude from indexer"
                                    onTriggered: {
                                        excludeFolderDialog.relativePath = item.path
                                        excludeFolderDialog.sourceId = item.sourceId
                                        excludeFolderDialog.folderName = item.name
                                        excludeFolderDialog.open()
                                    }
                                }
                                SourceMenuItem { text: "Link AniList entry..."; onTriggered: window.openAniListLink(item) }
                                SourceMenuItem { text: "Unlink AniList entry"; enabled: !!item.manualId; onTriggered: controller.unlinkAniListEntry(item.sourceId, item.path) }
                                SourceMenuItem { text: item.isFavorite ? "Unfavorite" : "Favorite"; onTriggered: controller.setFavorite(item.sourceId, item.path, !item.isFavorite) }
                                SourceMenuItem { text: item.isToWatch ? "Mark as Watched" : "Mark as To Watch"; onTriggered: controller.setToWatch(item.sourceId, item.path, !item.isToWatch) }
                                SourceMenuItem { text: "Search AniList"; onTriggered: librarySearchDialog.searchFolder(item.sourceId, item.path) }
                            }
                        }
                    }

                    Column {
                        anchors.centerIn: parent
                        spacing: 10
                        width: Math.min(350, parent.width - 50)
                        visible: controller.currentView === "source" && controller.selectedDrive.length === 0
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "✦"; color: window.accent; font.pixelSize: 35 }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: "Your library is waiting"; color: window.primary; font.pixelSize: 18; font.weight: Font.DemiBold }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; width: parent.width; text: "Add a folder or select an indexed external drive to begin browsing."; color: window.muted; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; lineHeight: 1.35 }
                    }

                    Column {
                        anchors.centerIn: parent
                        spacing: 9
                        width: Math.min(350, parent.width - 50)
                        visible: controller.currentView !== "planned" && controller.currentView !== "to_archive" && (controller.currentView !== "source" || controller.selectedDrive.length > 0) && controller.items.length === 0
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: controller.isSearching ? "⌕" : "▱"; color: window.accent; font.pixelSize: 30 }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; text: controller.isSearching ? "No files found" : (controller.currentView === "to_watch" ? "Nothing to watch yet" : (controller.currentView === "favorites" ? "No favorites yet" : "Nothing here yet")); color: window.primary; font.pixelSize: 17; font.weight: Font.DemiBold }
                        Text { anchors.horizontalCenter: parent.horizontalCenter; width: parent.width; text: controller.isSearching ? "No anime or files match your search." : (controller.currentView === "to_watch" ? "Mark an anime folder as To Watch to see it here." : (controller.currentView === "favorites" ? "Mark an anime folder as Favorite to see it here." : "This folder has no indexed items.")); color: window.muted; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter }
                    }
                }

                Rectangle {
                    id: libraryStatusPanel
                    objectName: "libraryStatusPanel"
                    readonly property bool isNotice: controller.notificationMessage.length > 0
                    readonly property string level: controller.statusMode === "persistent" && controller.statusLevel === "error" ? "error" : (isNotice ? controller.notificationLevel : controller.statusLevel)
                    readonly property string message: isNotice ? controller.notificationMessage : controller.status
                    readonly property bool showOperationalStatus: isNotice && controller.status !== message && (controller.statusMode === "operation" || controller.statusMode === "persistent")
                    readonly property bool idleReady: !isNotice && controller.statusMode === "idle" && controller.status === "Ready" && !controller.isIndexing && !controller.plannedBusy
                    readonly property color semanticColor: level === "error" ? "#f0a0ad" : (level === "warning" ? "#edc779" : (idleReady || level === "success" ? window.green : window.muted))
                    Layout.fillWidth: true
                    Layout.preferredHeight: 64
                    radius: 13
                    color: controller.isIndexing ? "#1b1930" : window.surface
                    border.color: level === "error" || level === "warning" || level === "success" ? semanticColor : (controller.isIndexing ? "#42376a" : (idleReady ? "#315d4c" : window.line))
                    border.width: 1

                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 14
                        anchors.rightMargin: 14
                        spacing: 11
                        BusyIndicator {
                            visible: controller.isIndexing || controller.statusMode === "operation"
                            running: visible
                            Layout.preferredWidth: 26
                            Layout.preferredHeight: 26
                        }
                        Rectangle {
                            id: libraryStatusIndicator
                            visible: !controller.isIndexing && controller.statusMode !== "operation"
                            Layout.preferredWidth: 9
                            Layout.preferredHeight: 9
                            radius: 5
                            color: libraryStatusPanel.semanticColor
                        }
                        ColumnLayout {
                            id: libraryStatusColumn
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            spacing: 2
                            Text {
                                objectName: "libraryStatusHeading"
                                text: libraryStatusPanel.showOperationalStatus ? controller.status : (controller.isIndexing ? "Indexing library" : "Library status")
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                elide: Text.ElideRight
                                textFormat: Text.PlainText
                                color: controller.statusMode === "persistent" && controller.statusLevel === "error" ? "#f0a0ad" : window.primary
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                            }
                            Text {
                                objectName: "libraryStatusMessage"
                                text: libraryStatusPanel.message
                                Accessible.name: text
                                textFormat: Text.PlainText
                                color: window.primary
                                font.pixelSize: 12
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                            }
                        }
                        CapsuleButton {
                            id: statusPlanned
                            objectName: "statusPlanned"
                            text: "Planned"
                            visible: libraryStatusPanel.isNotice && controller.notificationTarget === "planned" && controller.currentView !== "planned"
                            onClicked: controller.selectPlanned()
                        }
                        CapsuleButton {
                            id: feedbackDismiss
                            objectName: "statusDismiss"
                            text: "Dismiss"
                            Accessible.name: "Dismiss notification"
                            visible: libraryStatusPanel.isNotice
                            property int pressedRevision: -1
                            onPressed: pressedRevision = controller.notificationRevision
                            onClicked: controller.dismissNotification(pressedRevision)
                        }
                    }
                    ToolTip.visible: statusHover.hovered
                    ToolTip.text: (libraryStatusPanel.showOperationalStatus ? controller.status + "\n" : "") + libraryStatusPanel.message
                    HoverHandler { id: statusHover }
                }
            }
        }
    }
}
