"""Focused coverage for manually selected library folders."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from ui.controller import AppController


class FolderSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(__file__).parent / ".folder-source-test-runtime"
        test_root.mkdir(exist_ok=True)
        self.temp_path = Path(tempfile.mkdtemp(prefix="folder-", dir=test_root))
        self.original_app_dir = database.APP_DIR
        self.original_db_path = database.DB_PATH
        database.APP_DIR = self.temp_path / "app"
        database.DB_PATH = database.APP_DIR / "index.db"
        self.store = IndexStore()
        self.service = IndexerService(self.store)

    def tearDown(self) -> None:
        self.store.conn.close()
        database.APP_DIR = self.original_app_dir
        database.DB_PATH = self.original_db_path
        shutil.rmtree(self.temp_path, ignore_errors=True)

    def wait_for_index(self, source_id: str) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = self.service.process_events()
            if any(event[0] == "complete" and event[1] == source_id for event in events):
                return
            time.sleep(0.02)
        self.fail(f"Timed out indexing {source_id}")

    def test_folder_registration_persists_and_prevents_path_duplicates(self) -> None:
        source_path = self.temp_path / "Anime PC"
        source_path.mkdir()
        # Register directly here so the persistence assertion is independent of
        # the deliberately asynchronous first scan covered by the next test.
        source_id, label, stored_path, added = self.store.register_folder_source(str(source_path))
        self.assertTrue(added)
        self.assertEqual(label, "Anime PC")
        duplicate = self.store.register_folder_source(str(source_path) + "\\")
        self.assertEqual(duplicate[0], source_id)
        self.assertFalse(duplicate[3])
        case_duplicate = self.store.register_folder_source(str(source_path).upper())
        self.assertEqual(case_duplicate[0], source_id)
        self.assertFalse(case_duplicate[3])

        reopened = IndexStore()
        try:
            rows = reopened.folder_sources()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][:3], (source_id, "Anime PC", stored_path))
            self.assertEqual(rows[0][3:], (None, 0))
        finally:
            reopened.conn.close()

    def test_file_url_converts_to_a_native_windows_path(self) -> None:
        path = AppController._local_folder_path("file:///E:/Anime%20Library/%C3%89t%C3%A9")
        self.assertEqual(path, "E:\\Anime Library\\Été")
        self.assertEqual(AppController._local_folder_path(""), "")
        self.assertEqual(AppController._local_folder_path("https://example.test/anime"), "")

    def test_indexes_only_the_selected_folder_and_can_reindex(self) -> None:
        library = self.temp_path / "library"
        (library / "Dandadan").mkdir(parents=True)
        (library / "Dandadan" / "episode.mkv").write_bytes(b"episode")
        outside = self.temp_path / "outside"
        outside.mkdir()
        (outside / "ignored.mkv").write_bytes(b"outside")

        source_id, _, _, _, _ = self.service.add_folder_source(str(library))
        self.wait_for_index(source_id)
        first_paths = {row[0] for row in self.store.root_entries(source_id)}
        self.assertEqual(first_paths, {"Dandadan"})
        self.assertEqual(self.store.drives()[0][4], 2)

        (library / "Bocchi").mkdir()
        self.assertTrue(self.service.reindex(source_id))
        self.wait_for_index(source_id)
        self.assertEqual({row[0] for row in self.store.root_entries(source_id)}, {"Dandadan", "Bocchi"})
        self.assertEqual(self.store.drives()[0][4], 3)

    def test_unavailable_folder_remains_registered_and_keeps_its_index(self) -> None:
        library = self.temp_path / "portable-library"
        library.mkdir()
        (library / "show.mkv").write_bytes(b"show")
        source_id, _, _, _, _ = self.service.add_folder_source(str(library))
        self.wait_for_index(source_id)
        unavailable_path = self.temp_path / "portable-library-offline"
        library.rename(unavailable_path)

        self.service.refresh_folder_sources()
        self.assertIsNone(self.service.source_root(source_id))
        self.assertFalse(self.service.reindex(source_id))
        self.assertEqual(self.store.drives()[0][0], source_id)
        self.assertEqual(self.store.drives()[0][4], 1)
        self.assertEqual(self.store.folder_sources()[0][0], source_id)

    def test_existing_external_source_reindex_behavior_is_unchanged(self) -> None:
        external_root = self.temp_path / "external"
        external_root.mkdir()
        (external_root / "anime.mkv").write_bytes(b"anime")
        self.service.connected["EXTERNAL"] = ("E:", "External", str(external_root))
        self.assertTrue(self.service.reindex("EXTERNAL"))
        self.wait_for_index("EXTERNAL")
        self.assertEqual(self.store.drives()[0][0], "EXTERNAL")
        self.assertEqual(self.store.drives()[0][4], 1)


if __name__ == "__main__":
    unittest.main()
