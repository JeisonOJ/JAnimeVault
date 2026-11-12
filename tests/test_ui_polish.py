"""Headless real-input menu and hover checks; isolated data, no scanning/network."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class UIPolishTests(unittest.TestCase):
    def test_menus_and_hover(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario"],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1", PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Menu geometry, disabled actions and hover passed", result.stdout)


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
    app = QGuiApplication([])
    QQuickStyle.setStyle("Basic")
    root = Path(__file__).parent / ".ui-polish-runtime"
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temporary, ExitStack() as cleanup:
        db.APP_DIR = Path(temporary); db.DB_PATH = db.APP_DIR / "index.db"
        store = db.IndexStore()
        cleanup.callback(store.conn.close)
        with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService"):
            controller = AppController()
        cleanup.callback(controller.shutdown)
        controller._request_items = Mock()
        controller.unlinkAniListEntry = Mock()
        controller.unlinkPlannedEntry = Mock()
        controller._selected_drive = "TEST"
        store.replace_drive("TEST", "Test", "", [("TEST", "Folder", "", "Folder", 1, None, None)])
        engine = QQmlApplicationEngine()
        engine.rootContext().setContextProperty("controller", controller)
        engine.load(QUrl.fromLocalFile(str(Path("ui/qml/Main.qml").resolve())))
        assert engine.rootObjects()
        window = engine.rootObjects()[0]
        cleanup.callback(window.close)
        retained = []
        def evaluate(obj, expression):
            query = QQmlExpression(engine.contextForObject(obj), obj, expression)
            value, undefined = query.evaluate()
            assert not query.hasError(), query.error().toString()
            assert not undefined, expression
            retained.append(value)
            return value
        def move(item):
            QTest.mouseMove(window, item.mapToScene(item.boundingRect().center()).toPoint())
            QTest.qWait(160)
        def check_menu(menu, index, linked, call):
            evaluate(menu, "popup(); true")
            QTest.qWait(150)
            item = evaluate(menu, f"itemAt({index})")
            assert item.property("visible") and item.property("enabled") == linked
            assert evaluate(item, "contentItem.color.toString()") == ("#f5f6fb" if linked else "#596176")
            evaluate(item, "highlighted = true")
            assert evaluate(item, "background.color.toString()") == ("#20263a" if linked else "#00000000")
            for i in range(evaluate(menu, "count")):
                row = evaluate(menu, f"itemAt({i})")
                assert row.height() > 0
                if i:
                    previous = evaluate(menu, f"itemAt({i-1})")
                    assert abs(row.y() - previous.y() - previous.height()) <= 2
            assert row.y() + row.height() <= evaluate(menu, "contentItem.height") + 1
            assert evaluate(item, "contentItem.width > 0 && contentItem.elide === Text.ElideRight")
            assert item.width() > 0 and item.height() == 34
            before = call.call_count
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, item.mapToScene(item.boundingRect().center()).toPoint())
            QTest.qWait(50)
            assert call.call_count == before + int(linked)
            evaluate(menu, "close(); true")
            QTest.qWait(150)
        for grid in (True, False):
            window.setProperty("gridView", grid)
            for linked in (False, True):
                controller._items = [dict(name="Folder", path="Folder", sourceId="TEST", isFolder=True,
                    manualId=7 if linked else 0, coverStatus="no_match", coverKey="folder", coverUrl="", size="", isFavorite=False, isToWatch=False)]
                controller.itemsChanged.emit(); QTest.qWait(120)
                delegate = evaluate(window, ("mediaGrid" if grid else "mediaList") + ".itemAtIndex(0)")
                window.setProperty("selectedItemIndex", 0)
                if grid:
                    QTest.qWait(150)
                    assert evaluate(delegate.findChild(QObject, "libraryCardBackground"), "border.color.toString()") == "#aa8cff"
                else:
                    assert evaluate(delegate, "color.toString()") == "#2c2447"
                menu = delegate.findChild(QObject, "libraryCardMenu") if grid else evaluate(delegate, "listFolderMenu")
                check_menu(menu, 6, linked, controller.unlinkAniListEntry)
                # Appended Library action uses the shared responsive dialog in both layouts.
                action = evaluate(menu, "itemAt(count - 1)")
                assert action.property("text") == "Search AniList" and action.property("enabled")
                evaluate(action, "triggered(); true")
                QTest.qWait(100)
                search = evaluate(window, "librarySearchDialog")
                assert search.property("visible") and controller.plannedSearch["query"] == "Folder"
                assert controller._folder_search_target[:2] == ("TEST", "Folder")
                evaluate(search, "reject(); true")
                QTest.qWait(100)
                assert not search.property("visible") and not controller.plannedSearch["entryId"]
                evaluate(action, "triggered(); true")
                request = controller._candidate_request
                controller.cover_service.process_events.return_value = [("planned_candidates", *request,
                    [dict(id=7, title="Candidate", alternate="", year="2026", format="TV", localCoverUrl="")], "")]
                controller.process_cover_events()
                controller.cover_service.process_events.return_value = []
                QTest.qWait(100)
                candidates = search.findChild(QObject, "plannedCandidateList")
                candidate = evaluate(candidates, "itemAtIndex(0)")
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                    candidate.mapToScene(candidate.boundingRect().center()).toPoint())
                with patch.object(controller, "_link_anilist_entry") as link:
                    link.assert_not_called()
                    confirm = evaluate(search, "standardButton(Dialog.Ok)")
                    assert confirm.property("enabled")
                    QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                        confirm.mapToScene(confirm.boundingRect().center()).toPoint())
                    link.assert_called_once_with("TEST", "Folder", "https://anilist.co/anime/7/", require_indexed=True)
                QTest.qWait(100)
        for mode in ("favorites", "to_watch", "planned"):
            controller._current_view = "source"; controller.selectionChanged.emit()
            nav = evaluate(window, {"favorites": "favoritesNavigation", "to_watch": "toWatchNavigation", "planned": "plannedNavigation"}[mode])
            move(nav)
            assert evaluate(nav, "background.color.toString()") == "#20263a"
            controller._current_view = mode; controller.selectionChanged.emit()
            assert evaluate(nav, "background.color.toString()") == "#2c2447"
        plannedGrid = window.findChild(QObject, "plannedEntriesView")
        for grid in (True, False):
            window.setProperty("gridView", grid)
            for linked in (False, True):
                controller._planned_entries = [dict(id="test", title="Title", anilistId=7 if linked else None, anilistTitle=None, localCoverUrl="")]
                controller.plannedChanged.emit(); QTest.qWait(120)
                delegate = evaluate(plannedGrid, "itemAtIndex(0)")
                mouse = evaluate(delegate, "plannedMouse")
                move(mouse)
                assert evaluate(delegate, "children[0].color.toString()") == "#20263a"
                check_menu(evaluate(delegate, "entryMenu"), 3, linked, controller.unlinkPlannedEntry)
        for name in ("addPlanned", "addFromAniList"):
            button = evaluate(delegate, name); move(button)
            assert evaluate(button, "background.color.toString()") == "#20263a"
            button.setProperty("enabled", False)
            assert evaluate(button, "contentItem.color.toString()") == "#596176"
            assert evaluate(button, "background.color.toString()") == "#161a29"
        controller.shutdown(); window.close(); store.conn.close()
    print("Menu geometry, disabled actions and hover passed")


if __name__ == "__main__" and "--scenario" in sys.argv:
    scenario()
