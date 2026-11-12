"""Same-context updates through real controller queries and virtualized QML views."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ScrollStabilityTests(unittest.TestCase):
    def test_grid_list_and_mixed_anchors(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario"],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1", PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Stable browsing anchors passed", result.stdout)


def scenario():
    from contextlib import ExitStack
    import tempfile
    from unittest.mock import Mock, patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QUrl, QBuffer, QByteArray, QIODevice, QObject
    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    import indexer.database as db
    from indexer.service import IndexerService
    from ui.controller import AppController
    app = QGuiApplication([]); QQuickStyle.setStyle("Basic")
    runtime = Path(__file__).parent / ".scroll-test-runtime"; runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary, ExitStack() as cleanup:
        db.APP_DIR = Path(temporary).resolve(); db.DB_PATH = db.APP_DIR / "index.db"
        store = db.IndexStore(); cleanup.callback(store.conn.close)
        source, label, path, _ = store.register_folder_source(str(db.APP_DIR / "unavailable"))
        rows = [(source, f"anime{i:04}", "", f"Title {i:04}", 1, None, 0) for i in range(1200)]
        rows += [(source, f"parent/file{i:04}.mkv", "parent", f"Episode {i:04}.mkv", 0, 1024, 0) for i in range(3000)]
        store.replace_drive(source, label, path, rows)
        cover = Mock(); cover.process_events.return_value = []
        with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService", return_value=cover):
            c = AppController()
        cleanup.callback(c.shutdown)
        c._selected_drive = source
        def query():
            c._query_number += 1
            c._query_items(c._query_number, c.selectedDrive, c.currentPath, c.searchTerm, c.currentView, c.drives)
        c._request_items = query
        engine = QQmlApplicationEngine(); warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        engine.rootContext().setContextProperty("controller", c)
        engine.load(QUrl.fromLocalFile(str(Path("ui/qml/Main.qml").resolve())))
        assert engine.rootObjects(), warnings
        window = engine.rootObjects()[0]; cleanup.callback(window.close)
        retained = []
        def evaluate(obj, code):
            expression = QQmlExpression(engine.contextForObject(obj), obj, code)
            value, undefined = expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            assert not undefined, code
            retained.append(value); return value
        def top(view, mixed=False):
            row = evaluate(view, "indexAt(1, contentY + 2)")
            assert row >= 0, (view.property("contentY"), row)
            index = evaluate(view, f"model[{row}].indices[0]") if mixed else row
            item = evaluate(view, f"itemAtIndex({row})")
            return c.items[index]["path"], item.y() - view.property("contentY"), index
        def visible_anchor(view, path, grid):
            first = top(view)[2]
            index = next(i for i, item in enumerate(c.items) if item["path"] == path)
            columns = evaluate(view, "Math.max(1, Math.floor(width / cellWidth))") if grid else 1
            assert first <= index < first + columns, (path, top(view), index)
        query(); QTest.qWait(100)
        presentation = evaluate(window, "libraryPresentation")
        changed = []
        presentation.dataChanged.connect(lambda first, last, roles: changed.append((first.row(), last.row())))
        data = QByteArray(); buffer = QBuffer(data); buffer.open(QIODevice.WriteOnly)
        image = QImage(1, 1, QImage.Format_RGB32); image.fill(0xff00aa00); image.save(buffer, "PNG"); buffer.close()
        cover_url = "data:image/png;base64," + bytes(data.toBase64()).decode("ascii")
        manual_data = QByteArray(); manual_buffer = QBuffer(manual_data); manual_buffer.open(QIODevice.WriteOnly)
        image.fill(0xff0000bb); image.save(manual_buffer, "PNG"); manual_buffer.close()
        manual_url = "data:image/png;base64," + bytes(manual_data.toBase64()).decode("ascii")
        for grid in (True, False):
            window.setProperty("gridView", grid)
            view = evaluate(window, "mediaGrid" if grid else "mediaList")
            evaluate(view, "positionViewAtIndex(600, ListView.Beginning); forceLayout(); true")
            QTest.qWait(80)
            before = top(view); window.setProperty("selectedItemIndex", before[2])
            QTest.qWait(40); before = top(view)
            geometry = (view.mapToScene(view.boundingRect().topLeft()), view.width(), view.height())
            c._set_status("Notification " + str(grid) + " long message " * 50, "warning")
            QTest.qWait(40)
            assert top(view)[:2] == before[:2]
            assert (view.mapToScene(view.boundingRect().topLeft()), view.width(), view.height()) == geometry
            c.dismissNotification(); QTest.qWait(40)
            assert top(view)[:2] == before[:2]
            assert (view.mapToScene(view.boundingRect().topLeft()), view.width(), view.height()) == geometry
            delegate = evaluate(view, f"itemAtIndex({before[2]})")
            key = c.items[before[2]]["coverKey"]
            cover.process_events.return_value = [("ready", key, cover_url)]
            c.process_cover_events(); QTest.qWait(40)
            assert top(view)[:2] == before[:2], (grid, "cover", before, top(view))
            if grid:
                image_item = delegate.findChild(QObject, "libraryCoverImage")
                assert image_item.property("source").toString() == cover_url
                snapshot = [dict(item) for item in c.items]
                snapshot[before[2]]["manualId"] = 7
                snapshot[before[2]]["coverUrl"] = manual_url
                c._apply_query(c._query_number, snapshot)
                assert image_item.property("source").toString() == manual_url
                c.process_cover_events()  # Delayed automatic ready must not replace a manual override.
                assert image_item.property("source").toString() == manual_url
            for action, field in ((c.setFavorite, "isFavorite"), (c.setToWatch, "isToWatch")):
                changed.clear()
                flag = not c.items[before[2]][field]
                action(source, before[0], flag); QTest.qWait(40)
                assert c.items[before[2]][field] == flag
                assert len(changed) == 1, len(changed)
                assert top(view)[:2] == before[:2], (grid, before, top(view))
                assert evaluate(window, "libraryPresentation") == presentation
                assert evaluate(view, f"itemAtIndex({before[2]})") == delegate
                assert window.property("selectedItemIndex") == before[2]
                if grid: assert image_item.property("source").toString() == cover_url
            # Delayed stale query cannot replace newer flag/cover data.
            c._apply_query(c._query_number - 1, [])
            assert top(view)[:2] == before[:2]
        for table, select, toggle in (("folder_favorites", c.selectFavorites, c.setFavorite),
                                      ("folder_to_watch", c.selectToWatch, c.setToWatch)):
            with store.conn:
                store.conn.executemany(f"INSERT OR IGNORE INTO {table}(source_id,normalized_path) VALUES(?,?)", [(source, row[1]) for row in rows[:1200]])
            select(); QTest.qWait(50)
            for grid in (True, False):
                window.setProperty("gridView", grid)
                view = evaluate(window, "mediaGrid" if grid else "mediaList")
                evaluate(view, "positionViewAtIndex(600, ListView.Beginning); forceLayout(); true"); QTest.qWait(50)
                before = top(view)
                toggle(source, c.items[0]["path"], False); QTest.qWait(50)
                visible_anchor(view, before[0], grid)
                removed = top(view); successor = c.items[removed[2] + 1]["path"]
                toggle(source, removed[0], False); QTest.qWait(50)
                visible_anchor(view, successor, grid)
        c.selectDrive(source); c.openFolder("parent"); QTest.qWait(80)
        mixed = evaluate(window, "folderContentsGrid")
        window.setProperty("gridView", True); QTest.qWait(40)
        evaluate(mixed, "positionViewAtIndex(1700, ListView.Beginning); forceLayout(); true"); QTest.qWait(60)
        before = top(mixed, True)
        snapshot = [dict(item) for item in c.items]; snapshot[1700]["size"] = "2 GB"
        c._apply_query(c._query_number, snapshot); QTest.qWait(50)
        assert top(mixed, True)[:2] == before[:2]
        # Structural file insertion before the viewport must retain the same file.
        added = dict(snapshot[0], path="parent/inserted.mkv", name="Inserted")
        c._apply_query(c._query_number, [added] + snapshot); QTest.qWait(50)
        assert top(mixed, True)[0] == before[0]
        assert len(mixed.findChildren(QObject, "folderFileRow")) < 150
        c.openFolder("parent/nested"); QTest.qWait(50)
        assert mixed.property("count") == 0
        c.selectDrive(source); QTest.qWait(50)
        assert evaluate(window, "mediaGrid.contentY") <= 1
        c.search("Title 0001"); QTest.qWait(50)
        assert evaluate(window, "mediaGrid.contentY") <= 1
        assert c.items and len(c.items) == 1
        # Global Planned Grid/List keeps both its anchor and geometry across feedback.
        with store.conn:
            store.conn.executemany("INSERT INTO planned_anime(id,title) VALUES(?,?)",
                                  [(f"planned-{i}", f"Planned title {i:04}") for i in range(400)])
        c.selectPlanned()
        while c.importsBusy: QTest.qWait(10)
        planned = window.findChild(QObject, "plannedEntriesView")
        for grid in (True, False):
            window.setProperty("gridView", grid); QTest.qWait(60)
            evaluate(planned, "positionViewAtIndex(200, GridView.Beginning); forceLayout(); true")
            QTest.qWait(60)
            anchor = evaluate(planned, "indexAt(1, contentY + 2)")
            before = planned.property("contentY")
            geometry = (planned.mapToScene(planned.boundingRect().topLeft()), planned.width(), planned.height())
            c._set_status("Planned event " + str(grid), "error"); QTest.qWait(40)
            assert planned.property("contentY") == before
            assert evaluate(planned, "indexAt(1, contentY + 2)") == anchor
            assert (planned.mapToScene(planned.boundingRect().topLeft()), planned.width(), planned.height()) == geometry
            c.dismissNotification(); QTest.qWait(40)
            assert planned.property("contentY") == before
            assert (planned.mapToScene(planned.boundingRect().topLeft()), planned.width(), planned.height()) == geometry
        unexpected = [w for w in warnings if "Controls/Basic/MenuItem.qml:12:" not in w]
        assert not unexpected, unexpected
    print("Stable browsing anchors passed")


if __name__ == "__main__" and "--scenario" in sys.argv:
    scenario()
