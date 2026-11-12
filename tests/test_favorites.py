"""Global favorites with isolated SQLite, no drive polling or API worker."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication
import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from ui.controller import AppController


class FavoritesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        root = Path(__file__).parent / ".favorites-test-runtime"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.original = database.APP_DIR, database.DB_PATH
        database.APP_DIR = Path(self.temp.name)
        database.DB_PATH = database.APP_DIR / "index.db"
        self.store = IndexStore()
        self.source, _, _, _ = self.store.register_folder_source(str(database.APP_DIR / "missing-library"))
        self.seed(self.source, ["One/Anime", "Two/Anime"])
        self.seed("AABBCCDD", ["Anime"])
        self.backend = IndexerService(self.store)
        with patch("ui.controller.IndexerService", return_value=self.backend), patch("ui.controller.CoverService"), patch.object(AppController, "_request_items"):
            self.controller = AppController()
        self.controller._request_items = Mock()

    def tearDown(self):
        self.controller.shutdown()
        self.store.conn.close()
        database.APP_DIR, database.DB_PATH = self.original
        self.temp.cleanup()

    def seed(self, source, paths):
        self.store.replace_drive(source, "Library" if source.startswith("folder:") else "External", "E:", [
            (source, path, "", path.split("/")[-1], 1, None, None) for path in paths
        ])

    def query(self, view="favorites", term=""):
        results = []
        callback = lambda number, items: results.extend(items)
        self.controller._queryFinished.connect(callback)
        try:
            self.controller._query_items(99, self.source, "", term, view, self.controller.drives)
        finally:
            self.controller._queryFinished.disconnect(callback)
        return results

    def test_persistence_normalization_and_same_named_folders(self):
        for source, path in ((self.source, "ONE\\ANIME\\"), (self.source, "Two/Anime"), ("AABBCCDD", "Anime")):
            self.store.set_favorite(source, path, True)
        self.store.set_favorite(self.source, "one/anime", True)
        reopened = IndexStore()
        try:
            self.assertEqual(len(reopened.favorite_keys()), 3)
            self.assertEqual(len(reopened.favorite_entries()), 3)
        finally:
            reopened.conn.close()
        items = self.query()
        self.assertEqual(len({item["folderKey"] for item in items}), 3)
        self.assertTrue(all(item["isFavorite"] for item in items))

    def test_disconnected_and_unavailable_sources_display_status(self):
        self.store.set_favorite(self.source, "One/Anime", True)
        self.store.set_favorite("AABBCCDD", "Anime", True)
        items = {item["sourceId"]: item for item in self.query()}
        self.assertEqual(items[self.source]["sourceStatus"], "Unavailable")
        self.assertEqual(items["AABBCCDD"]["sourceStatus"], "Disconnected")
        self.assertEqual(items["AABBCCDD"]["sourceLabel"], "External")

    def test_reindex_missing_entries_exclusions_and_source_removal(self):
        self.store.set_favorite(self.source, "One/Anime", True)
        self.seed(self.source, [])
        self.assertEqual(self.store.favorite_entries(), [])
        self.assertEqual(len(self.store.favorite_keys()), 1)
        self.seed(self.source, ["One/Anime"])
        self.assertEqual(len(self.store.favorite_entries()), 1)
        self.backend.exclude_folder(self.source, "One/Anime")
        self.assertEqual(self.store.favorite_entries(), [])
        self.assertEqual(len(self.store.favorite_keys()), 1)
        self.backend.remove_source(self.source)
        self.assertEqual(self.store.favorite_keys(), set())
        self.assertTrue(self.store.drives())  # retained indexes aren't registrations

    def test_excluded_physical_sources_do_not_appear(self):
        self.store.set_favorite("AABBCCDD", "Anime", True)
        self.store.exclude_drive("AABBCCDD")
        self.assertEqual(self.store.favorite_entries(), [])
        self.assertEqual(len(self.store.favorite_keys()), 1)
        self.backend.remove_source("AABBCCDD")
        self.assertEqual(self.store.favorite_keys(), set())

    def test_cover_priority_and_same_names_from_different_sources(self):
        automatic, manual = database.APP_DIR / "auto.jpg", database.APP_DIR / "manual.jpg"
        for path in (automatic, manual):
            path.write_bytes(b"\xff\xd8\xff" + b"x" * 1100)
        self.store.save_cover_result("anime", 1, "auto", str(automatic), "found")
        self.store.save_manual_link(self.source, "One/Anime", 2, "manual", str(manual))
        for source, path in ((self.source, "One/Anime"), ("AABBCCDD", "Anime")):
            self.store.set_favorite(source, path, True)
        items = {item["sourceId"]: item for item in self.query()}
        self.assertEqual(items[self.source]["manualId"], 2)
        self.assertEqual(items[self.source]["coverUrl"], manual.as_uri())
        self.assertEqual(items["AABBCCDD"]["coverUrl"], automatic.as_uri())
        manual.unlink()
        self.assertEqual({item["sourceId"]: item for item in self.query()}[self.source]["coverUrl"], "")

    def test_view_transitions_search_and_refresh_do_not_leave_favorites(self):
        c = self.controller
        c.selectDrive(self.source)
        c.openFolder("One")
        c.search("source query")
        c.selectFavorites()
        c.search("favorites query")
        self.assertEqual(c.selectedDrive, self.source)
        self.assertEqual(c.breadcrumbs, [])
        self.assertFalse(c.canNavigateUp)
        c._selected_drive = ""  # even with no selected source, refresh stays global
        c._refresh_drives()
        self.assertEqual(c.currentView, "favorites")
        c._selected_drive = self.source
        c.selectDrive(self.source)
        self.assertEqual(c.currentView, "source")
        self.assertEqual(c.searchTerm, "source query")
        self.assertEqual(c.currentPath, "One")
        c.selectFavorites()
        self.assertEqual(c.searchTerm, "favorites query")

    def test_immediate_unfavorite_global_search_and_stale_results(self):
        c = self.controller
        c.selectFavorites()
        c.setFavorite(self.source, "One/Anime", True)
        self.assertEqual(len(self.query(term="Anime")), 1)
        self.assertEqual(self.query(term="Missing"), [])
        c._request_items.reset_mock()
        c.setFavorite(self.source, "One/Anime", False)
        c._request_items.assert_called_once()
        self.assertEqual(self.query(), [])
        c._query_number = 9
        c._items = []
        c._apply_query(8, [{"stale": True}])
        self.assertEqual(c.items, [])

    def test_card_actions_target_originating_source(self):
        c = self.controller
        c.selectDrive(self.source)
        c.selectFavorites()
        with patch.object(self.backend, "source_root", return_value=None) as root:
            c.openCardInExplorer("AABBCCDD", "Anime")
            root.assert_called_once_with("AABBCCDD")
        with patch.object(self.backend, "exclude_folder", return_value=True) as exclude:
            c.excludeCardFolder("AABBCCDD", "Anime")
            exclude.assert_called_once_with("AABBCCDD", "Anime")
        c.openCardItem("AABBCCDD", "Anime", True)
        self.assertEqual(c.selectedDrive, "AABBCCDD")
        self.assertEqual(c.currentView, "source")
        self.assertEqual(c.currentPath, "Anime")

    def test_to_watch_independence_normalized_identity_and_persistence(self):
        self.store.set_favorite(self.source, "One/Anime", True)
        for source, path in ((self.source, "ONE\\ANIME\\"), (self.source, "Two/Anime"), ("AABBCCDD", "Anime")):
            self.store.set_to_watch(source, path, True)
        self.store.set_to_watch(self.source, "one/anime", True)
        self.assertEqual(len(self.store.to_watch_keys()), 3)
        self.assertEqual(len(self.store.favorite_keys()), 1)
        reopened = IndexStore()
        try:
            self.assertEqual(len(reopened.to_watch_entries()), 3)
        finally:
            reopened.conn.close()
        items = self.query("to_watch")
        self.assertTrue(all(item["isToWatch"] for item in items))
        self.assertEqual(sum(item["isFavorite"] for item in items), 1)
        self.assertEqual(len({item["folderKey"] for item in items}), 3)
        self.store.set_to_watch(self.source, "One/Anime", False)
        self.assertEqual(len(self.store.favorite_keys()), 1)

    def test_to_watch_reindex_disconnection_exclusion_and_source_cleanup(self):
        self.store.set_to_watch(self.source, "One/Anime", True)
        self.store.set_to_watch("AABBCCDD", "Anime", True)
        self.seed(self.source, [])
        self.assertEqual(len(self.store.to_watch_entries()), 1)
        self.assertEqual(len(self.store.to_watch_keys()), 2)
        self.seed(self.source, ["One/Anime"])
        items = {item["sourceId"]: item for item in self.query("to_watch")}
        self.assertEqual(items[self.source]["sourceStatus"], "Unavailable")
        self.assertEqual(items["AABBCCDD"]["sourceStatus"], "Disconnected")
        self.backend.exclude_folder(self.source, "One/Anime")
        self.assertEqual(len(self.store.to_watch_entries()), 1)
        self.backend.remove_source(self.source)
        self.backend.remove_source("AABBCCDD")
        self.assertEqual(self.store.to_watch_keys(), set())

    def test_to_watch_search_transitions_unmark_and_origin_actions(self):
        c = self.controller
        c.selectDrive(self.source)
        c.search("source")
        c.selectFavorites()
        c.search("favorites")
        c.selectToWatch()
        c.search("Anime")
        c.setToWatch("AABBCCDD", "Anime", True)
        self.assertEqual(len(self.query("to_watch", "Anime")), 1)
        self.assertEqual(self.query("to_watch", "missing"), [])
        self.assertEqual(c.breadcrumbs, [])
        c._request_items.reset_mock()
        c.setToWatch("AABBCCDD", "Anime", False)
        c._request_items.assert_called_once()
        self.assertEqual(self.query("to_watch"), [])
        c._refresh_drives()
        self.assertEqual(c.currentView, "to_watch")
        c.selectFavorites()
        self.assertEqual(c.searchTerm, "favorites")
        c.selectToWatch()
        self.assertEqual(c.searchTerm, "Anime")
        c.selectDrive(self.source)
        self.assertEqual(c.searchTerm, "source")
        c.selectToWatch()
        with patch.object(self.backend, "source_root", return_value=None) as root:
            c.openCardInExplorer("AABBCCDD", "Anime")
            root.assert_called_once_with("AABBCCDD")
        c.openCardItem("AABBCCDD", "Anime", True)
        self.assertEqual(c.currentView, "source")
        self.assertEqual(c.selectedDrive, "AABBCCDD")

    def test_to_watch_reuses_manual_and_automatic_covers(self):
        automatic, manual = database.APP_DIR / "auto.jpg", database.APP_DIR / "manual.jpg"
        for path in (automatic, manual):
            path.write_bytes(b"\xff\xd8\xff" + b"x" * 1100)
        self.store.save_cover_result("anime", 1, "auto", str(automatic), "found")
        self.store.save_manual_link(self.source, "One/Anime", 2, "manual", str(manual))
        for source, path in ((self.source, "One/Anime"), ("AABBCCDD", "Anime")):
            self.store.set_to_watch(source, path, True)
        items = {item["sourceId"]: item for item in self.query("to_watch")}
        self.assertEqual(items[self.source]["coverUrl"], manual.as_uri())
        self.assertEqual(items["AABBCCDD"]["coverUrl"], automatic.as_uri())
