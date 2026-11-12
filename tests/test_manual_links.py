"""Manual links: synthetic API/image results and isolated SQLite only."""
import json
import io
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import indexer.database as database
from indexer.database import IndexStore
from indexer.service import IndexerService
from indexer.covers import CoverService, CoverMatch, CoverLookupError, parse_anilist_url, folder_cover_key, lookup_anilist_id
from ui.controller import AppController

IMAGE = b"\xff\xd8\xff" + b"x" * 1100


class ManualLinksTests(unittest.TestCase):
    def test_title_flows_from_existing_response_without_extra_requests(self):
        from indexer.covers import search_anilist
        candidate = {"id": 7, "title": {"english": "Frieren: Beyond Journey's End", "romaji": "Sousou no Frieren", "native": "葬送のフリーレン"}, "coverImage": {"large": "https://example.test/cover"}}
        for lookup in (lambda: lookup_anilist_id(7), lambda: search_anilist("Sousou no Frieren")):
            response = io.StringIO(json.dumps({"data": {"Page": {"media": [candidate]}}}))
            with patch("indexer.covers.urlopen", return_value=response) as api:
                match = lookup()
            api.assert_called_once()
            self.assertEqual(match.title, "Frieren: Beyond Journey's End")
        for titles, expected in (({"english": " ", "romaji": "Romaji", "native": "原題"}, "Romaji"), ({"native": "原題"}, "原題"), ({}, None)):
            candidate["title"] = titles
            response = io.StringIO(json.dumps({"data": {"Page": {"media": [candidate]}}}))
            with patch("indexer.covers.urlopen", return_value=response):
                self.assertEqual(lookup_anilist_id(7).title, expected)

    def test_worker_title_persistence_replacement_failure_and_cached_repair(self):
        lookup = Mock(return_value=CoverMatch(7, "cover", "Frieren"))
        service = self.service(exact_lookup=lookup)
        service.link_folder(self.source, "BlueStacks", 7); self.wait(service)
        self.assertEqual(self.store.linked_anilist_title(self.source, "BlueStacks", "bluestacks"), "Frieren")
        lookup.assert_called_once_with(7)
        lookup.side_effect = CoverLookupError("failed replacement")
        service.link_folder(self.source, "BlueStacks", 8); self.wait(service, "manual_failed")
        self.assertEqual(self.store.linked_anilist_title(self.source, "BlueStacks", "bluestacks"), "Frieren")
        # Repair must retain the exact association's title without fetching metadata.
        service.link_folder(self.source, "BlueStacks", 7, repair=True); self.wait(service)
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(self.store.linked_anilist_title(self.source, "BlueStacks", "bluestacks"), "Frieren")
        automatic = self.service(searcher=Mock(return_value=CoverMatch(9, "auto", "Automatic title")))
        automatic.enqueue("Auto"); self.wait(automatic, "ready")
        self.assertEqual(self.store.linked_anilist_title(self.source, "Auto", "auto"), "Automatic title")
        automatic._searcher.assert_called_once_with("Auto")
        # An existing cached image/result is reused without searching to backfill.
        automatic._process_job("auto", "Auto", time.monotonic())
        self.assertEqual(automatic._searcher.call_count, 1)

    def setUp(self):
        test_root = Path(__file__).parent / ".manual-link-test-runtime"
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.original = database.APP_DIR, database.DB_PATH
        database.APP_DIR = Path(self.temp.name)
        database.DB_PATH = database.APP_DIR / "index.db"
        self.store = IndexStore()
        self.source, _, _, _ = self.store.register_folder_source(str(database.APP_DIR / "library"))
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
            service._worker.join(2)
        self.store.conn.close()
        for handler in list(logging.getLogger("external_disk_indexer.covers").handlers):
            if getattr(handler, "_cover_log_path", "").startswith(self.temp.name):
                logging.getLogger("external_disk_indexer.covers").removeHandler(handler)
                handler.close()
        database.APP_DIR, database.DB_PATH = self.original
        self.temp.cleanup()

    def service(self, **kwargs):
        def download(url, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(IMAGE)
        kwargs.setdefault("downloader", download)
        kwargs.setdefault("exact_lookup", lambda number: CoverMatch(number, "https://example.test/cover"))
        kwargs.setdefault("request_interval", 0)
        service = CoverService(**kwargs)
        self.services.append(service)
        return service

    def wait(self, service, kind="manual_ready"):
        until = time.monotonic() + 3
        while time.monotonic() < until:
            events = service.process_events()
            if any(event[0] == kind for event in events):
                return events
            time.sleep(0.01)
        self.fail("No " + kind)

    def test_url_validation_and_exact_query(self):
        self.assertEqual(parse_anilist_url("https://anilist.co/anime/101167/Title/"), 101167)
        for url in ("http://anilist.co/anime/1", "https://evil.test/anime/1", "https://anilist.co.evil.test/anime/1",
                    "https://user@anilist.co/anime/1", "https://anilist.co:443/anime/1", "https://anilist.co/manga/1",
                    "https://anilist.co/anime/0", "https://anilist.co/anime/-1", "https://anilist.co/anime/a", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_anilist_url(url)
        response = io.StringIO(json.dumps({"data": {"Page": {"media": [{"id": 7, "coverImage": {"large": "cover"}}]}}}))
        with patch("indexer.covers.urlopen", return_value=response) as api:
            self.assertEqual(lookup_anilist_id(7).anilist_id, 7)
            payload = json.loads(api.call_args.args[0].data)
            self.assertEqual(payload["variables"], {"id": 7})
            self.assertIn("id: $id", payload["query"])

    def test_links_are_folder_specific_persist_and_survive_reindex(self):
        service = self.service()
        for path, number in (("One/Anime", 7), ("Two/Anime", 8)):
            service.link_folder(self.source, path, number)
            self.wait(service)
        self.assertEqual(self.store.manual_links(self.source)["one\\anime"][0], 7)
        self.assertEqual(self.store.manual_links(self.source)["two\\anime"][0], 8)
        self.store.replace_drive(self.source, "Library", "ignored", [])
        reopened = IndexStore()
        try:
            self.assertEqual(len(reopened.manual_links(self.source)), 2)
        finally:
            reopened.conn.close()
        self.assertNotEqual(folder_cover_key(self.source, "One/Anime"), folder_cover_key("OTHER", "One/Anime"))

    def test_failed_replacement_keeps_previous_link_and_reuses_image(self):
        service = self.service()
        service.link_folder(self.source, "Anime", 7)
        self.wait(service)
        previous = self.store.manual_links(self.source)
        service._exact_lookup = Mock(side_effect=CoverLookupError("not found"))
        service.link_folder(self.source, "Anime", 8)
        self.wait(service, "manual_failed")
        self.assertEqual(self.store.manual_links(self.source), previous)
        service._exact_lookup = lambda number: CoverMatch(number, "cover")
        original_downloader = service._downloader
        service._downloader = Mock(side_effect=CoverLookupError("invalid image"))
        service.link_folder(self.source, "Anime", 8)
        self.wait(service, "manual_failed")
        self.assertEqual(self.store.manual_links(self.source), previous)
        service._downloader = original_downloader
        service._exact_lookup = lambda number: CoverMatch(number, "cover")
        service._downloader = Mock(side_effect=AssertionError("Already cached"))
        service.link_folder(self.source, "Other", 7)
        self.wait(service)
        service._downloader.assert_not_called()

    def test_pending_replacement_unlink_and_source_removal_cannot_resurrect_links(self):
        for action in ("replace", "unlink", "remove"):
            with self.subTest(action=action):
                started, release = threading.Event(), threading.Event()
                def lookup(number):
                    if number == 7:
                        started.set()
                        release.wait(2)
                    return CoverMatch(number, "cover")
                service = self.service(exact_lookup=lookup)
                service.link_folder(self.source, "Anime", 7)
                self.assertTrue(started.wait(2))
                if action == "replace":
                    service.link_folder(self.source, "Anime", 8)
                else:
                    service.invalidate_manual(self.source, "Anime" if action == "unlink" else None)
                    if action == "unlink":
                        self.store.delete_manual_link(self.source, "Anime")
                    else:
                        IndexerService(self.store).remove_source(self.source)
                release.set()
                if action == "replace":
                    self.wait(service)
                    self.assertEqual(self.store.manual_links(self.source)["anime"][0], 8)
                service.shutdown()
                service._worker.join(2)
                if action != "replace":
                    self.assertNotIn("anime", self.store.manual_links(self.source))

    def test_override_priority_repair_unlink_and_no_automatic_requests(self):
        service = self.service()
        service.link_folder(self.source, "Anime", 7)
        self.wait(service)
        self.store.replace_drive(self.source, "Library", "", [(self.source, "Anime", "", "Anime", 1, None, None)])
        auto = database.APP_DIR / "automatic.jpg"
        auto.write_bytes(IMAGE)
        self.store.save_cover_result("anime", 9, "cover", str(auto), "found")
        probe = SimpleNamespace(_queryFinished=Mock(), _cached_cover_url=AppController._cached_cover_url, cover_service=service)
        def query():
            AppController._query_items(probe, 1, self.source, "", "")
            return probe._queryFinished.emit.call_args.args[1]
        item = query()[0]
        self.assertEqual(item["manualId"], 7)
        self.assertNotEqual(item["coverUrl"], auto.as_uri())
        Path(self.store.manual_links(self.source)["anime"][2]).unlink()
        items = query()
        self.assertEqual(items[0]["coverUrl"], "")
        viewport = SimpleNamespace(currentView="source", _items=items, _unlinked_folders=set(), cover_service=service)
        # A new launch repairs from the persisted exact entry without a search.
        repair_service = self.service(exact_lookup=Mock(side_effect=AssertionError("Repair uses persisted URL")))
        viewport.cover_service = repair_service
        AppController.requestCoversForRange(viewport, 0, 0)
        self.wait(repair_service)
        unlink_probe = SimpleNamespace(cover_service=service, service=SimpleNamespace(store=self.store),
                                       _folder_search_target=None, _folder_candidate_links={},
                                       _manual_status_pending=set(),
                                       _unlinked_folders=set(), _selected_drive=self.source, _request_items=Mock(), _set_status=Mock())
        AppController.unlinkAniListEntry(unlink_probe, self.source, "Anime")
        items = query()
        self.assertEqual(items[0]["manualId"], 0)
        self.assertEqual(items[0]["coverUrl"], auto.as_uri())
        self.assertEqual(self.store.manual_links(self.source), {})
        self.store.conn.execute("DELETE FROM anime_cover_cache")
        self.store.conn.commit()
        placeholder_items = query()
        self.assertEqual(placeholder_items[0]["coverUrl"], "")
        viewport = SimpleNamespace(currentView="source", _items=placeholder_items, _unlinked_folders=unlink_probe._unlinked_folders, cover_service=Mock())
        AppController.requestCoversForRange(viewport, 0, 0)
        viewport.cover_service.enqueue.assert_not_called()

    def test_invalid_url_does_not_enqueue_or_change_an_existing_link(self):
        service = self.service()
        service.link_folder(self.source, "Anime", 7)
        self.wait(service)
        previous = self.store.manual_links(self.source)
        probe = SimpleNamespace(service=SimpleNamespace(store=self.store), cover_service=Mock(), _set_status=Mock(),
                                _folder_search_target=None, _folder_candidate_links={})
        AppController.linkAniListEntry(probe, self.source, "Anime", "https://other.test/anime/8")
        probe.cover_service.link_folder.assert_not_called()
        self.assertEqual(self.store.manual_links(self.source), previous)

    def test_source_removal_cleans_links_but_disconnection_and_reindex_do_not(self):
        service = self.service()
        service.link_folder(self.source, "Anime", 7)
        self.wait(service)
        image_path = Path(self.store.manual_links(self.source)["anime"][2])
        backend = IndexerService(self.store)
        self.assertIsNone(backend.source_root(self.source))  # unavailable root
        self.assertEqual(self.store.manual_links(self.source)["anime"][0], 7)
        service.invalidate_manual(self.source)
        backend.remove_source(self.source)
        self.assertEqual(self.store.manual_links(self.source), {})
        self.assertTrue(image_path.exists())
        same_id, _, _, _ = self.store.register_folder_source(str(database.APP_DIR / "library"))
        self.assertEqual(same_id, self.source)
        self.assertEqual(self.store.manual_links(same_id), {})

    def test_four_second_spacing_across_automatic_and_manual_jobs(self):
        clock, calls = [0.0], []
        done = threading.Event()
        def automatic(name):
            calls.append(("automatic", clock[0]))
            return None
        def exact(number):
            calls.append(("manual", clock[0]))
            done.set()
            return CoverMatch(number, "cover")
        def advance(seconds):
            clock[0] += seconds
            return False
        with patch("indexer.covers.time.monotonic", side_effect=lambda: clock[0]):
            service = self.service(searcher=automatic, exact_lookup=exact, request_interval=4)
            with patch.object(service._stopped, "wait", side_effect=advance):
                service.enqueue("Automatic Anime")
                service.link_folder(self.source, "Anime", 7)
                self.assertTrue(done.wait(2))
                service.shutdown()
                service._worker.join(2)
        self.assertEqual(calls, [("automatic", 0.0), ("manual", 4.0)])
