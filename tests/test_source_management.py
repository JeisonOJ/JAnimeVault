"""Focused persistence and service coverage for library-source management."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from ui.controller import AppController


class _ControllerProbe:
    def __init__(self, root: str | None) -> None:
        self.service = type("Service", (), {"source_root": lambda _, __: root})()
        self.status = ""

    def _set_status(self, status: str, severity=None) -> None:
        self.status = status


class _SidebarProbe:
    """Exercise real sidebar/removal methods without Qt workers or drive polling."""

    _refresh_drives = AppController._refresh_drives
    _refresh_exclusions = AppController._refresh_exclusions
    removeSource = AppController.removeSource
    currentView = "source"

    def __init__(self, service: IndexerService, selected: str) -> None:
        self.service = service
        self.cover_service = Mock()
        self._manual_status_pending = set()
        self._selected_drive = selected
        self._current_path = ""
        self._search_term = ""
        self._items = []
        self._drives = []
        self._archive_source = ""
        self.archiveChanged = Mock()
        self.drivesChanged = Mock()
        self.exclusionsChanged = Mock()
        self.selectionChanged = Mock()
        self.itemsChanged = Mock()
        self.selectDrive = Mock()
        self._set_status = Mock()


class SourceManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(__file__).parent / ".source-management-test-runtime"
        test_root.mkdir(exist_ok=True)
        self.temp_path = Path(tempfile.mkdtemp(prefix="source-", dir=test_root))
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

    def test_manual_removal_stays_hidden_after_refresh_and_reregistration(self) -> None:
        library = self.temp_path / "anime"
        library.mkdir()
        physical_file = library / "show.mkv"
        physical_file.write_bytes(b"show")
        source_id, label, path, _ = self.store.register_folder_source(str(library))
        entries = [(source_id, "show.mkv", "", "show.mkv", 0, 4, None)]
        self.store.replace_drive(source_id, label, path, entries)
        sidebar = _SidebarProbe(self.service, source_id)
        sidebar._refresh_drives()
        self.assertEqual([item["kind"] for item in sidebar._drives], ["folder"])

        sidebar.removeSource(source_id)
        for _ in range(3):
            sidebar._refresh_drives()
            self.assertEqual(sidebar._drives, [])
        self.assertEqual(self.store.folder_sources(), [])
        self.assertEqual(self.store.drives()[0][0], source_id)
        self.assertEqual(len(self.store.root_entries(source_id)), 1)
        self.assertEqual(physical_file.read_bytes(), b"show")

        registered_id, _, _, added = self.store.register_folder_source(str(library))
        self.assertTrue(added)
        self.assertEqual(registered_id, source_id)
        sidebar._refresh_drives()
        self.assertEqual([(item["id"], item["kind"]) for item in sidebar._drives], [(source_id, "folder")])

    def test_unavailable_manual_folder_and_disconnected_drive_keep_their_types(self) -> None:
        source_id, label, path, _ = self.store.register_folder_source(str(self.temp_path / "missing"))
        self.store.replace_drive(source_id, label, path, [])
        self.store.replace_drive("1234ABCD", "External", "E:", [])
        sidebar = _SidebarProbe(self.service, source_id)
        sidebar._refresh_drives()
        sources = {item["id"]: item for item in sidebar._drives}
        self.assertEqual(sources[source_id]["kind"], "folder")
        self.assertFalse(sources[source_id]["connected"])
        self.assertEqual(sources["1234ABCD"]["kind"], "drive")
        self.assertFalse(sources["1234ABCD"]["connected"])

    def test_late_scan_completion_does_not_resurrect_removed_manual_folder(self) -> None:
        source_id, label, path, _ = self.store.register_folder_source(str(self.temp_path / "anime"))
        sidebar = _SidebarProbe(self.service, source_id)
        sidebar._refresh_drives()
        sidebar.removeSource(source_id)
        # Simulate an already-running scan's result; do not start a scanner.
        self.service.events.put(("complete", source_id, label, path, []))
        self.service.process_events()
        sidebar._refresh_drives()
        self.assertEqual(sidebar._drives, [])
        self.assertEqual(self.store.drives()[0][0], source_id)

    def test_manual_folder_can_be_renamed_and_unregistered_without_losing_index(self) -> None:
        library = self.temp_path / "anime"
        library.mkdir()
        (library / "show.mkv").write_bytes(b"show")
        source_id, _, _, _, _ = self.service.add_folder_source(str(library))
        self.wait_for_index(source_id)

        self.assertTrue(self.service.rename_source(source_id, "Anime PC"))
        reopened = IndexStore()
        try:
            self.assertEqual(reopened.source_preferences()[source_id], ("Anime PC", False))
        finally:
            reopened.conn.close()

        self.assertTrue(self.service.remove_source(source_id))
        self.assertEqual(self.store.folder_sources(), [])
        self.assertEqual(self.store.drives()[0][0], source_id)
        self.assertEqual(self.store.root_entries(source_id)[0][0], "show.mkv")

    def test_duplicate_or_empty_display_name_is_rejected(self) -> None:
        first, _, _, _ = self.store.register_folder_source(str(self.temp_path / "one"))
        second, _, _, _ = self.store.register_folder_source(str(self.temp_path / "two"))
        self.assertTrue(self.store.rename_source(first, "Anime"))
        self.assertFalse(self.store.rename_source(second, " anime "))
        self.assertFalse(self.store.rename_source(second, "   "))

    def test_external_drive_removal_persists_an_exclusion_without_losing_index(self) -> None:
        root = self.temp_path / "external"
        root.mkdir()
        (root / "anime.mkv").write_bytes(b"anime")
        connected = {"EXTERNAL": ("E:", "External", str(root))}
        with patch("indexer.service.connected_removable_drives", return_value=connected):
            self.assertTrue(self.service.check_drives())
            self.wait_for_index("EXTERNAL")
            self.service.remove_source("EXTERNAL")
            self.assertNotIn("EXTERNAL", self.service.connected)
            self.assertFalse(self.service.check_drives())
            self.assertEqual(self.store.drives()[0][0], "EXTERNAL")
            self.assertEqual(self.store.root_entries("EXTERNAL")[0][0], "anime.mkv")

            self.store.include_drive("EXTERNAL")
            self.assertTrue(self.service.check_drives())

    def test_source_root_is_only_returned_while_a_source_is_available(self) -> None:
        library = self.temp_path / "available"
        library.mkdir()
        source_id, _, _, _, _ = self.service.add_folder_source(str(library))
        self.assertEqual(self.service.source_root(source_id), str(library))
        library.rename(self.temp_path / "offline")
        self.assertIsNone(self.service.source_root(source_id))

    def test_opening_a_source_uses_its_current_root_and_skips_unavailable_sources(self) -> None:
        available = _ControllerProbe(str(self.temp_path / "available"))
        with patch("ui.controller.os.startfile", create=True) as open_in_explorer:
            AppController.openSourceInExplorer(available, "SOURCE")
            open_in_explorer.assert_called_once_with(str(self.temp_path / "available"))

        unavailable = _ControllerProbe(None)
        with patch("ui.controller.os.startfile", create=True) as open_in_explorer:
            AppController.openSourceInExplorer(unavailable, "SOURCE")
            open_in_explorer.assert_not_called()
        self.assertIn("available", unavailable.status)


if __name__ == "__main__":
    unittest.main()
