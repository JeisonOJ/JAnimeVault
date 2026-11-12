"""Focused coverage for persisted source-relative folder exclusions."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from ui.controller import AppController


class _FolderOpenProbe:
    def __init__(self, root: str | None) -> None:
        self._selected_drive = "SOURCE"
        self.service = type("Service", (), {"source_root": lambda _, __: root})()
        self.status = ""

    def _set_status(self, status: str, severity=None) -> None:
        self.status = status


class FolderExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(__file__).parent / ".folder-exclusion-test-runtime"
        test_root.mkdir(exist_ok=True)
        self.temp_path = Path(tempfile.mkdtemp(prefix="exclusion-", dir=test_root))
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
            if any(event[0] == "complete" and event[1] == source_id for event in self.service.process_events()):
                return
            time.sleep(0.02)
        self.fail(f"Timed out indexing {source_id}")

    def test_exclusion_removes_only_its_subtree_persists_and_survives_reindex(self) -> None:
        library = self.temp_path / "library"
        (library / "Anime" / "Season 1").mkdir(parents=True)
        (library / "Anime" / "Season 1" / "episode.mkv").write_bytes(b"episode")
        (library / "Anime Movies").mkdir()
        (library / "Anime Movies" / "movie.mkv").write_bytes(b"movie")
        source_id, _, _, _, _ = self.service.add_folder_source(str(library))
        self.wait_for_index(source_id)

        self.assertTrue(self.service.exclude_folder(source_id, "Anime"))
        self.assertEqual({row[0] for row in self.store.root_entries(source_id)}, {"Anime Movies"})
        self.assertTrue((library / "Anime" / "Season 1" / "episode.mkv").exists())

        reopened = IndexStore()
        try:
            self.assertEqual(reopened.folder_exclusion_paths(source_id), ["anime"])
        finally:
            reopened.conn.close()

        self.assertTrue(self.service.reindex(source_id))
        self.wait_for_index(source_id)
        self.assertEqual({row[0] for row in self.store.root_entries(source_id)}, {"Anime Movies"})

        self.assertTrue(self.service.restore_folder(source_id, "Anime"))
        self.assertTrue(self.service.reindex(source_id))
        self.wait_for_index(source_id)
        self.assertEqual({row[0] for row in self.store.root_entries(source_id)}, {"Anime", "Anime Movies"})

    def test_exclusions_follow_a_stable_external_source_when_its_root_changes(self) -> None:
        first_root = self.temp_path / "first-mount"
        second_root = self.temp_path / "second-mount"
        for root in (first_root, second_root):
            (root / "Ignore Me").mkdir(parents=True)
            (root / "Ignore Me" / "episode.mkv").write_bytes(b"episode")
            (root / "Keep Me").mkdir()
        source_id = "STABLE-DRIVE"
        self.service.connected[source_id] = ("E:", "External", str(first_root))
        self.assertTrue(self.service.reindex(source_id))
        self.wait_for_index(source_id)
        self.assertTrue(self.service.exclude_folder(source_id, "Ignore Me"))

        self.service.connected[source_id] = ("F:", "External", str(second_root))
        self.assertTrue(self.service.reindex(source_id))
        self.wait_for_index(source_id)
        self.assertEqual({row[0] for row in self.store.root_entries(source_id)}, {"Keep Me"})

    def test_builtin_system_folders_are_skipped_without_affecting_normal_folders(self) -> None:
        library = self.temp_path / "library"
        for name in ("$RECYCLE.BIN", "System Volume Information", "Normal Anime"):
            (library / name).mkdir(parents=True)
            (library / name / "file.mkv").write_bytes(b"file")
        source_id, _, _, _, _ = self.service.add_folder_source(str(library))
        self.wait_for_index(source_id)
        self.assertEqual({row[0] for row in self.store.root_entries(source_id)}, {"Normal Anime"})

    def test_web_search_url_encodes_folder_names(self) -> None:
        url = AppController.web_search_url("Re:Zero & Élite")
        encoded = bytes(url.toEncoded())
        self.assertIn(b"q=Re:Zero%20%26%20", encoded)
        self.assertIn(b"%C3%89lite", encoded)

    def test_opening_anime_folder_uses_the_current_source_and_handles_unavailable(self) -> None:
        library = self.temp_path / "library"
        anime = library / "Anime"
        anime.mkdir(parents=True)
        available = _FolderOpenProbe(str(library))
        with patch("ui.controller.os.startfile", create=True) as open_in_explorer:
            AppController.openFolderInExplorer(available, "Anime")
            open_in_explorer.assert_called_once_with(str(anime))

        unavailable = _FolderOpenProbe(None)
        with patch("ui.controller.os.startfile", create=True) as open_in_explorer:
            AppController.openFolderInExplorer(unavailable, "Anime")
            open_in_explorer.assert_not_called()
        self.assertIn("available", unavailable.status)


if __name__ == "__main__":
    unittest.main()
