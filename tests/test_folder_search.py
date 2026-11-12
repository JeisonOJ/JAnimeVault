"""Library candidate search: isolated SQLite and offline shared-worker fixtures."""
import threading
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication
import test_planned_links as fixtures
from test_planned_search import candidate
from indexer.covers import CoverMatch, CoverLookupError
from indexer.service import IndexerService
from ui.controller import AppController


class FolderSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        fixtures.PlannedLinkTests.setUp(self)
        self.controllers = []
        for source in ("A", "B"):
            self.store.replace_drive(source, source, "", [(source, "Folder/Anime", "Folder", "Anime", 1, None, None),
                                                        (source, "file.txt", "", "file.txt", 0, 10, None)])

    def tearDown(self):
        for c in self.controllers: c.shutdown()
        fixtures.PlannedLinkTests.tearDown(self)

    service = fixtures.PlannedLinkTests.service
    wait = fixtures.PlannedLinkTests.wait

    def controller(self, service):
        with patch("ui.controller.IndexerService", return_value=IndexerService(self.store)), patch("ui.controller.CoverService", return_value=service):
            c = AppController()
        self.controllers.append(c)
        c._selected_drive = "A"
        c._request_items = Mock()
        return c

    def results(self, c):
        self.wait(lambda: (c.process_cover_events() or True) and not c.plannedSearch["busy"])

    def test_confirm_identity_replace_failure_and_offline_retention(self):
        service = self.service(candidate_search=lambda query: [candidate()], exact_lookup=lambda number: CoverMatch(number, "cover"))
        c = self.controller(service)
        self.store.set_favorite("A", "Folder/Anime", True)
        self.store.set_to_watch("A", "Folder/Anime", True)
        c.beginFolderSearch("B", "FOLDER\\ANIME"); self.results(c)
        self.assertEqual(c.plannedSearch["query"], "Anime")
        self.assertEqual(self.store.manual_links("B"), {})
        c.confirmPlannedCandidate(99)
        self.assertEqual(self.store.manual_links("B"), {})
        c.confirmPlannedCandidate(7)
        self.wait(lambda: (c.process_cover_events() or True) and not c._manual_status_pending)
        self.assertEqual(self.store.manual_links("B")["folder\\anime"][0], 7)
        self.assertEqual(self.store.manual_links("A"), {})
        before = self.store.manual_links("B")
        service._candidate_search = lambda query: [candidate(8)]
        service._exact_lookup = Mock(side_effect=CoverLookupError("offline"))
        c.beginFolderSearch("B", "Folder/Anime"); self.results(c); c.confirmPlannedCandidate(8)
        self.wait(lambda: (c.process_cover_events() or True) and not c._manual_status_pending)
        self.assertEqual(self.store.manual_links("B"), before)
        self.assertFalse(c.service.connected)
        self.assertEqual(self.store.planned_entries(), [])
        self.assertEqual(c.currentView, "source")
        self.assertEqual(c.selectedDrive, "A")
        self.assertIn(("A", "folder\\anime"), self.store.favorite_keys())
        self.assertIn(("A", "folder\\anime"), self.store.to_watch_keys())

    def test_edit_cancel_files_and_removed_entry(self):
        service = self.service(candidate_search=lambda query: [candidate()])
        c = self.controller(service)
        c.beginFolderSearch("A", "file.txt")
        self.assertFalse(c.plannedSearch["entryId"])
        c.beginFolderSearch("A", "Folder/Anime"); self.results(c)
        c.searchPlannedCandidates("Edited query"); self.results(c)
        self.assertEqual(c.plannedSearch["query"], "Edited query")
        c.cancelPlannedSearch(); c.confirmPlannedCandidate(7)
        self.assertFalse(self.store.manual_links("A"))
        c.beginFolderSearch("A", "Folder/Anime"); self.results(c)
        self.store.replace_drive("A", "A", "", [])
        c.confirmPlannedCandidate(7)
        self.assertFalse(c.plannedSearch["entryId"])
        self.assertFalse(self.store.manual_links("A"))

    def test_navigation_and_resubmission_reject_stale_candidates(self):
        started, release = threading.Event(), threading.Event()
        def search(query):
            if query == "Anime": started.set(); release.wait(2)
            return [candidate(8 if query == "New" else 7)]
        service = self.service(candidate_search=search)
        c = self.controller(service)
        c.beginFolderSearch("A", "Folder/Anime"); self.assertTrue(started.wait(2))
        c.searchPlannedCandidates("New"); release.set(); self.results(c)
        self.assertEqual(c.plannedSearch["results"][0]["id"], 8)
        for action in (lambda: c.selectDrive("B"), lambda: c.openFolder("Folder"), lambda: c.selectFavorites()):
            c.beginFolderSearch("A", "Folder/Anime"); self.results(c)
            action()
            self.assertFalse(c.plannedSearch["entryId"])
            c.confirmPlannedCandidate(8)
        self.assertFalse(self.store.manual_links("A"))

    def test_pending_link_cancelled_by_navigation_or_entry_deletion(self):
        for action in ("navigate", "delete", "unlink", "source"):
            with self.subTest(action=action):
                self.store.replace_drive("A", "A", "", [("A", "Folder/Anime", "Folder", "Anime", 1, None, None)])
                started, release = threading.Event(), threading.Event()
                def lookup(number):
                    started.set(); release.wait(2); return CoverMatch(number, "cover")
                service = self.service(candidate_search=lambda query: [candidate()], exact_lookup=lookup)
                c = self.controller(service)
                c.beginFolderSearch("A", "Folder/Anime"); self.results(c); c.confirmPlannedCandidate(7)
                self.assertTrue(started.wait(2))
                if action == "navigate": c.openFolder("Other")
                elif action == "delete": self.store.replace_drive("A", "A", "", [])
                elif action == "unlink": c.unlinkAniListEntry("A", "Folder/Anime")
                else: c.removeSource("A")
                release.set()
                self.wait(lambda: not service._manual_pending)
                c.process_cover_events()
                self.assertFalse(self.store.manual_links("A"))

    def test_folder_search_and_link_share_four_second_fifo(self):
        clock, calls, done = [0.0], [], threading.Event()
        def lookup(number):
            calls.append(("folder", clock[0])); done.set(); return CoverMatch(number, "cover")
        with patch("indexer.covers.time.monotonic", side_effect=lambda: clock[0]):
            service = self.service(request_interval=4, candidate_search=lambda query: calls.append(("search", clock[0])) or [candidate()],
                exact_lookup=lookup, searcher=lambda name: calls.append(("auto", clock[0])))
            def advance(seconds): clock[0] += seconds; return False
            with patch.object(service._stopped, "wait", side_effect=advance):
                service.enqueue("Automatic")
                service.search_planned("opaque-folder-target", "Anime")
                service.link_folder("A", "Folder/Anime", 7, require_indexed=True)
                self.assertTrue(done.wait(2))
                service.shutdown(); service._worker.join(2)
        self.assertEqual(calls, [("auto", 0), ("search", 4), ("folder", 8)])
