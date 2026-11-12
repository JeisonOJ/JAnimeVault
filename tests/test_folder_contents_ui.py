"""Virtualized mixed folder contents with isolated data and real Qt input."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class FolderContentsUITests(unittest.TestCase):
    def test_mixed_folder_grid(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario"],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1", PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Mixed folder contents passed", result.stdout)


def scenario():
    from contextlib import ExitStack
    import tempfile
    from unittest.mock import Mock, patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QObject, QUrl, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    import indexer.database as db
    from indexer.service import IndexerService
    from ui.controller import AppController
    app = QGuiApplication([]); QQuickStyle.setStyle("Basic")
    runtime = Path(__file__).parent / ".folder-contents-ui-runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as temporary, ExitStack() as cleanup:
        db.APP_DIR = Path(temporary); db.DB_PATH = db.APP_DIR / "index.db"
        store = db.IndexStore(); cleanup.callback(store.conn.close)
        with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService"):
            controller = AppController()
        cleanup.callback(controller.shutdown)
        controller._request_items = Mock()
        controller._selected_drive = "TEST"
        controller._current_path = "Anime"
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
        engine.rootContext().setContextProperty("controller", controller)
        engine.load(QUrl.fromLocalFile(str(Path("ui/qml/Main.qml").resolve())))
        assert engine.rootObjects(), warnings
        window = engine.rootObjects()[0]; cleanup.callback(window.close)
        mixed = window.findChild(QObject, "folderContentsGrid")
        kept = []
        def evaluate(obj, code):
            expression = QQmlExpression(engine.contextForObject(obj), obj, code)
            value, undefined = expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            assert not undefined, code
            kept.append(value); return value
        def item(folder, number=0):
            return dict(name=("Season" if folder else "勇者 — episode " + "long " * 20 + str(number) + ".mkv"),
                path="Anime/" + str(number), sourceId="TEST", isFolder=folder, size="" if number % 2 else "1 GB",
                coverKey="season" if folder else "", coverUrl="", coverStatus="no_match", manualId=0,
                isFavorite=False, isToWatch=False)
        def show(items):
            controller._items = items; controller.itemsChanged.emit(); QTest.qWait(180)
        def visuals(root, name):
            found = [root] if root.objectName() == name else []
            for child in root.childItems(): found.extend(visuals(child, name))
            kept.extend(found)
            return found
        assert mixed.property("visible")
        show([]); assert mixed.property("count") == 0
        show([item(True, i) for i in range(7)])
        assert evaluate(mixed, "model.every(row => row.folders)")
        row = evaluate(mixed, "itemAtIndex(0)")
        cards = visuals(row, "libraryCoverCard")
        assert len(cards) == mixed.property("columns"), (len(cards), mixed.property("columns"), warnings)
        card = cards[0]
        controller.openCardItem("TEST", "Anime/Nested", True)
        assert controller.currentPath == "Anime/Nested"  # Indexed navigation works disconnected.
        assert mixed.property("visible")
        show([item(True), item(False, 1), item(False, 2)])
        assert evaluate(mixed, "model.map(row => row.folders ? 'folder' : 'file').join(',')") == "folder,file,file"
        assert evaluate(mixed, "model[1].gap")
        window.setProperty("selectedItemIndex", 0); mixed.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_Down)
        assert window.property("selectedItemIndex") == 1
        window.setProperty("selectedItemIndex", -1)
        row = evaluate(mixed, "itemAtIndex(1)")
        file = row.findChild(QObject, "folderFileRow")
        QTest.mouseMove(window, file.mapToScene(file.boundingRect().center()).toPoint()); QTest.qWait(80)
        assert evaluate(file, "color.toString()") == "#20263a"
        controller.openCardItem = Mock()
        QTest.mouseClick(window, Qt.RightButton, Qt.NoModifier, file.mapToScene(file.boundingRect().center()).toPoint())
        assert window.property("selectedItemIndex") == 1
        controller.openCardItem.assert_not_called()  # Existing files have no folder context menu.
        QTest.mouseDClick(window, Qt.LeftButton, Qt.NoModifier, file.mapToScene(file.boundingRect().center()).toPoint())
        controller.openCardItem.assert_called_with("TEST", "Anime/1", False)
        assert evaluate(file, "color.toString()") == "#2c2447"
        show([item(False, i) for i in range(5000)])
        assert mixed.property("count") == 5000
        assert evaluate(mixed, "model.every(row => !row.folders && !row.gap)")
        assert len(mixed.findChildren(QObject, "folderFileRow")) < 100
        evaluate(mixed, "positionViewAtIndex(4999, ListView.End); true"); QTest.qWait(250)
        assert evaluate(mixed, "itemAtIndex(4999) !== null")
        assert len(mixed.findChildren(QObject, "folderFileRow")) < 150
        mixed.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_End); QTest.qWait(80)
        assert window.property("selectedItemIndex") == 4999
        QTest.keyClick(window, Qt.Key_Return)
        controller.openCardItem.assert_called_with("TEST", "Anime/4999", False)
        window.setProperty("gridView", False); QTest.qWait(50)
        assert not mixed.property("visible") and evaluate(window, "mediaList.visible")
        window.setProperty("gridView", True)
        for mode in ("favorites", "to_watch", "planned"):
            controller._current_view = mode; controller.selectionChanged.emit()
            assert not mixed.property("visible")
        controller._current_view = "source"; controller._current_path = ""; controller.selectionChanged.emit()
        assert not mixed.property("visible") and evaluate(window, "mediaGrid.visible")
        controller._current_path = "Nested"; controller._search_term = "episode"; controller.selectionChanged.emit()
        assert not mixed.property("visible") and evaluate(window, "mediaGrid.visible")
        unexpected = [warning for warning in warnings if "Controls/Basic/MenuItem.qml:12:" not in warning]
        assert not unexpected, unexpected
    print("Mixed folder contents passed")


if __name__ == "__main__" and "--scenario" in sys.argv:
    scenario()
