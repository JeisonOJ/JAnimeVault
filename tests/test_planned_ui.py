"""Real QML input -> confirmation -> controller -> isolated SQLite regression."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class PlannedDeletionUITests(unittest.TestCase):
    def test_grid_and_list_deletion(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario"],
                                cwd=Path(__file__).resolve().parents[1], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Grid/list Cancel and Confirm passed", result.stdout)


def scenario():
    import tempfile
    import time
    from unittest.mock import patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QObject, QUrl, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlEngine, QQmlComponent, QQmlExpression
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    import indexer.database as database
    from indexer.database import IndexStore
    from indexer.service import IndexerService
    from ui.controller import AppController

    app = QGuiApplication([])
    QQuickStyle.setStyle("Basic")
    root = Path("tests/.planned-ui-test-runtime")
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as temp:
        database.APP_DIR = Path(temp)
        database.DB_PATH = database.APP_DIR / "index.db"
        store = IndexStore()
        with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService"):
            controller = AppController()
        controller.selectPlanned()
        engine = QQmlEngine()
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path("ui/qml/PlannedView.qml").resolve())))
        view = component.createWithInitialProperties({"backend": controller})
        assert view is not None, component.errors()
        window = QQuickWindow()
        window.resize(700, 600)
        view.setParentItem(window.contentItem())
        view.setWidth(700)
        view.setHeight(600)
        window.show()

        def wait(predicate):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                app.processEvents()
                if predicate():
                    return
                QTest.qWait(10)
            raise AssertionError("Timed out waiting for QML state")

        retained = []  # retain wrappers for QML-created delegate items
        def find_visual(item, name):
            retained.append(item)
            found = item.findChild(QObject, name)
            if found:
                retained.append(found)
                return found
            for child in item.childItems():
                found = find_visual(child, name)
                if found:
                    return found

        def evaluate(obj, expression):
            result = QQmlExpression(engine.contextForObject(obj), obj, expression)
            value, undefined = result.evaluate()
            assert not result.hasError(), result.error().toString()
            assert not undefined
            return value

        for grid in (True, False):
            view.setProperty("gridMode", grid)
            first, _ = store.save_planned_title("Selected entry")
            from test_planned_imports import sample
            store.import_planned_capture(sample(text="Keep this entry"))
            second = next(row["id"] for row in store.planned_entries() if row["title"] == "Keep this entry")
            controller._request_items()
            wait(lambda: len(controller.plannedEntries) == 2)
            QTest.qWait(100)  # allow deferred delegate replacement/layout to settle
            wait(lambda: find_visual(view, "plannedEntryMouse") is not None)
            mouse = find_visual(view, "plannedEntryMouse")
            menu = find_visual(mouse.parentItem(), "plannedEntryMenu")
            remove_dialog = view.findChild(QObject, "plannedRemoveDialog")

            def open_confirmation():
                QTest.mouseClick(window, Qt.RightButton, Qt.NoModifier, mouse.mapToScene(mouse.boundingRect().center()).toPoint())
                wait(lambda: menu.property("visible"))
                QTest.qWait(150)
                assert menu.property("width") > 0
                assert evaluate(menu, "itemAt(0).text") == "Edit Planned Entry"
                assert evaluate(menu, "itemAt(1).text") == "Remove from Planned"
                assert evaluate(menu, "itemAt(2).text") == "Link AniList entry..."
                assert evaluate(menu, "itemAt(3).text") == "Unlink AniList entry"
                assert not evaluate(menu, "itemAt(3).enabled")
                action_item = evaluate(menu, "itemAt(1)")
                assert action_item.height() > 0 and action_item.width() > 0
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, action_item.mapToScene(action_item.boundingRect().center()).toPoint())
                wait(lambda: remove_dialog.property("visible"))

            open_confirmation()
            selected = remove_dialog.property("entryId")
            assert selected in {first, second}
            cancel = evaluate(remove_dialog, "standardButton(Dialog.Cancel)")
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, cancel.mapToScene(cancel.boundingRect().center()).toPoint())
            wait(lambda: not remove_dialog.property("visible"))
            assert len(store.planned_entries()) == 2
            # Appended search action is available for both manual/imported cards.
            def visible_mice(item):
                found = [item] if item.objectName() == "plannedEntryMouse" and item.property("visible") else []
                for child in item.childItems(): found.extend(visible_mice(child))
                retained.extend(found)
                return found
            candidate_mice = visible_mice(view)
            assert len(candidate_mice) == 2
            searched = set()
            for candidate_mouse in candidate_mice:
                candidate_menu = find_visual(candidate_mouse.parentItem(), "plannedEntryMenu")
                QTest.mouseClick(window, Qt.RightButton, Qt.NoModifier, candidate_mouse.mapToScene(candidate_mouse.boundingRect().center()).toPoint())
                wait(lambda: candidate_menu.property("visible"))
                QTest.qWait(150)
                assert evaluate(candidate_menu, "itemAt(4).text") == "Search AniList"
                action = evaluate(candidate_menu, "itemAt(4)")
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, action.mapToScene(action.boundingRect().center()).toPoint())
                search_dialog = view.findChild(QObject, "plannedSearchDialog")
                wait(lambda: search_dialog.property("visible"))
                entry_id, token = controller._candidate_request
                assert entry_id in {first, second}
                searched.add(entry_id)
                query_input = search_dialog.findChild(QObject, "candidateQuery")
                assert query_input.property("text") == next(row["title"] for row in store.planned_entries() if row["id"] == entry_id)
                assert not evaluate(search_dialog, "standardButton(Dialog.Ok).enabled")
                controller.cover_service.process_events.return_value = [("planned_candidates", entry_id, token,
                    [{"id": 7, "title": "Candidate", "alternate": "候補", "year": "2026", "format": "TV", "localCoverUrl": ""}], "")]
                controller.process_cover_events(); QTest.qWait(80)
                candidate_list = search_dialog.findChild(QObject, "plannedCandidateList")
                row = evaluate(candidate_list, "itemAtIndex(0)")
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, row.mapToScene(row.boundingRect().center()).toPoint())
                assert evaluate(search_dialog, "standardButton(Dialog.Ok).enabled")
                controller.cover_service.link_planned.assert_not_called()
                cancel_search = evaluate(search_dialog, "standardButton(Dialog.Cancel)")
                QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, cancel_search.mapToScene(cancel_search.boundingRect().center()).toPoint())
                wait(lambda: not search_dialog.property("visible"))
                assert controller.plannedSearch["entryId"] == ""
                assert len(store.planned_entries()) == 2
            assert searched == {first, second}
            open_confirmation()
            confirm = evaluate(remove_dialog, "standardButton(Dialog.Ok)")
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, confirm.mapToScene(confirm.boundingRect().center()).toPoint())
            wait(lambda: len(controller.plannedEntries) == 1)
            assert selected not in {entry["id"] for entry in store.planned_entries()}
            assert store.planned_entries()[0]["id"] == ({first, second} - {selected}).pop()
            store.remove_planned(store.planned_entries()[0]["id"])
            controller._request_items()
            wait(lambda: not controller.plannedEntries)
        # Exercise real Refresh imports button and stable Planned anchors in both modes.
        import json
        import uuid
        from test_planned_imports import sample
        with store.conn:
            store.conn.executemany("INSERT INTO planned_anime(id,title) VALUES(?,?)",
                                   [(str(uuid.uuid4()), f"Existing {i}") for i in range(400)])
        controller._request_items()
        wait(lambda: len(controller.plannedEntries) == 400 and not controller.importsBusy)
        grid_view = view.findChild(QObject, "plannedEntriesView")
        refresh = view.findChild(QObject, "refreshImportsButton")
        for grid in (True, False):
            view.setProperty("gridMode", grid)
            QTest.qWait(100)
            evaluate(grid_view, "positionViewAtIndex(200, GridView.Beginning); forceLayout(); true")
            QTest.qWait(50)
            before = evaluate(grid_view, "indexAt(1, contentY + 2)")
            anchor = controller.plannedEntries[before]["id"]
            capture = sample(text="Imported while open 🌸")
            inbox = database.APP_DIR / "imports" / "inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            (inbox / (capture["capture_id"] + ".json")).write_text(json.dumps(capture), encoding="utf-8")
            previous_count = len(controller.plannedEntries)
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, refresh.mapToScene(refresh.boundingRect().center()).toPoint())
            wait(lambda: not controller.importsBusy and len(controller.plannedEntries) == previous_count + 1)
            QTest.qWait(50)
            first = evaluate(grid_view, "indexAt(1, contentY + 2)")
            columns = evaluate(grid_view, "Math.max(1, Math.floor(width / cellWidth))")
            assert anchor in [entry["id"] for entry in controller.plannedEntries[first:first + columns]]
            previous_delegate = evaluate(grid_view, f"itemAtIndex({first})")
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, refresh.mapToScene(refresh.boundingRect().center()).toPoint())
            wait(lambda: not controller.importsBusy and controller.captureImportMessage == "No new imports.")
            assert evaluate(grid_view, f"itemAtIndex({first})") == previous_delegate
        controller.shutdown()
        if controller._capture_import_thread:
            controller._capture_import_thread.join(5)
        window.close()
        store.conn.close()
    print("Grid/list Cancel and Confirm passed")


if __name__ == "__main__" and "--scenario" in sys.argv:
    scenario()
