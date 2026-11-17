import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: archive
    objectName: "archiveView"
    required property var backend
    readonly property var state: backend.archiveState
    ListModel { id: presentation; dynamicRoles: true }
    property var shown: []
    property string context: ""
    property var expandedKeys: ({})
    property var details: ({})
    property string selectedKey: ""
    function key(row) { return JSON.stringify([row.temporary.source_id, row.temporary.path.toLowerCase()]) }
    function toggle(row) {
        const copy = Object.assign({}, expandedKeys), id = key(row)
        copy[id] = !copy[id]; expandedKeys = copy; selectedKey = id
        if (copy[id]) backend.loadArchiveDetails(row.temporary.path)
    }
    function updatePresentation() {
        const detail = backend.archiveDetail
        if (detail.path && detail.sourceId && !detail.busy && detail.text) {
            const cache = Object.assign({}, details)
            cache[JSON.stringify([detail.sourceId, detail.path.toLowerCase()])] = detail.text
            details = cache
        }
        if (state.busy || state.error) return
        const wantedRows = state.items || []
        const newContext = JSON.stringify([state.context || "", state.offset || 0])
        const reset = newContext !== context
        if (!reset && JSON.stringify(wantedRows) === JSON.stringify(shown)) return
        rows.forceLayout()
        let index = rows.indexAt(1, rows.contentY + 1)
        const item = index >= 0 ? rows.itemAtIndex(index) : null
        const anchor = !reset && item && shown[index] ? {id: key(shown[index]), next: shown[index+1] ? key(shown[index+1]) : "", index: index, offset: item.y-rows.contentY} : null
        const previous = new Map(shown.map(row => [key(row),JSON.stringify(row)]))
        const reload = wantedRows.filter(row => expandedKeys[key(row)] && previous.has(key(row)) && previous.get(key(row)) !== JSON.stringify(row))
        const keys = reset ? [] : shown.map(key), wanted = wantedRows.map(key), keep = new Set(wanted)
        if (reset) presentation.clear()
        for (let i = keys.length-1; i >= 0; --i) {
            if (!keep.has(keys[i])) { presentation.remove(i); keys.splice(i,1) }
        }
        for (let i = 0; i < wanted.length; ++i) {
            if (keys[i] !== wanted[i]) {
                const found = keys.indexOf(wanted[i], i+1)
                if (found >= 0) { presentation.move(found,i,1); keys.splice(i,0,keys.splice(found,1)[0]) }
                else { presentation.insert(i,{entryData:wantedRows[i]}); keys.splice(i,0,wanted[i]) }
            }
            presentation.setProperty(i,'entryData',wantedRows[i])
        }
        shown = wantedRows; context = newContext
        rows.forceLayout()
        if (reset) rows.positionViewAtBeginning()
        else if (anchor && wanted.length) {
            let target = wanted.indexOf(anchor.id)
            if (target < 0) target = wanted.indexOf(anchor.next)
            if (target < 0) target = Math.min(anchor.index,wanted.length-1)
            rows.positionViewAtIndex(target,ListView.Beginning); rows.forceLayout()
            const current = rows.itemAtIndex(target)
            if (current) rows.contentY = Math.max(rows.originY, Math.min(current.y-anchor.offset, rows.originY+Math.max(0,rows.contentHeight-rows.height)))
        }
        for (const row of reload) if (row.temporary.source_id === backend.archiveSource) backend.loadArchiveDetails(row.temporary.path)
    }
    function warnings(row) {
        const text = []
        if (row.state === 'invalid_destination') text.push('Destination missing from the saved index; assign or repair it.')
        if (row.destination && row.destination.connected === false) text.push('Destination disconnected: only its saved index is available.')
        if (row.overlap_unverified) text.push('Physical path overlap cannot be verified; check the locations manually.')
        if (row.state === 'overlap_review') text.push('Known overlapping roots cannot be used for comparison.')
        if (row.counts && row.counts.unknown) text.push(row.counts.unknown + ' unknown/ambiguous filenames; expand details.')
        if (row.nested_content_review && (row.nested_content_review.temporary || row.nested_content_review.destination)) text.push('Nested content is not compared.')
        if (row.truncated || row.files_truncated) text.push('Truncated result: review required; counts may be unavailable.')
        return text.join('\n')
    }
    function wheelScroll(inner, event) {
        const delta = event.pixelDelta.y || event.angleDelta.y / 120 * 48
        if (!delta) { event.accepted = false; return }
        const bottom = inner.originY + Math.max(0,inner.contentHeight-inner.height)
        const canScroll = delta > 0 ? inner.contentY > inner.originY+0.5 : inner.contentY < bottom-0.5
        const target = canScroll ? inner : rows
        target.cancelFlick()
        target.contentY = Math.max(target.originY,Math.min(target.contentY-delta,target.originY+Math.max(0,target.contentHeight-target.height)))
        // One recipient per event. Boundary events are delegated to the outer page.
        event.accepted = true
    }
    Component.onCompleted: updatePresentation()
    Connections {
        target: archive.backend
        function onArchiveChanged() {
            if (picker.visible && picker.temporarySource !== archive.backend.archiveSource) picker.close()
            archive.updatePresentation()
        }
    }
    function statusLabel(row) {
        if (row.comparison_state === "incomplete_review") return "Incomplete indexed result — review required"
        if (row.comparison_state === "season_conflict_review") return "Conflicting folder seasons — review required"
        const labels = {confirmed: "Confirmed destination", suggested: "Suggested — confirmation required",
            multiple_candidates: "Multiple destinations — choose one", no_match: "No destination assigned",
            invalid_destination: "Previously confirmed destination is missing", overlap_review: "Overlapping sources — review required"}
        return labels[row.state] || row.state
    }
    function destinationText(dest) {
        if (!dest) return "No destination assigned"
        return dest.label + " · " + dest.path + "\n" +
            (dest.connected === true ? "Available" : (dest.connected === false ? "Disconnected / unavailable" : "Availability unknown")) +
            " · Indexed: " + (dest.last_indexed || "Unknown")
    }
    function chooseDestination(row) {
        picker.folderPath = row.temporary.path
        picker.temporarySource = row.temporary.source_id
        destinationInput.text = row.temporary.name
        backend.searchArchiveDestinations(picker.folderPath, destinationInput.text, picker.temporarySource)
        picker.open()
    }
    component Action: Button {
        id: control
        implicitHeight: 34
        padding: 9
        hoverEnabled: true
        contentItem: Text { text: control.text; textFormat: Text.PlainText; color: control.enabled ? "#f5f6fb" : "#596176"; font.pixelSize: 12; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { radius: 10; color: control.enabled && control.hovered ? "#20263a" : "#161a29"; border.color: control.activeFocus ? "#aa8cff" : "#262c3e" }
        Accessible.name: text
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: archive.state.summary || (archive.state.busy ? "Loading saved indexes…" : "To Archive"); color: "#f5f6fb"; textFormat: Text.PlainText; wrapMode: Text.Wrap }
            Action { text: "Refresh"; enabled: !archive.state.busy; onClicked: backend.refreshArchive() }
            Action {
                objectName: "archiveReindex"
                text: backend.archiveSourceStatus.indexing ? "Indexing…" : "Re-index source"
                enabled: !!backend.archiveSource && backend.archiveSourceStatus.connected === true && !backend.archiveSourceStatus.indexing
                onClicked: backend.reindexArchiveSource()
                ToolTip.visible: hovered && !enabled
                ToolTip.text: "Connect the designated source and wait for any current scan to finish."
            }
            Action { text: "Clear source"; enabled: !!backend.archiveSource && !archive.state.busy; onClicked: backend.designateArchiveSource("") }
        }
        Text {
            Layout.fillWidth: true
            text: "Read-only tracking from saved indexes; connected does not mean synchronized. No media files are changed."
            color: "#929bb0"; font.pixelSize: 11; wrapMode: Text.Wrap
        }
        Text {
            Layout.fillWidth: true; visible: !!archive.state.source
            text: archive.state.source ? "Source: " + archive.state.source.label + " · Indexed: " + (archive.state.source.last_indexed || "Unknown") : ""
            color: "#929bb0"; textFormat: Text.PlainText; wrapMode: Text.Wrap
        }
        Text { Layout.fillWidth: true; visible: !!archive.state.error; text: archive.state.error || ""; color: "#e6bb68"; textFormat: Text.PlainText; wrapMode: Text.Wrap }
        Text {
            Layout.fillWidth: true
            visible: !archive.state.busy && !archive.shown.length
            objectName: "archiveEmptyState"
            text: !backend.archiveSource ? "No To Archive source. Right-click an already-indexed manual folder in the sidebar and choose ‘Use as To Archive source’." : "No immediate anime folders match this page/search. Use Refresh to return to the first page."
            color: "#929bb0"; wrapMode: Text.Wrap
        }
        ListView {
            id: rows
            objectName: "archiveRows"
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; spacing: 10
            model: presentation
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                id: card
                required property var entryData
                readonly property var row: entryData
                readonly property bool expanded: !!archive.expandedKeys[archive.key(row)]
                width: rows.width
                height: contents.implicitHeight + 24
                radius: 12; color: "#161a29"; border.color: archive.selectedKey === archive.key(row) ? "#555070" : "#262c3e"
                TapHandler { acceptedButtons: Qt.RightButton; onTapped: copyMenu.popup() }
                Menu {
                    id: copyMenu
                    objectName: "archiveCopyMenu"
                    background: Rectangle { color: "#161a29"; border.color: "#262c3e"; radius: 10 }
                    MenuItem {
                        text: "Copy name"
                        contentItem: Text { text: "Copy name"; color: "#f5f6fb" }
                        background: Rectangle { color: parent.highlighted ? "#20263a" : "transparent" }
                        onTriggered: backend.copyFolderName(card.row.temporary.name)
                    }
                }
                ColumnLayout {
                    id: contents
                    anchors { left: parent.left; right: parent.right; top: parent.top; margins: 12 }
                    spacing: 5
                    RowLayout {
                        Layout.fillWidth: true; spacing: 12
                        Rectangle {
                            Layout.preferredWidth: 56; Layout.preferredHeight: 78; color: "#20263a"; radius: 5
                            Text { anchors.centerIn: parent; text: "▱"; color: "#929bb0"; font.pixelSize: 26 }
                            Image { anchors.fill: parent; source: card.row.coverUrl || ""; fillMode: Image.PreserveAspectFit; asynchronous: true }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            RowLayout {
                                Layout.fillWidth: true
                                Text { Layout.fillWidth: true; text: card.row.temporary.name; textFormat: Text.PlainText; color: "#f5f6fb"; font.pixelSize: 16; wrapMode: Text.Wrap }
                                Action {
                                    objectName: "archiveCopyName"; text: "Copy"; Accessible.name: "Copy anime name"
                                    contentItem: Item {
                                        implicitWidth: 16; implicitHeight: 16
                                        Rectangle { x: 1; y: 1; width: 10; height: 11; radius: 1; color: "transparent"; border.color: "#aa8cff" }
                                        Rectangle { x: 5; y: 5; width: 10; height: 11; radius: 1; color: "#161a29"; border.color: "#aa8cff" }
                                    }
                                    onClicked: backend.copyFolderName(card.row.temporary.name)
                                    ToolTip.visible: hovered; ToolTip.text: "Copy name"
                                }
                            }
                            Rectangle {
                                Layout.maximumWidth: parent.width
                                implicitWidth: statusText.implicitWidth + 12; implicitHeight: statusText.implicitHeight + 6
                                radius: 5; color: card.row.review ? "#30291e" : "#2c2447"
                                Text { id: statusText; anchors.fill: parent; anchors.margins: 3; anchors.leftMargin: 6; anchors.rightMargin: 6; text: archive.statusLabel(card.row); color: card.row.review ? "#e6bb68" : "#aa8cff"; font.pixelSize: 11; wrapMode: Text.Wrap }
                            }
                            Text {
                                Layout.fillWidth: true
                                text: card.row.counts ? "Archived: " + card.row.counts.archived + " · Pending: " + card.row.counts.pending + (card.row.review ? " (provisional)" : "") + " · Both: " + card.row.counts.both : "Episode counts unavailable — destination/review needed"
                                color: "#c1c6d4"; font.weight: Font.DemiBold; wrapMode: Text.Wrap
                            }
                            Text { Layout.fillWidth: true; text: card.row.previous_destination ? "Missing: " + card.row.previous_destination[0] + " · " + card.row.previous_destination[1] : archive.destinationText(card.row.destination || ((card.row.candidates || []).length === 1 ? card.row.candidates[0] : null)); textFormat: Text.PlainText; color: "#929bb0"; wrapMode: Text.Wrap; font.pixelSize: 11 }
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: text.length > 0
                        text: archive.warnings(card.row)
                        color: "#e6bb68"; wrapMode: Text.Wrap; font.pixelSize: 11
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 8
                        Action {
                            id: confirmSuggestion
                            objectName: "archiveConfirmSuggestion"
                            text: "Confirm destination"
                            visible: card.row.state === "suggested"
                            onClicked: { const d = card.row.candidates[0]; backend.confirmArchiveDestination(card.row.temporary.path, d.source_id, d.path, card.row.temporary.source_id) }
                        }
                        Action { text: card.row.state === "confirmed" ? "Change destination…" : "Choose destination…"; onClicked: archive.chooseDestination(card.row) }
                        Action { text: "Unlink destination"; visible: card.row.state === "confirmed" || card.row.state === "invalid_destination" || card.row.state === "overlap_review"; onClicked: backend.unlinkArchiveDestination(card.row.temporary.path, card.row.temporary.source_id) }
                        Action { objectName: "archiveEpisodeToggle"; text: card.expanded ? "Hide episodes" : "Episode details"; onClicked: archive.toggle(card.row) }
                    }
                    Flickable {
                        id: episodeScroll
                        objectName: "archiveEpisodeScroll"
                        Layout.fillWidth: true; Layout.preferredHeight: 180; visible: card.expanded
                        clip: true; boundsBehavior: Flickable.StopAtBounds
                        flickableDirection: Flickable.VerticalFlick
                        activeFocusOnTab: true
                        Keys.onPressed: function(event) {
                            let amount = 0
                            if (event.key === Qt.Key_PageDown) amount = height * 0.8
                            else if (event.key === Qt.Key_PageUp) amount = -height * 0.8
                            else if (event.key === Qt.Key_Down) amount = 24
                            else if (event.key === Qt.Key_Up) amount = -24
                            else return
                            contentY = Math.max(originY,Math.min(contentY+amount,originY+Math.max(0,contentHeight-height)))
                            event.accepted = true
                        }
                        ScrollBar.vertical: ScrollBar {}
                        WheelHandler { target: null; onWheel: function(event) { archive.wheelScroll(episodeScroll,event) } }
                        TextArea.flickable: TextArea {
                            objectName: "archiveEpisodeDetails"
                            readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap
                            color: "#f5f6fb"; background: Rectangle { color: "#0e111c"; radius: 8 }
                            text: backend.archiveDetail.path === card.row.temporary.path && backend.archiveDetail.busy ? "Loading…" : (archive.details[archive.key(card.row)] || "Expand details again to load this folder.")
                        }
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: "Counts are distinct episode identifiers, not file counts or verified media copies."; color: "#929bb0"; font.pixelSize: 11; wrapMode: Text.Wrap }
            Action { text: "Previous"; enabled: (archive.state.offset || 0) > 0 && !archive.state.busy; onClicked: backend.previousArchivePage() }
            Action { text: "Next page"; enabled: !!archive.state.nextAfter && !archive.state.busy; onClicked: backend.nextArchivePage() }
        }
    }
    Dialog {
        id: picker
        objectName: "archiveDestinationDialog"
        property string folderPath: ""
        property string temporarySource: ""
        parent: Overlay.overlay
        width: Math.min(720, parent ? parent.width - 32 : 720)
        height: Math.min(560, parent ? parent.height - 32 : 560)
        x: parent ? (parent.width - width) / 2 : 0
        y: parent ? (parent.height - height) / 2 : 0
        modal: true; title: "Choose indexed destination"
        header: Label { text: picker.title; color: "#f5f6fb"; padding: 16; font.pixelSize: 16 }
        background: Rectangle { color: "#161a29"; border.color: "#262c3e"; radius: 14 }
        onClosed: backend.cancelArchivePicker()
        contentItem: ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                TextField {
                    id: destinationInput; objectName: "archiveDestinationSearch"
                    Layout.fillWidth: true; placeholderText: "Search indexed folder names"
                    color: "#f5f6fb"; placeholderTextColor: "#929bb0"; selectByMouse: true
                    Accessible.name: "Search indexed destination folders"
                    background: Rectangle { radius: 8; color: "#0e111c"; border.color: destinationInput.activeFocus ? "#aa8cff" : "#262c3e" }
                    onAccepted: backend.searchArchiveDestinations(picker.folderPath, text, picker.temporarySource)
                }
                Action { text: "Search"; onClicked: backend.searchArchiveDestinations(picker.folderPath, destinationInput.text, picker.temporarySource) }
            }
            Text { Layout.fillWidth: true; text: backend.archivePicker.busy ? "Searching saved indexes…" : (backend.archivePicker.error || (backend.archivePicker.truncated ? "Showing first 50 matches. Narrow your search." : "Select a destination explicitly. Known overlapping sources cannot be selected.")); color: "#929bb0"; textFormat: Text.PlainText; wrapMode: Text.Wrap }
            ListView {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
                model: backend.archivePicker.items || []
                ScrollBar.vertical: ScrollBar {}
                delegate: ColumnLayout {
                    required property var modelData
                    width: ListView.view.width
                    Text { Layout.fillWidth: true; text: archive.destinationText(modelData); color: "#f5f6fb"; textFormat: Text.PlainText; wrapMode: Text.Wrap }
                    Text { visible: modelData.overlap !== false; text: modelData.overlap === true ? "Overlapping source — unavailable" : "Physical overlap unverified; check manually"; color: "#e6bb68" }
                    Action { objectName: "archivePickerConfirm"; text: "Confirm destination"; enabled: modelData.overlap !== true; onClicked: { backend.confirmArchiveDestination(picker.folderPath, modelData.source_id, modelData.path, picker.temporarySource); picker.close() } }
                }
            }
            Action { text: "Cancel"; onClicked: picker.close() }
        }
    }
}
