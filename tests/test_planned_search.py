"""Candidate review: offline transport mocks, isolated DB, shared FIFO and tokens."""
import io
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication
import test_planned_links as fixtures
from indexer.covers import search_planned_candidates, CoverLookupError, ApiBlocked, RateLimited, PlannedMatch
from indexer.service import IndexerService
from ui.controller import AppController


def candidate(number=7):
    return {"id": number, "title": "Candidate", "alternate": "候補", "year": "2026", "format": "TV"}


class PlannedSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        fixtures.PlannedLinkTests.setUp(self)
        self.controllers = []

    def tearDown(self):
        for c in self.controllers:
            c.shutdown()
            if c._capture_import_thread: c._capture_import_thread.join(3)
        fixtures.PlannedLinkTests.tearDown(self)

    service = fixtures.PlannedLinkTests.service
    wait = fixtures.PlannedLinkTests.wait
    finish = fixtures.PlannedLinkTests.finish

    def controller(self, service):
        with patch("ui.controller.IndexerService", return_value=IndexerService(self.store)), patch("ui.controller.CoverService", return_value=service):
            c = AppController()
        self.controllers.append(c)
        c._current_view = "planned"; c._request_items = Mock()
        return c

    def results(self, c):
        self.wait(lambda: (c.process_cover_events() or True) and not c.plannedSearch["busy"])

    def test_parse_candidate_fields_without_changing_automatic_query(self):
        media = [{"id": 7, "title": {"english": "English", "romaji": "Romaji", "native": "日本語"}, "seasonYear": 2026, "format": "TV"},
                 {"id": "bad"}, None, {"id": 8, "title": {}}]
        with patch("indexer.covers.urlopen", return_value=io.StringIO(json.dumps({"data": {"Page": {"media": media}}}))) as request:
            results = search_planned_candidates("Original Unicode 🌸")
        self.assertEqual(results[0], {"id": 7, "title": "English", "alternate": "Romaji / 日本語", "year": "2026", "format": "TV"})
        payload = json.loads(request.call_args.args[0].data)
        self.assertIn("seasonYear", payload["query"])
        self.assertIn("perPage: 5", payload["query"])
        self.assertEqual(payload["variables"]["search"], "Original Unicode 🌸")
        from indexer.covers import SEARCH_QUERY
        self.assertNotIn("seasonYear", SEARCH_QUERY)

    def test_review_confirm_replace_and_cancel_reuse_existing_link_path(self):
        entry, _ = self.store.save_planned_title("Original manual title")
        service = self.service(candidate_search=lambda query: [candidate()])
        c = self.controller(service)
        c.beginPlannedSearch(entry); self.results(c)
        self.assertEqual(c.plannedSearch["query"], "Original manual title")
        self.assertIsNone(self.store.planned_entries()[0]["anilistId"])
        service._downloader = Mock(wraps=service._downloader)
        c.cancelPlannedSearch(); c.confirmPlannedCandidate(7)
        service._downloader.assert_not_called()
        c.beginPlannedSearch(entry); self.results(c)
        c.confirmPlannedCandidate(99)
        self.assertFalse(c.plannedBusy)
        c.confirmPlannedCandidate(7)
        self.wait(lambda: (c.process_cover_events() or True) and not c.plannedBusy)
        row = self.store.planned_entries()[0]
        self.assertEqual((row["id"], row["title"], row["anilistId"]), (entry, "Original manual title", 7))
        before = self.store.planned_entries()
        c.beginPlannedSearch(entry); self.results(c); c.cancelPlannedSearch()
        self.assertEqual(self.store.planned_entries(), before)
        service._candidate_search = lambda query: [candidate(8)]
        service._planned_lookup = Mock(side_effect=CoverLookupError("lookup failed"))
        c.beginPlannedSearch(entry); self.results(c); c.confirmPlannedCandidate(8)
        self.wait(lambda: (c.process_cover_events() or True) and not c.plannedBusy)
        self.assertEqual(self.store.planned_entries(), before)

    def test_errors_no_results_retry_and_conflict(self):
        entry, _ = self.store.save_planned_title("First")
        other, _ = self.store.save_planned_title("Other")
        service = self.service(candidate_search=Mock(side_effect=CoverLookupError("offline")))
        c = self.controller(service)
        c.beginPlannedSearch(entry); self.results(c)
        self.assertEqual(c.plannedSearch["error"], "offline")
        service._candidate_search = lambda query: []
        c.searchPlannedCandidates("Edited"); self.results(c)
        self.assertEqual(c.plannedSearch["results"], [])
        service._candidate_search = lambda query: [candidate()]
        service.link_planned(other, 7); self.finish(service)
        c.searchPlannedCandidates("Edited again"); self.results(c); c.confirmPlannedCandidate(7)
        self.wait(lambda: (c.process_cover_events() or True) and not c.plannedBusy)
        self.assertIsNone(next(row for row in self.store.planned_entries() if row["id"] == entry)["anilistId"])
        self.assertIn("already", c.status)

    def test_switched_edited_closed_deleted_and_navigation_stale_results(self):
        first, _ = self.store.save_planned_title("First")
        second, _ = self.store.save_planned_title("Second")
        started, release = threading.Event(), threading.Event()
        def slow(query):
            if query == "First": started.set(); release.wait(3)
            return [candidate()]
        service = self.service(candidate_search=slow)
        c = self.controller(service)
        c.beginPlannedSearch(first); self.assertTrue(started.wait(2))
        c.beginPlannedSearch(second); c.searchPlannedCandidates("Resubmitted")
        release.set(); self.results(c)
        self.assertEqual((c.plannedSearch["entryId"], c.plannedSearch["query"]), (second, "Resubmitted"))
        for action in (lambda: c.cancelPlannedSearch(), lambda: c.removePlanned(second), lambda: c.selectFavorites()):
            c._current_view = "planned"
            target = first if not self.store.conn.execute("SELECT 1 FROM planned_anime WHERE id=?", (second,)).fetchone() else second
            c.beginPlannedSearch(target)
            request = c._candidate_request
            action()
            service.events.put(("planned_candidates", request[0], request[1], [candidate()], ""))
            c.process_cover_events()
            self.assertEqual(c.plannedSearch["entryId"], "")
            self.assertFalse(c.plannedSearch["busy"])

    def test_shared_four_second_fifo_and_blocking(self):
        clock, calls, done = [0.0], [], threading.Event()
        def search(query): calls.append(("search", clock[0])); return [candidate()]
        def lookup(number): calls.append(("link", clock[0])); done.set(); return PlannedMatch(number, "Official", "image")
        with patch("indexer.covers.time.monotonic", side_effect=lambda: clock[0]):
            service = self.service(candidate_search=search, planned_lookup=lookup, request_interval=4,
                                   searcher=lambda name: calls.append(("auto", clock[0])))
            def advance(seconds): clock[0] += seconds; return False
            with patch.object(service._stopped, "wait", side_effect=advance):
                service.enqueue("Automatic")
                service.search_planned("entry", "Title")
                service.link_planned("new", 7, create=True)
                self.assertTrue(done.wait(2))
                service.shutdown(); service._worker.join(2)
        self.assertEqual(calls, [("auto", 0), ("search", 4), ("link", 8)])
        for error in (RateLimited(15), ApiBlocked("blocked")):
            service = self.service(candidate_search=Mock(side_effect=error))
            token = service.search_planned("entry", "Title")
            received = []
            self.wait(lambda: (received.extend(service.process_events()) or bool(received)))
            self.assertEqual(received[0][2], token)
            self.assertTrue(received[0][4])
            if isinstance(error, RateLimited): self.assertGreater(service._next_request_at, time.monotonic() + 10)
            else: self.assertTrue(service._api_blocked)
