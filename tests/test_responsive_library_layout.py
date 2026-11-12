"""Geometry-only QML fixture. No controller, services, database or network imports."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ResponsiveLibraryTests(unittest.TestCase):
    def test_logical_geometry_at_both_scales(self):
        for scale in ("1", "1.5"):
            with self.subTest(scale=scale):
                result = subprocess.run([sys.executable, "-B", __file__, "--scenario"],
                    env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=scale,
                             QML_DISABLE_DISK_CACHE="1"), capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def scenario(measure=False):
    from PySide6.QtCore import QObject, QUrl, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    app = QGuiApplication([])
    QQuickStyle.setStyle("Basic")
    engine = QQmlEngine()
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
    fields = dict(currentView="source", selectedDrive="test", currentPath="", searchTerm="",
        status="Ready", statusLevel="info", statusMode="idle", notificationMessage="",
        notificationLevel="info", notificationTarget="", notificationRevision=0,
        isSearching=False, isIndexing=False, plannedBusy=False, importsBusy=False, canNavigateUp=False,
        items=[], plannedEntries=[], excludedFolders=[], breadcrumbs=[dict(label="Library", path="")],
        drives=[dict(id="test", label="Synthetic source", letter="X:", fileCount=80, kind="drive", connected=False)],
        plannedSearch=dict(entryId="", query="", busy=False, error="", results=[]))
    functions = """
        signal plannedChanged(); signal exclusionsChanged();
        signal coverReady(string key, string url);
        property string lastAction: ""
        function coverUrlFor(key) { return "" }
        function requestCoversForRange(first, last) {}
        function refreshImports() { lastAction = "refresh" }
        function savePlannedTitle(id, title) { lastAction = "save" }
        function linkPlannedEntry(id, url) { lastAction = "link" }
        function removePlanned(id) { lastAction = "remove" }
    """
    backend_component = QQmlComponent(engine)
    backend_component.setData(("import QtQuick\nQtObject {\n" + "\n".join(
        f"property var {key}: {json.dumps(value)}" for key, value in fields.items()) + functions + "\n}").encode(), QUrl())
    backend = backend_component.create()
    assert backend, backend_component.errors()
    engine.rootContext().setContextProperty("controller", backend)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path("ui/qml/Main.qml").resolve())))
    window = component.create()
    assert window, component.errors()
    kept = []
    def evaluate(obj, expression):
        expr = QQmlExpression(engine.contextForObject(obj), obj, expression)
        value, _ = expr.evaluate()
        assert not expr.hasError(), expr.error().toString()
        kept.append(value)
        return value
    def settle():
        QTest.qWait(40)
        app.processEvents()
    def visuals(item):
        yield item
        for child in item.childItems():
            yield from visuals(child)
    rows = [dict(name="Very long title 日本語 " * 5, path=f"Anime/{i}", sourceId="test",
        isFolder=True, size="", coverKey="", coverUrl="", coverStatus="no_match", manualId=0,
        sourceLabel="Long source label " * 5, sourceStatus="Disconnected", isFavorite=False, isToWatch=False)
        for i in range(80)]
    planned_rows = [dict(id=str(i), title=row["name"], anilistTitle="Linked title " * 10,
        anilistId=1, localCoverUrl="") for i, row in enumerate(rows)]
    backend.setProperty("items", rows); backend.itemsChanged.emit()
    backend.setProperty("plannedEntries", planned_rows); backend.plannedChanged.emit()
    results = []
    try:
        for width, height in ((900,600),(1280,740),(1280,800),(1600,900),(1920,1080)):
            window.setWidth(width); window.setHeight(height)
            for view in ("source", "favorites", "to_watch", "planned"):
                backend.setProperty("currentView", view)
                for grid in (True, False):
                    window.setProperty("gridView", grid); settle()
                    entries = window.findChild(QObject, "plannedEntriesView") if view == "planned" else evaluate(window, "mediaGrid" if grid else "mediaList")
                    geometry = {key: entries.property(key) for key in ("width","height")}
                    cell = entries.property("cellHeight") if (grid or view == "planned") else 58
                    assert geometry["height"] > 0, (width, height, view, geometry)
                    assert evaluate(window, "libraryStatusPanel.height") == 64
                    if not measure:
                        assert window.property("compactLayout") == (height < 840)
                        assert window.property("contentInset") == (12 if height < 840 else 18)
                        assert evaluate(window, "header.height") == (60 if height < 840 else 76)
                        assert evaluate(window, "mediaGrid.parent.parent.spacing") == (9 if height < 840 else 19)
                        assert evaluate(window, "mediaGrid.parent.parent.anchors.margins") == (16 if height < 840 else 32)
                        if grid and width == 1280:
                            assert geometry["height"] >= cell, (view, geometry, cell)
                    if grid and view in ("source", "planned"):
                        results.append(dict(size=[width,height], view=view, viewport=round(geometry["height"],2),
                            cell=round(cell,2), deficit=round(max(0,cell-geometry["height"]),2)))
                    if view == "planned":
                        buttons = [item for item in visuals(window.contentItem()) if item.property("text") in
                            ("Add to Planned", "Refresh imports", "Add from AniList URL...") and item.inherits("QQuickButton")]
                        assert len(buttons) == 3
                        if not measure and width >= 1280:
                            assert len({round(item.y()) for item in buttons}) == 1
                        for item in buttons:
                            assert item.x() >= 0 and item.x()+item.width() <= item.parentItem().width()+1
                        if not measure:
                            backend.setProperty("plannedBusy", True); settle()
                            toolbar = window.findChild(QObject, "plannedActionToolbar")
                            for item in toolbar.childItems():
                                if item.isVisible(): assert item.x()+item.width() <= toolbar.width()+1
                            backend.setProperty("plannedBusy", False); settle()
                    # Exercise reflow without changing selection or resetting scroll.
                    if grid and view == "source":
                        window.setProperty("selectedItemIndex", 4)
                        entries.setProperty("contentY", cell)
                        window.setHeight(height+1); settle(); window.setHeight(height); settle()
                        assert window.property("selectedItemIndex") == 4
            backend.setProperty("currentView", "source"); backend.setProperty("currentPath", "Anime")
            backend.setProperty("items", rows[:3] + [dict(rows[0], isFolder=False, name="Episode.mkv", path="Anime/file", size="1 GB")])
            backend.itemsChanged.emit()
            window.setProperty("gridView", True); settle()
            assert evaluate(window, "folderContentsGrid.visible")
            assert evaluate(window, "folderContentsGrid.count") >= 2
            backend.setProperty("items", rows); backend.itemsChanged.emit()
            backend.setProperty("currentPath", "")
        if not measure:
            backend.setProperty("currentView", "source")
            window.setProperty("selectedItemIndex", 4)
            for height in (839, 840, 839, 840):
                window.setHeight(height); settle()
                assert window.property("compactLayout") == (height < 840)
                assert window.property("selectedItemIndex") == 4
        # A deliberately narrow standalone Planned viewport must wrap and stay bounded.
        planned = window.findChild(QObject, "plannedEntriesView").parentItem().parentItem()
        backend.setProperty("currentView", "planned"); backend.setProperty("plannedBusy", True)
        if not measure:
            toolbar = window.findChild(QObject, "plannedActionToolbar")
            assert toolbar is not None
            evaluate(planned, "anchors.fill = undefined; width = 340; height = 500")
            settle()
            assert toolbar.height() > 40
            for item in toolbar.childItems():
                if item.isVisible(): assert item.x()+item.width() <= toolbar.width()+1
            for button in visuals(toolbar):
                if button.property("text") == "Refresh imports" and button.inherits("QQuickButton"):
                    button.forceActiveFocus()
                    QTest.keyClick(window, Qt.Key_Space); settle()
                    backend.setProperty("importsBusy", True); settle()
                    assert not button.property("enabled")
                    backend.setProperty("importsBusy", False)
            assert backend.property("lastAction") == "refresh"
            evaluate(planned, 'editEntry("", "")'); settle()
            edit = next(obj for obj in window.findChildren(QObject) if obj.property("title") == "Add to Planned")
            edit.property("contentItem").setProperty("text", "Synthetic title")
            evaluate(edit, "accept()")
            assert backend.property("lastAction") == "save"
            evaluate(planned, 'linkEntry("", 0)'); settle()
            link = window.findChild(QObject, "plannedLinkDialog")
            link.property("contentItem").setProperty("text", "https://anilist.co/anime/1/")
            evaluate(link, "accept()")
            assert backend.property("lastAction") == "link"
        assert not warnings, warnings
        print(json.dumps(results))
    finally:
        window.close()
        del component, window
        engine.rootContext().setContextProperty("controller", backend)


if __name__ == "__main__":
    if "--scenario" in sys.argv or "--measure" in sys.argv:
        scenario("--measure" in sys.argv)
    else:
        unittest.main()
