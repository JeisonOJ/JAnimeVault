"""Focused tests for viewport-driven AniList cover loading."""

from __future__ import annotations

import tempfile
import time
import unittest
import shutil
import logging
import re
import json
import os
import io
import threading
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication

import indexer.database as database
from indexer.covers import MATCHING_REVISION, ApiBlocked, CoverLookupError, CoverMatch, CoverService, normalize_anime_name, select_conservative_match
from indexer.database import IndexStore
from indexer.covers import replay_captured_candidates, search_anilist, shortened_search_title, RateLimited


JPEG_BYTES = b"\xff\xd8\xff" + (b"x" * 1100)


class CoverServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        test_root = Path(__file__).parent / ".cover-test-runtime"
        test_root.mkdir(exist_ok=True)
        self.temp_path = Path(tempfile.mkdtemp(prefix="cover-", dir=test_root))
        self.original_app_dir = database.APP_DIR
        self.original_db_path = database.DB_PATH
        database.APP_DIR = self.temp_path / "app"
        database.DB_PATH = database.APP_DIR / "index.db"
        self.cache_dir = database.APP_DIR / "cache" / "covers"

    def tearDown(self) -> None:
        logger = logging.getLogger("external_disk_indexer.covers")
        for handler in list(logger.handlers):
            if getattr(handler, "_cover_log_handler", False) and str(handler.baseFilename if hasattr(handler, "baseFilename") else "").startswith(str(self.temp_path)):
                logger.removeHandler(handler)
                handler.close()
        database.APP_DIR = self.original_app_dir
        database.DB_PATH = self.original_db_path
        shutil.rmtree(self.temp_path, ignore_errors=True)

    @staticmethod
    def wait_for(service: CoverService, predicate, timeout: float = 2.0) -> list[tuple]:
        deadline = time.monotonic() + timeout
        events: list[tuple] = []
        while time.monotonic() < deadline:
            events.extend(service.process_events())
            if predicate(events):
                return events
            time.sleep(0.01)
        return events

    def save_result(self, name: str, status: str, path: Path | None = None, revision: int = 0) -> None:
        store = IndexStore()
        try:
            store.save_cover_result(normalize_anime_name(name), 7, "https://example.test/cover.jpg", str(path) if path else None, status, matching_revision=revision)
        finally:
            store.conn.close()

    def log_text(self, service: CoverService) -> str:
        for handler in service._logger.handlers:
            handler.flush()
        return (database.APP_DIR / "logs" / "covers.log").read_text(encoding="utf-8")

    def test_valid_cached_cover_does_not_search_again(self) -> None:
        cover = self.cache_dir / "7.jpg"
        cover.parent.mkdir(parents=True)
        cover.write_bytes(JPEG_BYTES)
        self.save_result("Dandadan", "found", cover)
        searches = 0

        def searcher(_: str) -> CoverMatch:
            nonlocal searches
            searches += 1
            return CoverMatch(8, "https://example.test/new.jpg")

        service = CoverService(searcher=searcher, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(service.enqueue("Dandadan"))
            events = self.wait_for(service, lambda received: bool(received))
            self.assertEqual(searches, 0)
            self.assertEqual(events[0][0], "ready")
            log_text = self.log_text(service)
            self.assertIn("Dandadan | Cache hit", log_text)
        finally:
            service.shutdown()

    def test_completed_no_match_is_not_searched_again(self) -> None:
        self.save_result("Unknown Show", "no_match", revision=MATCHING_REVISION)
        searches = 0

        def searcher(_: str) -> CoverMatch | None:
            nonlocal searches
            searches += 1
            return None

        service = CoverService(searcher=searcher, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(service.enqueue("Unknown Show"))
            self.wait_for(service, lambda _: not service._queued)
            self.assertEqual(searches, 0)
            self.assertIn("Unknown Show | Cached result: no_match", self.log_text(service))
        finally:
            service.shutdown()

    def test_legacy_no_match_retries_once_across_worker_restarts(self) -> None:
        self.save_result("Unknown Show", "no_match")
        searches = []
        for _ in range(2):
            service = CoverService(searcher=lambda name: searches.append(name), cache_dir=self.cache_dir, request_interval=0)
            try:
                service.enqueue("Unknown Show")
                self.wait_for(service, lambda _: not service._queued)
                self.assertFalse(service._queued)
            finally:
                service.shutdown()
                service._worker.join(2)
        self.assertEqual(searches, ["Unknown Show"])

    def test_old_cache_schema_migrates_without_losing_rows(self) -> None:
        import sqlite3

        database.APP_DIR.mkdir(parents=True)
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute("CREATE TABLE anime_cover_cache (normalized_name TEXT PRIMARY KEY, anilist_id INTEGER, cover_url TEXT, cache_path TEXT, status TEXT NOT NULL, updated_at TEXT)")
        conn.execute("INSERT INTO anime_cover_cache(normalized_name,status) VALUES('legacy','no_match')")
        conn.commit()
        conn.close()
        for _ in range(2):
            store = IndexStore()
            try:
                self.assertEqual(store.cover_result("legacy")[3], "no_match")
                self.assertEqual(store.cover_matching_revision("legacy"), 0)
            finally:
                store.conn.close()

    def test_controller_only_requeues_legacy_no_match(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from ui.controller import AppController

        self.save_result("Legacy", "no_match")
        self.save_result("Current", "no_match", revision=MATCHING_REVISION)
        store = IndexStore()
        try:
            store.replace_drive("TEST", "Test", "T:", [
                ("TEST", name, "", name, 1, None, None) for name in ("Legacy", "Current")
            ])
        finally:
            store.conn.close()
        probe = SimpleNamespace(_queryFinished=Mock(), _cached_cover_url=AppController._cached_cover_url, cover_service=_CoverService())
        AppController._query_items(probe, 1, "TEST", "", "")
        items = probe._queryFinished.emit.call_args.args[1]
        self.assertEqual({item["name"]: item["coverStatus"] for item in items}, {"Legacy": "", "Current": "no_match"})

    def test_opt_in_diagnostics_and_offline_replay_leave_matching_unchanged(self) -> None:
        name = "Wonderful Adventure Academy"
        media = [MatchingTests.candidate(1, name), MatchingTests.candidate(2, name + " II"),
                 {"id": None, "title": {"romaji": name}},
                 MatchingTests.candidate(3, "Unrelated Short Title")]
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": "[]"}), patch("indexer.covers.get_cover_logger") as logger:
            baseline = select_conservative_match(name, media)
            logger.assert_not_called()
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([name])}), patch("indexer.covers.urlopen", side_effect=AssertionError("No live requests")):
            self.assertEqual(select_conservative_match(name, media), baseline)
            log_path = database.APP_DIR / "logs" / "covers.log"
            records = [json.loads(line.split("MATCH_DIAG ", 1)[1]) for line in log_path.read_text(encoding="utf-8").splitlines() if "MATCH_DIAG " in line]
            snapshot = next(record for record in records if record["event"] == "candidate_snapshot")
            self.assertEqual(snapshot["count"], 4)
            self.assertEqual(snapshot["search"], name)
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("canonical sequence conflict", text)
            self.assertIn("missing valid integer ID or nonempty cover URL", text)
            self.assertIn("per-word similarity below 0.8", text)
            self.assertIn('"margin":', text)
            self.assertNotIn("https://example.test", text)
            with patch("indexer.covers.database.IndexStore", side_effect=AssertionError("No cache access")):
                self.assertEqual(replay_captured_candidates(log_path, name).anilist_id, baseline.anilist_id)
                with self.assertRaisesRegex(ValueError, "No captured candidates"):
                    replay_captured_candidates(log_path, "Not captured")

    def test_diagnostics_explain_ambiguity_and_similarity_rejections(self) -> None:
        name = "Wonderful Adventure Academy"
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([name])}):
            self.assertIsNone(select_conservative_match(name, [MatchingTests.candidate(1, name), MatchingTests.candidate(2, name)]))
            self.assertIsNone(select_conservative_match(name, [MatchingTests.candidate(3, name + " Returns")]))
            self.assertIsNone(select_conservative_match(name, []))
        text = (database.APP_DIR / "logs" / "covers.log").read_text(encoding="utf-8")
        self.assertIn("ambiguous: margin below 0.08", text)
        self.assertIn("word-count mismatch", text)
        self.assertIn('"count": 0', text)

    def test_diagnostics_do_not_retry_terminal_cache_entries(self) -> None:
        from unittest.mock import Mock

        for status in ("no_match", "failed", "blocked", "found"):
            name = "Diagnostic " + status
            path = self.cache_dir / "7.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(JPEG_BYTES)
            self.save_result(name, status, path, revision=MATCHING_REVISION)
            searcher = Mock(side_effect=AssertionError("Cached entries must not search"))
            with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([name])}):
                service = CoverService(searcher=searcher, request_interval=0, cache_dir=self.cache_dir)
                try:
                    service.enqueue(name)
                    self.wait_for(service, lambda _: not service._queued)
                    self.assertFalse(service._queued)
                    searcher.assert_not_called()
                    text = self.log_text(service)
                    self.assertIn('"event": "worker_cache"', text)
                    self.assertIn('"lookup_skipped": true', text)
                finally:
                    service.shutdown()
                    service._worker.join(2)

    def test_controller_cache_skip_diagnostics_preserve_eligibility(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from ui.controller import AppController

        name = "Diagnostic Show"
        self.save_result(name, "no_match", revision=MATCHING_REVISION)
        store = IndexStore()
        try:
            store.replace_drive("TEST", "Test", "T:", [("TEST", name, "", name, 1, None, None)])
        finally:
            store.conn.close()
        probe = SimpleNamespace(_queryFinished=Mock(), _cached_cover_url=AppController._cached_cover_url, cover_service=_CoverService())
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([name])}):
            AppController._query_items(probe, 1, "TEST", "", "")
            items = probe._queryFinished.emit.call_args.args[1]
            queue_probe = SimpleNamespace(currentView="source", _items=items, cover_service=_CoverService(), _unlinked_folders=set())
            AppController.requestCoversForRange(queue_probe, 0, 0)
            self.assertEqual(queue_probe.cover_service.names, [])
        text = (database.APP_DIR / "logs" / "covers.log").read_text(encoding="utf-8")
        self.assertIn('"event": "controller_cache"', text)
        self.assertIn(f'"revision": {MATCHING_REVISION}', text)
        self.assertIn('"event": "controller_skip"', text)

    def test_explicit_diagnostic_retry_is_exact_title_once_and_uses_worker(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from ui.controller import AppController

        name = "Yuusha Party wo Oidasareta Kiyoubinbou"
        others = [name.lower(), name + " II", "Other Anime"]
        for title in [name] + others:
            self.save_result(title, "no_match", revision=MATCHING_REVISION)
        searches = []
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([name]), "EDI_COVER_DIAGNOSTIC_RETRY": name}):
            service = CoverService(searcher=lambda title: searches.append(title), request_interval=0, cache_dir=self.cache_dir)
            try:
                probe = SimpleNamespace(currentView="source", cover_service=service, _unlinked_folders=set(), _items=[
                    {"name": title, "isFolder": True, "coverUrl": "", "coverStatus": "no_match"}
                    for title in [name] + others
                ])
                with patch.object(service, "_wait_for_rate_limit", wraps=service._wait_for_rate_limit) as limiter:
                    for _ in range(3):
                        AppController.requestCoversForRange(probe, 0, 3)
                    self.wait_for(service, lambda _: not service._queued)
                    self.assertFalse(service._queued)
                    limiter.assert_called_once()
                self.assertEqual(searches, [name])
                self.assertFalse(service.diagnostic_retry_available(name))
                AppController.requestCoversForRange(probe, 0, 3)
                self.assertFalse(service.enqueue(name))
                # Also exercise the worker cache bypass check after the token is consumed.
                service._process_job(normalize_anime_name(name), name, time.monotonic())
                self.assertEqual(searches, [name])
                store = IndexStore()
                try:
                    self.assertEqual(store.cover_result(normalize_anime_name(name))[3], "no_match")
                    self.assertEqual(store.cover_matching_revision(normalize_anime_name(name)), MATCHING_REVISION)
                finally:
                    store.conn.close()
                text = self.log_text(service)
                self.assertIn('"event": "controller_diagnostic_retry"', text)
                self.assertIn('"event": "worker_diagnostic_retry"', text)
            finally:
                service.shutdown()
                service._worker.join(2)

    def test_explicit_diagnostic_retry_preserves_found_failed_and_blocked(self) -> None:
        from unittest.mock import Mock

        name = "Selected Anime"
        path = self.cache_dir / "7.jpg"
        path.parent.mkdir(parents=True)
        path.write_bytes(JPEG_BYTES)
        for status in ("found", "failed", "blocked"):
            self.save_result(name, status, path, revision=MATCHING_REVISION)
            with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([name]), "EDI_COVER_DIAGNOSTIC_RETRY": name}):
                searcher = Mock()
                service = CoverService(searcher=searcher, request_interval=0, cache_dir=self.cache_dir)
                try:
                    service.enqueue(name)
                    self.wait_for(service, lambda _: not service._queued)
                    self.assertFalse(service._queued)
                    searcher.assert_not_called()
                finally:
                    service.shutdown()
                    service._worker.join(2)

    def test_retry_requires_diagnostics_to_be_explicitly_enabled(self) -> None:
        name = "Selected Anime"
        self.save_result(name, "no_match", revision=MATCHING_REVISION)
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": "[]", "EDI_COVER_DIAGNOSTIC_RETRY": name}):
            service = CoverService(searcher=lambda _: self.fail("Unexpected lookup"), request_interval=0)
            try:
                self.assertFalse(service.diagnostic_retry_available(name))
                service.enqueue(name)
                self.wait_for(service, lambda _: not service._queued)
                self.assertNotIn("Unexpected lookup", self.log_text(service))
            finally:
                service.shutdown()
                service._worker.join(2)

    def test_fallback_request_spacing_and_next_fifo_job(self) -> None:
        first = "Yuusha Party wo Oidasareta Kiyoubinbou"
        second = "Other Complete Anime Title"
        clock = [0.0]
        requests = []
        last_request = threading.Event()

        def response(request, **_):
            text = json.loads(request.data)["variables"]["search"]
            requests.append((text, clock[0]))
            media = [] if len(requests) < 3 else [MatchingTests.candidate(7, "Unrelated")]
            if len(requests) == 3:
                last_request.set()
            return io.StringIO(json.dumps({"data": {"Page": {"media": media}}}))

        def advance(seconds):
            clock[0] += seconds
            return False

        with patch("indexer.covers.urlopen", side_effect=response), patch("indexer.covers.time.monotonic", side_effect=lambda: clock[0]):
            service = CoverService(cache_dir=self.cache_dir)  # real default four-second gate
            try:
                with patch.object(service._stopped, "wait", side_effect=advance):
                    service.enqueue(first)
                    service.enqueue(second)
                    self.assertTrue(last_request.wait(2))
                    service.shutdown()
                    service._worker.join(2)
                self.assertEqual(requests, [(first, 0.0), ("Yuusha Party wo Oidasareta", 4.0), (second, 8.0)])
                store = IndexStore()
                try:
                    self.assertEqual(store.cover_matching_revision(normalize_anime_name(first)), MATCHING_REVISION)
                finally:
                    store.conn.close()
            finally:
                service.shutdown()
                service._worker.join(2)

    def test_revision_one_no_match_gets_one_new_search_opportunity(self) -> None:
        self.assertEqual(MATCHING_REVISION, 2)
        name = "Previous Search Revision"
        self.save_result(name, "no_match", revision=1)
        searches = []
        for _ in range(2):
            service = CoverService(searcher=lambda title: searches.append(title), request_interval=0)
            try:
                service.enqueue(name)
                self.wait_for(service, lambda _: not service._queued)
                self.assertFalse(service._queued)
            finally:
                service.shutdown()
                service._worker.join(2)
        self.assertEqual(searches, [name])

    def test_duplicate_enqueue_runs_one_lookup(self) -> None:
        searches = 0

        def searcher(_: str) -> CoverMatch | None:
            nonlocal searches
            searches += 1
            return None

        service = CoverService(searcher=searcher, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(service.enqueue("Bocchi the Rock!"))
            self.assertFalse(service.enqueue("Bocchi the Rock!"))
            self.wait_for(service, lambda _: not service._queued)
            self.assertEqual(searches, 1)
        finally:
            service.shutdown()

    def test_lookup_failure_is_persisted_without_stopping_worker(self) -> None:
        def searcher(_: str) -> CoverMatch:
            raise CoverLookupError("network unavailable")

        service = CoverService(searcher=searcher, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(service.enqueue("Failure Test"))
            events = self.wait_for(service, lambda received: any(event[0] == "failed" for event in received))
            self.assertTrue(any("network unavailable" in event[2] for event in events if event[0] == "failed"))
            self.assertIn("Failure Test | Failed", self.log_text(service))
            store = IndexStore()
            try:
                self.assertEqual(store.cover_result(normalize_anime_name("Failure Test"))[3], "failed")
            finally:
                store.conn.close()
        finally:
            service.shutdown()

    def test_successful_search_and_download_logs_durations(self) -> None:
        def searcher(_: str) -> CoverMatch:
            time.sleep(0.01)
            return CoverMatch(99, "https://example.test/cover.jpg")

        def downloader(_: str, path: Path) -> None:
            time.sleep(0.01)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(JPEG_BYTES)

        service = CoverService(searcher=searcher, downloader=downloader, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(service.enqueue("Logged Anime"))
            self.wait_for(service, lambda events: any(event[0] == "ready" for event in events))
            log_text = self.log_text(service)
            self.assertIn("Logged Anime | Enqueued", log_text)
            self.assertIn("Logged Anime | Processing started | Queue wait:", log_text)
            self.assertIn("Logged Anime | Search started |", log_text)
            self.assertRegex(log_text, r"Logged Anime \| Match found \| Search: \d+ ms")
            self.assertRegex(log_text, r"Logged Anime \| Download completed \| Download: \d+ ms")
            self.assertRegex(log_text, r"Logged Anime \| Cover saved \| Total: \d+ ms")
        finally:
            service.shutdown()

    def test_no_match_and_blocked_results_are_logged(self) -> None:
        no_match_service = CoverService(searcher=lambda _: None, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(no_match_service.enqueue("No Match Anime"))
            self.wait_for(no_match_service, lambda _: not no_match_service._queued)
            self.assertRegex(self.log_text(no_match_service), r"No Match Anime \| No match \| Search: \d+ ms")
        finally:
            no_match_service.shutdown()

        def blocked(_: str) -> CoverMatch:
            raise ApiBlocked("Cloudflare blocked access")

        blocked_service = CoverService(searcher=blocked, cache_dir=self.cache_dir, request_interval=0)
        try:
            self.assertTrue(blocked_service.enqueue("Blocked Anime"))
            self.wait_for(blocked_service, lambda events: any(event[0] == "failed" for event in events))
            self.assertIn("Blocked Anime | Blocked", self.log_text(blocked_service))
        finally:
            blocked_service.shutdown()


class MatchingTests(unittest.TestCase):
    @staticmethod
    def candidate(number, romaji, synonyms=None, **titles):
        return {"id": number, "title": {"romaji": romaji, **titles}, "synonyms": synonyms or [], "coverImage": {"large": "https://example.test/cover.jpg"}}

    def test_code_geass_ambiguous_short_title(self):
        self.assertIsNone(select_conservative_match("Code Geass", [
            self.candidate(1, "Code Geass: Hangyaku no Lelouch"),
            self.candidate(2, "Code Geass: Hangyaku no Lelouch R2"),
        ]))
        self.assertIsNone(select_conservative_match("Code Geass", [
            self.candidate(1, "First", ["Code Geass"]), self.candidate(2, "Other", ["Code Geass"])
        ]))

    def test_joined_words_punctuation_and_unicode(self):
        item = self.candidate(1, "Yuusha Party wo Oidasareta Kiyou binbou")
        self.assertEqual(select_conservative_match("Yuusha Party wo Oidasareta Kiyoubinbou", [item]).anilist_id, 1)
        self.assertIsNotNone(select_conservative_match("Élite-Anime!", [self.candidate(1, "Elite Anime")]))
        self.assertIsNotNone(select_conservative_match("勇者", [self.candidate(1, "Yuusha", native="勇者")]))

    def test_game_center_abbreviation_requires_alias(self):
        name = "Game Center Shoujo to Ibunka Kouryuu"
        item = self.candidate(1, "GaCen Shoujo to Ibunka Kouryuu")
        self.assertIsNone(select_conservative_match(name, [item]))
        item["synonyms"] = [name]
        self.assertIsNotNone(select_conservative_match(name, [item]))

    def test_danmachi_alias_and_sequence_conflicts(self):
        base = self.candidate(1, "Dungeon ni Deai wo Motomeru no wa Machigatteiru Darou ka", ["Danmachi"])
        sequel = self.candidate(2, "Dungeon ni Deai wo Motomeru no wa Machigatteiru Darou ka II", ["Danmachi"])
        self.assertEqual(select_conservative_match("Danmachi", [base, sequel]).anilist_id, 1)
        for title in ("Danmachi 2", "Danmachi Part 2", "Danmachi Final", "Danmachi Movie"):
            self.assertIsNone(select_conservative_match("Danmachi", [self.candidate(2, title, ["Danmachi"])]))

    def test_similarity_threshold_margin_and_exact_priority(self):
        name = "Wonderful Adventure Academy"
        near = self.candidate(1, "Wonderful Adventures Academy")
        self.assertIsNotNone(select_conservative_match(name, [near]))
        self.assertIsNone(select_conservative_match(name, [near, self.candidate(2, "Wonderful Adventure Academyy")]))
        self.assertEqual(select_conservative_match(name, [near, self.candidate(3, name)]).anilist_id, 3)
        self.assertIsNone(select_conservative_match(name, [self.candidate(4, name + " Returns")]))


class FallbackSearchTests(unittest.TestCase):
    name = "Yuusha Party wo Oidasareta Kiyoubinbou"

    @staticmethod
    def response(media):
        return io.StringIO(json.dumps({"data": {"Page": {"media": media}}}))

    def test_zero_candidates_fallback_compares_original_title_and_logs_stages(self):
        from unittest.mock import Mock

        gate = Mock(return_value=True)
        candidate = MatchingTests.candidate(1, "Yuusha Party wo Oidasareta Kiyou binbou")
        with patch.dict(os.environ, {"EDI_COVER_DIAGNOSTICS": json.dumps([self.name])}), patch("indexer.covers.urlopen", side_effect=[self.response([]), self.response([candidate])]) as request, patch("indexer.covers.log_matching_diagnostic") as diagnostic:
            self.assertEqual(search_anilist(self.name, before_fallback=gate).anilist_id, 1)
            self.assertEqual(request.call_count, 2)
            self.assertEqual([json.loads(call.args[0].data)["variables"]["search"] for call in request.call_args_list],
                             [self.name, "Yuusha Party wo Oidasareta"])
            gate.assert_called_once()
            snapshots = [call.kwargs for call in diagnostic.call_args_list if call.args[1] == "candidate_snapshot"]
            self.assertEqual([snapshot["stage"] for snapshot in snapshots], ["primary", "fallback"])
            self.assertEqual([snapshot["count"] for snapshot in snapshots], [0, 1])

    def test_shortened_title_alone_is_not_enough_and_ambiguity_is_rejected(self):
        from unittest.mock import Mock

        for media in ([MatchingTests.candidate(1, "Yuusha Party wo Oidasareta")],
                      [MatchingTests.candidate(1, self.name), MatchingTests.candidate(2, self.name)]):
            with self.subTest(media=media), patch("indexer.covers.urlopen", side_effect=[self.response([]), self.response(media)]) as request:
                self.assertIsNone(search_anilist(self.name, before_fallback=Mock(return_value=True)))
                self.assertEqual(request.call_count, 2)

    def test_nonzero_primary_never_falls_back_even_when_rejected(self):
        from unittest.mock import Mock

        gate = Mock(return_value=True)
        with patch("indexer.covers.urlopen", return_value=self.response([MatchingTests.candidate(1, "Unrelated")])) as request:
            self.assertIsNone(search_anilist(self.name, before_fallback=gate))
            request.assert_called_once()
            gate.assert_not_called()

    def test_http_and_invalid_responses_never_trigger_fallback(self):
        from unittest.mock import Mock

        for code, exception in ((403, ApiBlocked), (429, RateLimited), (500, CoverLookupError)):
            gate = Mock(return_value=True)
            error = HTTPError("https://example.test", code, "error", {"Retry-After": "10"}, io.BytesIO(b"error"))
            with self.subTest(code=code), patch("indexer.covers.urlopen", side_effect=error) as request:
                with self.assertRaises(exception):
                    search_anilist(self.name, before_fallback=gate)
                request.assert_called_once()
                gate.assert_not_called()
        with patch("indexer.covers.urlopen", return_value=io.StringIO('{"data":{"Page":{"media":null}}}')) as request:
            with self.assertRaises(CoverLookupError):
                search_anilist(self.name)
            request.assert_called_once()

    def test_short_titles_and_cancelled_wait_do_not_request_fallback(self):
        self.assertIsNone(shortened_search_title("Code Geass"))
        for title in ("Code Geass", self.name):
            with patch("indexer.covers.urlopen", return_value=self.response([])) as request:
                self.assertIsNone(search_anilist(title, before_fallback=lambda: False))
                request.assert_called_once()


class _ControllerStore:
    def drives(self) -> list[tuple]:
        return []

    def folder_sources(self) -> list[tuple]:
        return []

    def source_preferences(self) -> dict:
        return {}

    def folder_exclusions(self) -> list[tuple]:
        return []


class _IndexerService:
    def __init__(self) -> None:
        self.store = _ControllerStore()
        self.connected = {}
        self.indexing = set()

    def refresh_folder_sources(self) -> None:
        pass


class _CoverService:
    def __init__(self) -> None:
        self.names: list[str] = []
        self._keys: set[str] = set()

    def enqueue(self, name: str) -> bool:
        key = normalize_anime_name(name)
        if key in self._keys:
            return False
        self._keys.add(key)
        self.names.append(name)
        return True

    def diagnostic_retry_available(self, name: str) -> bool:
        return False

    def process_events(self) -> list[tuple]:
        return []

    def shutdown(self) -> None:
        pass


class ControllerViewportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_visible_and_prefetched_items_are_enqueued_once(self) -> None:
        cover_service = _CoverService()
        with patch("ui.controller.IndexerService", return_value=_IndexerService()), patch(
            "ui.controller.CoverService", return_value=cover_service
        ):
            from ui.controller import AppController

            controller = AppController()
            controller._items = [
                {"name": f"Anime {number}", "isFolder": True, "coverUrl": "", "coverStatus": ""}
                for number in range(10)
            ]
            controller._items[2]["isFolder"] = False
            controller._items[3]["coverStatus"] = "no_match"
            controller.requestCoversForRange(0, 1)
            self.assertEqual(cover_service.names, ["Anime 0", "Anime 1", "Anime 4", "Anime 5"])
            controller.requestCoversForRange(4, 5)
            self.assertEqual(cover_service.names, ["Anime 0", "Anime 1", "Anime 4", "Anime 5", "Anime 6", "Anime 7", "Anime 8", "Anime 9"])
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
