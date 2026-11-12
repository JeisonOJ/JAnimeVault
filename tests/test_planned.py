"""Stage 1 Planned storage/navigation tests: isolated DB and no API worker."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication
import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from ui.controller import AppController


class PlannedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        root = Path(__file__).parent / ".planned-test-runtime"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.original = database.APP_DIR, database.DB_PATH
        database.APP_DIR = Path(self.temp.name)
        database.DB_PATH = database.APP_DIR / "index.db"

    def tearDown(self):
        database.APP_DIR, database.DB_PATH = self.original
        self.temp.cleanup()

    def test_additive_migration_preserves_existing_rows_and_is_repeatable(self):
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute("CREATE TABLE drives(id TEXT PRIMARY KEY,label TEXT,letter TEXT,last_indexed TEXT,file_count INTEGER)")
        conn.execute("INSERT INTO drives VALUES('EXTERNAL','External','E:',NULL,12)")
        conn.commit()
        conn.close()
        for _ in range(2):
            store = IndexStore()
            try:
                self.assertEqual(store.drives()[0][0], "EXTERNAL")
                self.assertEqual(store.planned_entries(), [])
            finally:
                store.conn.close()

    def test_crud_persistence_unicode_and_search(self):
        store = IndexStore()
        try:
            entry_id, duplicate = store.save_planned_title("  勇者 ÉTÉ 🌸  ")
            self.assertFalse(duplicate)
            row = store.planned_entries("été")[0]
            self.assertEqual(row["title"], "勇者 ÉTÉ 🌸")
            self.assertIsNone(row["anilistId"])
            self.assertTrue(row["createdAt"])
            self.assertEqual(store.save_planned_title("New title", entry_id)[0], entry_id)
            self.assertEqual(store.planned_entries("勇者"), [])
        finally:
            store.conn.close()
        store = IndexStore()
        try:
            self.assertEqual(store.planned_entries()[0]["id"], entry_id)
            store.remove_planned(entry_id)
            self.assertEqual(store.planned_entries(), [])
            with self.assertRaises(ValueError):
                store.save_planned_title("Missing", entry_id)
            with self.assertRaises(ValueError):
                store.save_planned_title("   ")
        finally:
            store.conn.close()

    def test_duplicates_remain_distinct_and_edit_does_not_overwrite_metadata(self):
        store = IndexStore()
        try:
            first, _ = store.save_planned_title("Example Anime")
            second, duplicate = store.save_planned_title("EXAMPLE  Anime")
            self.assertTrue(duplicate)
            self.assertNotEqual(first, second)
            self.assertEqual(len(store.planned_entries()), 2)
            store.conn.execute("UPDATE planned_anime SET anilist_id=123,anilist_title='Official title' WHERE id=?", (first,))
            store.conn.commit()
            store.save_planned_title("Edited manual title", first)
            row = next(row for row in store.planned_entries() if row["id"] == first)
            self.assertEqual(row["anilistId"], 123)
            self.assertEqual(row["anilistTitle"], "Official title")
        finally:
            store.conn.close()

    def test_source_lifecycle_does_not_touch_planned(self):
        store = IndexStore()
        try:
            entry_id, _ = store.save_planned_title("Future anime")
            source, label, path, _ = store.register_folder_source(str(database.APP_DIR / "library"))
            store.replace_drive(source, label, path, [])
            IndexerService(store).remove_source(source)
            store.replace_drive("EXTERNAL", "External", "E:", [])
            IndexerService(store).remove_source("EXTERNAL")
            self.assertEqual(store.planned_entries()[0]["id"], entry_id)
        finally:
            store.conn.close()

    def test_controller_navigation_crud_warning_and_no_cover_jobs(self):
        store = IndexStore()
        try:
            with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService"), patch.object(AppController, "_request_items"):
                controller = AppController()
            controller._request_items = Mock()
            controller.selectPlanned()
            controller.search("Future")
            controller.savePlannedTitle("", "Future anime")
            controller.savePlannedTitle("", "Future anime")
            self.assertIn("both were kept", controller.status)
            entries = store.planned_entries()
            self.assertEqual(len(entries), 2)
            controller._query_number = 8
            controller._query_items(8, "", "", "Future", "planned")
            self.assertEqual(len(controller.plannedEntries), 2)
            controller._apply_planned_query(7, [])
            self.assertEqual(len(controller.plannedEntries), 2)
            controller.requestCoversForRange(0, 10)
            controller.cover_service.enqueue.assert_not_called()
            controller.cover_service.link_folder.assert_not_called()
            controller.selectFavorites()
            controller.search("favorites")
            controller.selectPlanned()
            self.assertEqual(controller.searchTerm, "Future")
            controller.removePlanned(entries[0]["id"])
            self.assertEqual(len(store.planned_entries()), 1)
            controller.shutdown()
        finally:
            store.conn.close()
