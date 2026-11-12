"""Planned Stage 2: mocked lookups, isolated SQLite, no live API access."""
import io
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication
import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from indexer.covers import CoverService, PlannedMatch, CoverMatch, CoverLookupError, ApiBlocked, RateLimited, lookup_planned_id
from ui.controller import AppController


class PlannedLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        root = Path("tests/.planned-link-test-runtime")
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.original = database.APP_DIR, database.DB_PATH
        database.APP_DIR = Path(self.temp.name)
        database.DB_PATH = database.APP_DIR / "index.db"
        self.store = IndexStore()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
            service._worker.join(3)
        self.store.conn.close()
        for handler in list(logging.getLogger("external_disk_indexer.covers").handlers):
            if getattr(handler, "_cover_log_path", "").startswith(str(database.APP_DIR.resolve())):
                logging.getLogger("external_disk_indexer.covers").removeHandler(handler)
                handler.close()
        database.APP_DIR, database.DB_PATH = self.original
        self.temp.cleanup()

    def service(self, **kwargs):
        def download(url, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff\xd8\xff" + b"x" * 1100)
        kwargs.setdefault("downloader", download)
        kwargs.setdefault("planned_lookup", lambda number: PlannedMatch(number, "Official title", "https://example.test/cover"))
        kwargs.setdefault("request_interval", 0)
        service = CoverService(**kwargs)
        self.services.append(service)
        return service

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for Planned operation")

    def finish(self, service, expected="planned_ready"):
        received = []
        def done():
            received.extend(service.process_events())
            return any(event[0] == expected for event in received)
        self.wait(done)
        return received

    def test_exact_metadata_uses_existing_graphql_transport(self):
        response = io.StringIO(json.dumps({"data": {"Page": {"media": [{"id": 7, "title": {"english": "English", "romaji": "Romaji"}, "coverImage": {"large": "image"}}]}}}))
        with patch("indexer.covers.urlopen", return_value=response) as api:
            self.assertEqual(lookup_planned_id(7), PlannedMatch(7, "English", "image"))
            self.assertEqual(json.loads(api.call_args.args[0].data)["variables"], {"id": 7})

    def test_url_creation_link_replace_unlink_preserve_uuid_and_manual_title(self):
        service = self.service()
        with patch("ui.controller.IndexerService", return_value=IndexerService(self.store)), patch("ui.controller.CoverService", return_value=service):
            controller = AppController()
        controller._request_items = Mock()
        controller.selectPlanned()
        controller.linkPlannedEntry("", "https://anilist.co/anime/7/Title/")
        self.assertTrue(controller.plannedBusy)
        def finished():
            controller.process_cover_events()
            return not controller.plannedBusy
        self.wait(finished)
        row = self.store.planned_entries()[0]
        self.assertEqual(row["title"], "Official title")
        entry_id = row["id"]
        self.store.save_planned_title("My original title", entry_id)
        controller.linkPlannedEntry(entry_id, "https://anilist.co/anime/8/")
        self.wait(finished)
        row = self.store.planned_entries()[0]
        self.assertEqual((row["id"], row["title"], row["anilistId"]), (entry_id, "My original title", 8))
        self.assertTrue(Path(row["cachePath"]).exists())
        controller.unlinkPlannedEntry(entry_id)
        row = self.store.planned_entries()[0]
        self.assertEqual((row["id"], row["title"], row["anilistId"]), (entry_id, "My original title", None))
        controller.linkPlannedEntry(entry_id, "http://evil.test/anime/9/")
        self.assertFalse(controller.plannedBusy)
        controller.shutdown()

    def test_failures_preserve_old_link_and_failed_creation_leaves_no_row(self):
        service = self.service()
        entry_id, _ = self.store.save_planned_title("Keep me")
        service.link_planned(entry_id, 7)
        self.finish(service)
        before = self.store.planned_entries()
        service._planned_lookup = Mock(side_effect=CoverLookupError("lookup failed"))
        service.link_planned(entry_id, 8)
        self.finish(service, "planned_failed")
        service.link_planned("new-uuid", 9, create=True)
        self.finish(service, "planned_failed")
        self.assertEqual(self.store.planned_entries(), before)
        service._planned_lookup = lambda number: PlannedMatch(number, "New title", "image")
        service._downloader = Mock(side_effect=CoverLookupError("invalid image"))
        service.link_planned(entry_id, 10)
        self.finish(service, "planned_failed")
        self.assertEqual(self.store.planned_entries(), before)

    def test_duplicate_ids_are_atomic_and_images_are_reused(self):
        service = self.service()
        first, _ = self.store.save_planned_title("First")
        second, _ = self.store.save_planned_title("Second")
        service.link_planned(first, 7)
        self.finish(service)
        service._downloader = Mock(side_effect=AssertionError("Cached image must be reused"))
        service.link_planned(second, 7)
        self.finish(service, "planned_failed")
        service.link_planned("new-uuid", 7, create=True)
        self.finish(service, "planned_failed")
        self.assertEqual(len(self.store.planned_entries()), 2)
        self.assertIsNone(next(row for row in self.store.planned_entries() if row["id"] == second)["anilistId"])
        service.link_planned(first, 7)
        self.finish(service)
        service._downloader.assert_not_called()

    def test_stale_link_cannot_undo_replace_unlink_or_delete(self):
        for action in ("replace", "unlink", "delete"):
            with self.subTest(action=action):
                entry_id, _ = self.store.save_planned_title(action)
                started, release = threading.Event(), threading.Event()
                def lookup(number):
                    if number == 7:
                        started.set()
                        release.wait(2)
                    return PlannedMatch(number, "Official", "image")
                service = self.service(planned_lookup=lookup)
                service.link_planned(entry_id, 7)
                self.assertTrue(started.wait(2))
                if action == "replace":
                    service.link_planned(entry_id, 8)
                else:
                    service.invalidate_planned(entry_id)
                    (self.store.unlink_planned if action == "unlink" else self.store.remove_planned)(entry_id)
                    service.link_planned("barrier-" + action, 9 if action == "unlink" else 10, create=True)
                release.set()
                self.finish(service)
                rows = {row["id"]: row for row in self.store.planned_entries()}
                if action == "delete":
                    self.assertNotIn(entry_id, rows)
                else:
                    self.assertEqual(rows[entry_id]["anilistId"], 8 if action == "replace" else None)

    def test_rate_limit_and_blocked_errors_preserve_data(self):
        for error in (RateLimited(15), ApiBlocked("blocked")):
            service = self.service(planned_lookup=Mock(side_effect=error))
            service.link_planned("new", 7, create=True)
            self.finish(service, "planned_failed")
            self.assertEqual(self.store.planned_entries(), [])
            if isinstance(error, RateLimited):
                self.assertGreater(service._next_request_at, time.monotonic() + 10)
            else:
                self.assertTrue(service._api_blocked)

    def test_four_second_gate_shared_with_other_job_types(self):
        clock, calls = [0.0], []
        done = threading.Event()
        source, _, _, _ = self.store.register_folder_source(str(database.APP_DIR / "source"))
        def auto(name):
            calls.append(("auto", clock[0])); return None
        def planned(number):
            calls.append(("planned", clock[0])); return PlannedMatch(number, "Official", "image")
        def folder(number):
            calls.append(("folder", clock[0])); done.set(); return CoverMatch(number, "image")
        def advance(seconds):
            clock[0] += seconds; return False
        with patch("indexer.covers.time.monotonic", side_effect=lambda: clock[0]):
            service = self.service(searcher=auto, planned_lookup=planned, exact_lookup=folder, request_interval=4)
            with patch.object(service._stopped, "wait", side_effect=advance):
                service.enqueue("Automatic title")
                service.link_planned("new", 7, create=True)
                service.link_folder(source, "Anime", 8)
                self.assertTrue(done.wait(2))
                service.shutdown(); service._worker.join(2)
        self.assertEqual(calls, [("auto", 0), ("planned", 4), ("folder", 8)])
