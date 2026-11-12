"""Library navigation/title search regressions; isolated DB and mocked browser/worker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QUrl, QUrlQuery

# The direct-script QML scenario must also work in a fresh checkout, without
# relying on VS Code or a caller's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_favorites as fixtures
import indexer.database as database
from indexer.database import IndexStore
from ui.controller import AppController


class LibraryUxTests(unittest.TestCase):
    setUpClass = classmethod(fixtures.FavoritesTests.setUpClass.__func__)
    setUp = fixtures.FavoritesTests.setUp
    tearDown = fixtures.FavoritesTests.tearDown
    seed = fixtures.FavoritesTests.seed

    def test_return_to_empty_library_from_each_global_view(self):
        c = self.controller
        c._run_capture_import = Mock()
        c.service.store.conn.executescript("DELETE FROM entries; DELETE FROM drives; DELETE FROM folder_sources;")
        c._selected_drive = ""; c._refresh_drives()
        for select in (c.selectFavorites, c.selectToWatch, c.selectPlanned):
            select(); c.search("global query"); c.selectLibrary()
            self.assertEqual(c.currentView, "source")
            self.assertEqual(c.selectedDrive, "")
            self.assertEqual(c.drives, [])
            self.assertEqual(c.searchTerm, "")

    def test_return_preserves_selected_source_path_and_separate_searches(self):
        c = self.controller; c._run_capture_import = Mock()
        c.selectDrive(self.source); c.openFolder("One"); c.search("source query")
        for select in (c.selectFavorites, c.selectToWatch, c.selectPlanned):
            select(); c.search("global query"); c.selectLibrary()
            self.assertEqual((c.selectedDrive, c.currentPath, c.searchTerm), (self.source, "One", "source query"))
            select(); self.assertEqual(c.searchTerm, "global query"); c.selectLibrary()
        c.selectDrive("AABBCCDD")
        self.assertEqual((c.currentView, c.currentPath, c.searchTerm), ("source", "", ""))

    def web(self, path="One/Anime", source=None, supplied="Anime"):
        with patch("ui.controller.QDesktopServices.openUrl", return_value=True) as browser:
            self.controller.searchFolderOnWeb(supplied, source or self.source, path)
        url = browser.call_args.args[0]
        self.assertEqual(url.host(), "www.google.com")
        self.assertEqual(url.path(), "/search")
        return QUrlQuery(url).queryItemValue("q", QUrl.FullyDecoded), bytes(url.toEncoded())

    def test_linked_search_fallback_relink_unlink_and_unicode(self):
        s = self.store
        s.conn.execute("UPDATE entries SET name='BlueStacks' WHERE drive_id=? AND path='One/Anime'", (self.source,)); s.conn.commit()
        self.assertEqual(self.web()[0], "BlueStacks")
        s.save_cover_result("bluestacks", 99, "auto", "unused", "found", title="Unrelated automatic title")
        for missing in (None, "", " \t\n"):
            s.save_manual_link(self.source, "One/Anime", 1, "cover", "unused", title=missing)
            self.assertEqual(self.web()[0], "BlueStacks")  # Explicit missing title wins over auto.
        s.save_manual_link(self.source, "One/Anime", 1, "cover", "unused", title="Frieren: Beyond Journey's End")
        self.assertEqual(self.web()[0], "Frieren: Beyond Journey's End")
        title = "葬送のフリーレン & Élite + #2?"
        s.save_manual_link(self.source, "One/Anime", 2, "new cover", "new cache", title=title)
        decoded, encoded = self.web()
        self.assertEqual(decoded, title)
        self.assertIn(b'%26', encoded); self.assertIn(b'%23', encoded)
        self.assertNotIn(b' ', encoded)
        self.controller.unlinkAniListEntry(self.source, "One/Anime")
        self.assertEqual(self.web()[0], "BlueStacks")
        s.save_manual_link(self.source, "One/Anime", 3, "cover", "unused", title="New association")
        self.assertEqual(self.web()[0], "New association")

    def test_automatic_metadata_and_same_named_folder_identity(self):
        self.store.save_cover_result("anime", 7, "cover", "unused", "found", title="Automatic title")
        self.assertEqual(self.web()[0], "Automatic title")
        self.store.save_manual_link(self.source, "One/Anime", 8, "cover", "unused", title="Explicit title")
        self.assertEqual(self.web()[0], "Explicit title")
        self.assertEqual(self.web(path="Two/Anime")[0], "Automatic title")
        self.assertEqual(self.web(path="Anime", source="AABBCCDD")[0], "Automatic title")
        for status in ("failed", "blocked", "no_match"):
            self.store.save_cover_result("anime", 7, "cover", "unused", status, title="Must not use")
            self.assertEqual(self.web(path="Two/Anime")[0], "Anime")

    def test_old_schema_migration_preserves_all_existing_table_data(self):
        self.store.save_manual_link(self.source, "One/Anime", 8, "cover", "unused")
        self.store.save_cover_result("anime", 9, "auto", "other", "found")
        self.store.set_favorite(self.source, "One/Anime", True)
        self.store.set_to_watch(self.source, "One/Anime", True)
        entry, _ = self.store.save_planned_title("Planned 原題")
        self.store.link_planned(entry, 22, "Planned linked", "url", "cache")
        self.store.import_planned_capture(dict(version=1, type="capture", capture_id="12345678-1234-4234-8234-123456789abc", text="Captured", captured_at="2026-11-07T12:34:56Z"))
        self.store.rename_source(self.source, "Custom label")
        self.store.exclude_folder(self.source, "Two/Anime")
        for table in ("anime_cover_cache", "folder_anilist_links"):
            self.store.conn.execute(f"ALTER TABLE {table} DROP COLUMN anilist_title")
        self.store.conn.commit()
        tables = [r[0] for r in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        columns = {t: [r[1] for r in self.store.conn.execute(f"PRAGMA table_info({t})")] for t in tables}
        before = {t: self.store.conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
        for _ in range(2):
            reopened = IndexStore()
            try:
                for table in tables:
                    self.assertEqual(reopened.conn.execute(f"SELECT {','.join(columns[table])} FROM {table}").fetchall(), before[table])
                self.assertIsNone(reopened.linked_anilist_title(self.source, "One/Anime", "anime"))
            finally: reopened.conn.close()
        self.store.save_manual_link(self.source, "One/Anime", 8, "cover", "unused", title="Persisted title")
        reopened = IndexStore()
        try: self.assertEqual(reopened.linked_anilist_title(self.source, "ONE\\ANIME", "anime"), "Persisted title")
        finally: reopened.conn.close()

    def test_qml_heading_keyboard_navigation_and_grid_list_search_wiring(self):
        root = Path(__file__).resolve().parents[1]
        qml = (root / "ui/qml/Main.qml").read_text(encoding="utf-8")
        self.assertEqual(qml.count("controller.searchFolderOnWeb(item.name, item.sourceId, item.path)"), 2)
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--qml"],
            cwd=root, capture_output=True, text=True, timeout=30,
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1", PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Library keyboard navigation passed", result.stdout)


def qml_scenario():
    from PySide6.QtCore import QObject, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([])
    case = LibraryUxTests(); case.setUp()
    c = case.controller; c._run_capture_import = Mock()
    engine = QQmlApplicationEngine(); engine.rootContext().setContextProperty("controller", c)
    engine.load(str(Path("ui/qml/Main.qml").resolve()))
    try:
        assert engine.rootObjects()
        window = engine.rootObjects()[0]
        window.setProperty("gridView", False)
        heading = window.findChild(QObject, "libraryNavigation"); assert heading
        c.selectFavorites(); heading.forceActiveFocus()
        QTest.keyClick(window, Qt.Key_Space); app.processEvents()
        assert c.currentView == "source"
        assert window.property("gridView") is False
        def evaluate(obj, expression):
            query = QQmlExpression(engine.contextForObject(obj), obj, expression)
            value, _ = query.evaluate()
            assert not query.hasError(), query.error().toString()
            return value
        case.store.save_manual_link(case.source, "One/Anime", 7, "cover", "unused", title="Frieren 日本語")
        c.selectDrive("AABBCCDD"); c.selectFavorites()
        for grid in (True, False):
            window.setProperty("gridView", grid)
            c._items = [dict(name="BlueStacks", path="One/Anime", sourceId=case.source, isFolder=True,
                manualId=7, coverStatus="manual", coverKey="anime", coverUrl="", size="", isFavorite=True, isToWatch=False)]
            c.itemsChanged.emit(); QTest.qWait(160)
            delegate = evaluate(window, ("mediaGrid" if grid else "mediaList") + ".itemAtIndex(0)")
            assert delegate
            menu = delegate.findChild(QObject, "libraryCardMenu") if grid else evaluate(delegate, "listFolderMenu")
            with patch("ui.controller.QDesktopServices.openUrl", return_value=True) as browser:
                evaluate(menu, "itemAt(2).triggered(); true")
                assert browser.call_count == 1
                assert QUrlQuery(browser.call_args.args[0]).queryItemValue("q", QUrl.FullyDecoded) == "Frieren 日本語"
        window.close()
    finally:
        engine.rootContext().setContextProperty("controller", None)
        case.tearDown()
    print("Library keyboard navigation passed")


if __name__ == "__main__":
    if "--qml" in sys.argv: qml_scenario()
    else: unittest.main()
