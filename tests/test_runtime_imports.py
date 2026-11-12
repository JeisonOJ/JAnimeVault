"""On-demand import triggers, private worker connections, and overlap protection."""
import threading
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication
import test_planned_imports as fixtures
from indexer.service import IndexerService
from indexer.planned_imports import import_inbox
from ui.controller import AppController


class RuntimeImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        fixtures.PlannedImportTests.setUp(self)
        self.controllers = []

    def tearDown(self):
        for c in self.controllers:
            c.shutdown()
            if c._capture_import_thread: c._capture_import_thread.join(5)
        self.app.processEvents()
        fixtures.PlannedImportTests.tearDown(self)

    write = fixtures.PlannedImportTests.write

    def controller(self):
        cover = Mock(); cover.process_events.return_value = []
        with patch("ui.controller.IndexerService", return_value=IndexerService(self.store)), patch("ui.controller.CoverService", return_value=cover):
            c = AppController()
        c.poll_drives = Mock()
        self.controllers.append(c)
        return c

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                time.sleep(.02)
                return
            time.sleep(.005)
        self.fail("Timed out waiting for runtime import")

    def test_entry_manual_refresh_receipts_and_no_recheck_on_render(self):
        first = fixtures.sample(text="勇者 🌸")
        self.write(first); self.write(fixtures.sample(text=first["text"]))
        c = self.controller()
        with patch("ui.controller.import_inbox", wraps=import_inbox) as importer:
            c.selectPlanned()
            self.wait(lambda: not c.importsBusy and len(c.plannedEntries) == 2)
            self.assertEqual(len(self.store.planned_entries()), 2)
            original_thread = c._capture_import_thread
            c.selectPlanned(); c.plannedChanged.emit()
            self.assertIs(c._capture_import_thread, original_thread)
            self.assertEqual(importer.call_count, 1)
            self.write(fixtures.sample(text="New while open"))
            c.refreshImports()
            self.wait(lambda: not c.importsBusy and len(c.plannedEntries) == 3)
            self.write(first)
            c.selectFavorites(); c.selectPlanned()
            self.wait(lambda: not c.importsBusy)
            self.assertEqual(len(self.store.planned_entries()), 3)
            self.assertIn("already imported", c.captureImportMessage)
            c.cover_service.enqueue.assert_not_called()
            c.cover_service.link_planned.assert_not_called()

    def test_empty_checks_and_failed_files(self):
        c = self.controller(); c._request_items = Mock()
        c.selectPlanned(); self.wait(lambda: not c.importsBusy)
        self.assertEqual(c.captureImportMessage, "")
        c._request_items.reset_mock()
        c.refreshImports(); self.wait(lambda: not c.importsBusy)
        self.assertEqual(c.captureImportMessage, "No new imports.")
        c._request_items.assert_not_called()
        broken = self.write(raw=b"{", filename="broken.json")
        self.write()
        c.refreshImports(); self.wait(lambda: not c.importsBusy)
        self.assertEqual(len(self.store.planned_entries()), 1)
        self.assertEqual(c.captureImportLevel, "error")
        self.assertTrue(broken.exists())

    def test_startup_entry_and_clicks_cannot_overlap(self):
        c = self.controller(); started = threading.Event(); release = threading.Event()
        def slow(store):
            started.set(); release.wait(3)
            return import_inbox(store)
        with patch("ui.controller.import_inbox", side_effect=slow) as importer:
            c.start()
            self.assertTrue(started.wait(1)); self.assertTrue(c.importsBusy)
            c.selectPlanned()
            for _ in range(5): c.refreshImports()
            self.assertEqual(importer.call_count, 1)
            release.set(); self.wait(lambda: not c.importsBusy)
            c.refreshImports(); self.wait(lambda: not c.importsBusy)
            self.assertEqual(importer.call_count, 2)

    def test_failure_releases_busy_and_allows_retry(self):
        c = self.controller()
        with patch("ui.controller.import_inbox", side_effect=RuntimeError("injected failure")):
            c.refreshImports(); self.wait(lambda: not c.importsBusy)
        self.assertEqual(c.captureImportLevel, "error")
        self.write()
        c.refreshImports(); self.wait(lambda: not c.importsBusy)
        self.assertEqual(len(self.store.planned_entries()), 1)
