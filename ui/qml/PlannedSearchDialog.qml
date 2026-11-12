import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    required property var backend
    property int selectedId: 0
    objectName: "plannedSearchDialog"
    title: "Search AniList"
    modal: true
    parent: Overlay.overlay
    anchors.centerIn: Overlay.overlay
    width: Math.min(720, Math.max(0, parent ? parent.width - 32 : 720))
    height: Math.min(680, Math.max(0, parent ? parent.height - 32 : 680), implicitHeight)
    standardButtons: Dialog.Ok | Dialog.Cancel
    background: Rectangle { color: "#161a29"; radius: 14; border.color: "#262c3e" }
    function searchEntry(entryId) {
        selectedId = 0
        backend.beginPlannedSearch(entryId)
        query.text = backend.plannedSearch.query
        open()
    }
    function searchFolder(sourceId, path) {
        selectedId = 0
        backend.beginFolderSearch(sourceId, path)
        query.text = backend.plannedSearch.query
        if (backend.plannedSearch.entryId) open()
    }
    function updateConfirmation() {
        if (visible) standardButton(Dialog.Ok).enabled = selectedId > 0 && !backend.plannedSearch.busy
    }
    onOpened: { standardButton(Dialog.Ok).text = "Link selected entry"; updateConfirmation() }
    onSelectedIdChanged: updateConfirmation()
    onAccepted: backend.confirmPlannedCandidate(selectedId)
    onClosed: backend.cancelPlannedSearch()
    Connections {
        target: dialog.backend
        function onPlannedSearchChanged() {
            dialog.selectedId = 0
            if (!dialog.backend.plannedSearch.entryId && dialog.visible) dialog.close()
            dialog.updateConfirmation()
        }
    }
    contentItem: ColumnLayout {
        spacing: 10
        RowLayout {
            TextField {
                id: query
                objectName: "candidateQuery"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                color: "#f5f6fb"
                selectByMouse: true
                placeholderText: "Anime title"
                Accessible.name: "AniList search query"
                background: Rectangle { color: "#0e111c"; radius: 6; border.color: "#262c3e" }
                onAccepted: dialog.backend.searchPlannedCandidates(text)
            }
            Button { text: dialog.backend.plannedSearch.error ? "Retry search" : "Search"; onClicked: dialog.backend.searchPlannedCandidates(query.text) }
        }
        Label {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: "Select a candidate, then confirm. An existing link is replaced only after the new lookup and cover succeed."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: "#929bb0"
        }
        BusyIndicator { visible: dialog.backend.plannedSearch.busy; running: visible; Layout.preferredHeight: 30 }
        Label {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            color: "#f0a0ad"
            text: dialog.backend.plannedSearch.error
            visible: text.length > 0
        }
        ListView {
            id: candidates
            objectName: "plannedCandidateList"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 0
            implicitHeight: Math.max(94, count * 94 + Math.max(0, count - 1) * spacing)
            clip: true
            spacing: 6
            model: dialog.backend.plannedSearch.results
            delegate: Rectangle {
                required property var modelData
                width: candidates.width
                height: 94
                radius: 8
                color: dialog.selectedId === modelData.id ? "#2c2447" : (candidateMouse.containsMouse ? "#20263a" : "#0e111c")
                border.color: dialog.selectedId === modelData.id ? "#aa8cff" : "#262c3e"
                RowLayout {
                    anchors.fill: parent; anchors.margins: 8; spacing: 10
                    Rectangle {
                        Layout.preferredWidth: 48; Layout.preferredHeight: 74; color: "#201b35"
                        Image { anchors.fill: parent; source: modelData.localCoverUrl || ""; asynchronous: true; fillMode: Image.PreserveAspectFit }
                        Text { anchors.centerIn: parent; visible: !modelData.localCoverUrl; text: "Anime"; color: "#929bb0"; font.pixelSize: 10 }
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Text { Layout.fillWidth: true; Layout.minimumWidth: 0; text: modelData.title; textFormat: Text.PlainText; color: "#f5f6fb"; elide: Text.ElideRight }
                        Text { Layout.fillWidth: true; Layout.minimumWidth: 0; text: modelData.alternate; textFormat: Text.PlainText; color: "#929bb0"; font.pixelSize: 11; elide: Text.ElideRight }
                        Text { Layout.fillWidth: true; Layout.minimumWidth: 0; text: [modelData.year, modelData.format, "AniList " + modelData.id].filter(Boolean).join(" · "); textFormat: Text.PlainText; elide: Text.ElideRight; color: "#929bb0"; font.pixelSize: 11 }
                    }
                }
                MouseArea { id: candidateMouse; anchors.fill: parent; hoverEnabled: true; onClicked: dialog.selectedId = modelData.id }
                Accessible.name: modelData.title
                Accessible.role: Accessible.ListItem
                Accessible.onPressAction: dialog.selectedId = modelData.id
            }
            Text { anchors.centerIn: parent; width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; visible: !dialog.backend.plannedSearch.busy && !dialog.backend.plannedSearch.error && candidates.count === 0; text: "No results. Edit the query and search again."; color: "#929bb0" }
            ScrollBar.vertical: ScrollBar {}
            Keys.onReturnPressed: { if (currentIndex >= 0) dialog.selectedId = dialog.backend.plannedSearch.results[currentIndex].id }
            activeFocusOnTab: true
        }
    }
}
