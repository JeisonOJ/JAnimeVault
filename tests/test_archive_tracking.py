"""Synthetic saved indexes only: no controller, filesystem scan or external API."""
import ntpath
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import indexer.database as database
from indexer.archive_tracking import ArchiveTracker, compare_episodes, parse_episode, roots_overlap, title_key
from indexer.service import IndexerService


class EpisodeTests(unittest.TestCase):
    def test_supported_patterns_and_decimals(self):
        for name, expected in [('01.mkv', '1'), ('1.mp4', '1'), ('06.50.rar', '6.5'),
                               ('[Fansub] Anime 2 - 06v2 [1080p][HEVC][ABC12345].mkv', '6'),
                               ('Anime Episode 03.zip', '3'), ('EP06.5.7z', '6.5'),
                               ('Anime - 0007 [x265].webm', '7')]:
            with self.subTest(name=name):
                self.assertEqual(parse_episode(name).identifier, expected)
        result = parse_episode('Anime S02E06.mkv')
        self.assertEqual((result.identifier, result.season), ('6', 2))

    def test_ambiguous_files_never_guessed(self):
        for name in ['Anime 2026 1080p.mkv', '01-12.rar', 'Anime - 06 [01-12].rar',
                     'Anime - 06.part1.rar', 'Anime - 06.r00', 'Special 01.mkv',
                     'OVA 01.mp4', 'Anime - 06 07.mkv', 'readme.txt', '1080.mkv',
                     '2026.mkv', 'Anime S02E06 - 07.mkv', 'Anime [bad - 06.mkv',
                     'Anime - 01 + 02.mkv', 'Anime - 06 (Special).rar',
                     'Anime - 01 - 02.mkv', 'Anime - 06 [07].mkv', 'Anime - 06 [S03E07].mkv', 'Anime - 1080.mkv']:
            with self.subTest(name=name):
                self.assertIsNone(parse_episode(name).identifier)
                self.assertTrue(parse_episode(name).reason)

    def test_distinct_sets_and_original_names(self):
        result = compare_episodes(['01.mkv', '1.rar', '06.5.mp4', '07.mkv', 'pack.rar'],
                                  ['01.mp4', '02.mkv', '06.50.rar'])
        self.assertEqual(result['archived'], ['1', '2', '6.5'])
        self.assertEqual(result['pending'], ['7'])
        self.assertEqual(result['both'], ['1', '6.5'])
        self.assertEqual(result['temporary_files']['1'], ['01.mkv', '1.rar'])
        self.assertEqual(result['unknown']['temporary'][0]['filename'], 'pack.rar')

    def test_explicit_seasons_require_context(self):
        r = compare_episodes(['S02E01.mkv'], ['01.mkv'])
        self.assertEqual(r['pending'], [])
        self.assertTrue(r['unknown']['temporary'])
        r = compare_episodes(['S02E01.mkv'], ['01.mkv'], temporary_title='Anime Season 2')
        self.assertEqual(r['both'], ['1'])

    def test_normalization_and_overlap(self):
        self.assertEqual(title_key(' ＡＮＩＭＥ　 Season ２ '), title_key('Anime Season 2'))
        self.assertNotEqual(title_key('Anime 2'), title_key('Anime'))
        self.assertNotEqual(title_key('Anime II'), title_key('Anime 2'))
        self.assertNotEqual(title_key('Anime: Part 2'), title_key('Anime Part 2'))
        self.assertTrue(roots_overlap(r'D:\Anime', r'd:\anime\Season'))
        self.assertFalse(roots_overlap(r'D:\Anime', r'D:\AnimeExtra'))
        self.assertFalse(roots_overlap(r'D:\Anime', r'E:\Anime'))
        self.assertIsNone(roots_overlap(r'D:\Anime', None))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [patch.object(database, 'APP_DIR', root), patch.object(database, 'DB_PATH', root/'index.db')]
        for p in self.patches: p.start()
        self.store = database.IndexStore()
        self.temp_id = self.store.register_folder_source(r'D:\Temporary')[0]
        self.dest_id = self.store.register_folder_source(r'E:\Library')[0]
        self.seed(self.temp_id, ['Anime Season 2'], ['Anime Season 2/01.mkv', 'Anime Season 2/06.5.rar'])
        self.seed(self.dest_id, ['Collection', 'Collection/Anime Season 2'], ['Collection/Anime Season 2/01.mp4'])
        self.tracker = ArchiveTracker(self.store, {self.temp_id: {'connected': True}, self.dest_id: {'connected': False}})
        self.tracker.designate(self.temp_id)

    def tearDown(self):
        self.store.conn.close()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def seed(self, sid, folders, files=()):
        rows = []
        for values, is_folder in [(folders, 1), (files, 0)]:
            for value in values:
                value = value.replace('/', '\\')
                rows.append((sid, value, ntpath.dirname(value), ntpath.basename(value), is_folder, None, None))
        self.store.replace_drive(sid, 'Saved source', 'E:', rows)

    def confirm(self):
        self.tracker.confirm('ANIME SEASON 2', self.dest_id, 'collection/anime season 2')

    def test_suggestion_requires_explicit_confirmation(self):
        r = self.tracker.compare('Anime Season 2')
        self.assertEqual(r['state'], 'suggested')
        self.assertIsNone(r['comparison'])
        self.confirm()
        r = self.tracker.compare('Anime Season 2')
        self.assertEqual(r['state'], 'confirmed')
        self.assertEqual(r['comparison']['pending'], ['6.5'])
        self.assertEqual(r['comparison']['both'], ['1'])

    def test_persistence_reindex_switch_and_unlink(self):
        self.confirm()
        self.tracker.designate(None)
        self.tracker.designate(self.dest_id)
        self.tracker.designate(self.temp_id)
        self.seed(self.dest_id, ['Collection/Anime Season 2'], ['Collection/Anime Season 2/01.mp4'])
        other = database.IndexStore()
        try:
            reopened = ArchiveTracker(other)
            self.assertEqual(reopened.active_source(), self.temp_id)
            self.assertEqual(reopened.match('Anime Season 2')['state'], 'confirmed')
        finally: other.conn.close()
        self.tracker.unlink('anime season 2')
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'suggested')

    def test_missing_confirmed_destination_does_not_rematch(self):
        self.confirm()
        self.seed(self.dest_id, [])
        self.seed('12345678', ['Anime Season 2'])
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'invalid_destination')

    def test_ambiguous_and_candidate_limit(self):
        self.seed('12345678', ['Nested/Anime Season 2'])
        r = self.tracker.match('Anime Season 2')
        self.assertEqual(r['state'], 'multiple_candidates')
        self.assertEqual(len(r['candidates']), 2)
        r = self.tracker.match('Anime Season 2', limit=1)
        self.assertTrue(r['truncated'])
        self.assertEqual(r['state'], 'multiple_candidates')

    def test_season_and_unicode_names(self):
        self.seed(self.dest_id, ['Anime Season 3'])
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'no_match')
        self.seed(self.dest_id, ['Deep/ＡＮＩＭＥ　Ｓｅａｓｏｎ　２'])
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'suggested')

    def test_lifecycle_removes_both_sides_not_on_disconnect(self):
        self.confirm()
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'confirmed')
        self.store.remove_folder_source(self.dest_id)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM archive_folder_links').fetchone()[0], 0)
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'no_match')
        self.store.remove_folder_source(self.temp_id)
        self.assertIsNone(self.tracker.active_source())

    def test_drive_exclusion_and_service_removal(self):
        self.seed('12345678', ['Anime Season 2'])
        self.tracker.confirm('Anime Season 2', '12345678', 'Anime Season 2')
        self.store.exclude_drive('12345678')
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM archive_folder_links').fetchone()[0], 0)
        service = IndexerService(self.store)
        service.remove_source(self.temp_id)
        self.assertIsNone(self.tracker.active_source())

    def test_invalid_designations_and_confirmations(self):
        for sid in ['12345678', 'folder:missing', '']:
            with self.assertRaises(ValueError): self.tracker.designate(sid)
        for sid, path in [(self.temp_id, 'Anime Season 2'), ('missing', 'Anime'), (self.dest_id, '../Anime')]:
            with self.assertRaises(ValueError): self.tracker.confirm('Anime Season 2', sid, path)
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'suggested')

    def test_overlap_and_shared_root_boundary(self):
        overlap = self.store.register_folder_source(r'D:\Temporary\Nested')[0]
        self.seed(overlap, ['Anime Season 2'])
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'overlap_review')
        with self.assertRaises(ValueError): self.tracker.confirm('Anime Season 2', overlap, 'Anime Season 2')

    def test_drive_root_overlap_and_unknown_root(self):
        self.seed('12345678', ['Anime Season 2'])
        self.tracker.availability['12345678'] = {'connected': True, 'root': 'D:\\'}
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'overlap_review')
        self.tracker.availability['12345678'] = {'connected': False}
        self.tracker.confirm('Anime Season 2', '12345678', 'Anime Season 2')
        self.assertTrue(self.tracker.match('Anime Season 2')['overlap_unverified'])

    def test_freshness_even_when_connected(self):
        self.confirm()
        r = self.tracker.compare('Anime Season 2')
        self.assertTrue(r['temporary']['connected'])
        self.assertFalse(r['destination']['connected'])
        self.assertTrue(r['temporary']['potentially_stale'])
        self.assertTrue(r['destination']['potentially_stale'])
        self.assertTrue(r['destination']['last_indexed'])

    def test_nested_content_and_bounded_comparison(self):
        self.confirm()
        self.seed(self.dest_id, ['Collection/Anime Season 2', 'Collection/Anime Season 2/Extras'],
                  ['Collection/Anime Season 2/01.mkv', 'Collection/Anime Season 2/Extras/02.mkv'])
        r = self.tracker.compare('Anime Season 2')
        self.assertTrue(r['nested_content_review']['destination'])
        self.assertNotIn('2', r['comparison']['archived'])
        r = self.tracker.compare('Anime Season 2', file_limit=1)
        self.assertEqual(r['comparison_state'], 'incomplete_review')
        self.assertIsNone(r['comparison'])

    def test_pagination_and_immediate_only(self):
        self.seed(self.temp_id, ['A', 'B', 'B/Nested', 'C'])
        first = self.tracker.list_anime(limit=2)
        self.assertEqual([r['name'] for r in first['items']], ['A', 'B'])
        last = self.tracker.list_anime(limit=2, after_path=first['next_after'])
        self.assertEqual([r['name'] for r in last['items']], ['C'])
        with self.assertRaises(ValueError): self.tracker.match('B/Nested')

    def test_no_external_operations(self):
        self.confirm()
        with patch('os.walk', side_effect=AssertionError('scan')), \
             patch('os.stat', side_effect=AssertionError('probe')), \
             patch('subprocess.Popen', side_effect=AssertionError('process')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
             patch('builtins.open', side_effect=AssertionError('file IO')):
            self.tracker.list_anime()
            self.tracker.match('Anime Season 2')
            self.tracker.compare('Anime Season 2')

    def test_existing_data_and_migration(self):
        self.store.set_favorite(self.temp_id, 'Anime Season 2', True)
        self.store.set_to_watch(self.temp_id, 'Anime Season 2', True)
        entry, _ = self.store.save_planned_title('Future anime')
        self.store.conn.executescript('DROP TABLE archive_folder_links; DROP TABLE archive_tracking_settings;')
        reopened = database.IndexStore()
        try:
            t = ArchiveTracker(reopened)
            self.assertIsNone(t.active_source())
            self.assertEqual(len(reopened.favorite_keys()), 1)
            self.assertEqual(len(reopened.to_watch_keys()), 1)
            self.assertEqual(reopened.planned_entries()[0]['id'], entry)
            self.assertEqual(len(reopened.root_entries(self.temp_id)), 1)
        finally: reopened.conn.close()

    def test_source_rename_preserves_identity_and_mapping(self):
        self.confirm()
        self.store.rename_source(self.temp_id, 'This season')
        self.store.rename_source(self.dest_id, 'My archive')
        r = self.tracker.compare('Anime Season 2')
        self.assertEqual(r['state'], 'confirmed')
        self.assertEqual(r['destination']['label'], 'My archive')

    def test_excluded_folder_invalidates_mapping_without_deleting_it(self):
        self.confirm()
        self.store.exclude_folder(self.dest_id, 'Collection')
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'invalid_destination')
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM archive_folder_links').fetchone()[0], 1)

    def test_conflicting_explicit_folder_seasons_need_review(self):
        self.seed(self.dest_id, ['Anime Season 3'], ['Anime Season 3/01.mkv'])
        self.tracker.confirm('Anime Season 2', self.dest_id, 'Anime Season 3')
        r = self.tracker.compare('Anime Season 2')
        self.assertEqual(r['comparison_state'], 'season_conflict_review')
        self.assertIsNone(r['comparison'])

    def test_removed_or_never_indexed_source_is_ineligible(self):
        sid = self.store.register_folder_source(r'F:\NeverIndexed')[0]
        with self.assertRaises(ValueError): self.tracker.designate(sid)
        self.store.remove_folder_source(self.dest_id)
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'no_match')

    def test_absence_and_invalid_limits(self):
        for call in [lambda: self.tracker.list_anime(limit=0), lambda: self.tracker.match('Anime Season 2', limit=201),
                     lambda: self.tracker.compare('Anime Season 2', file_limit=20001)]:
            with self.assertRaises(ValueError): call()
        self.tracker.designate(None)
        self.assertEqual(self.tracker.list_anime()['items'], [])
        with self.assertRaises(ValueError): self.tracker.compare('Anime Season 2')

    def test_case_collision_is_not_confirmable(self):
        self.seed(self.dest_id, ['Anime Season 2', 'ANIME SEASON 2'])
        self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'multiple_candidates')
        with self.assertRaises(ValueError): self.tracker.confirm('Anime Season 2', self.dest_id, 'Anime Season 2')

    def test_read_snapshot_survives_concurrent_reindex(self):
        self.confirm()
        other = database.IndexStore()
        original = self.tracker._files
        calls = []
        def concurrent(source, path, limit):
            calls.append(source)
            if len(calls) == 1:
                other.replace_drive(self.dest_id, 'Replacement', 'E:', [])
            return original(source, path, limit)
        try:
            with patch.object(self.tracker, '_files', side_effect=concurrent):
                result = self.tracker.compare('Anime Season 2')
            self.assertEqual(result['comparison']['both'], ['1'])
            self.assertEqual(self.tracker.match('Anime Season 2')['state'], 'invalid_destination')
        finally: other.conn.close()

    def test_failed_designation_restores_previous_setting(self):
        self.store.conn.execute("CREATE TEMP TRIGGER reject_archive_setting BEFORE INSERT ON archive_tracking_settings "
                                "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.tracker.designate(self.dest_id)
        self.assertEqual(self.tracker.active_source(), self.temp_id)

    def test_concurrent_removal_cannot_publish_stale_confirmation(self):
        other = database.IndexStore()
        overlap = self.tracker._overlap
        def remove(*args):
            other.remove_folder_source(self.dest_id)
            return overlap(*args)
        try:
            with patch.object(self.tracker, '_overlap', side_effect=remove):
                with self.assertRaises(sqlite3.OperationalError): self.confirm()
            self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM archive_folder_links').fetchone()[0], 0)
        finally: other.conn.close()

    def test_manual_destination_search_is_bounded_and_excludes_self_overlap_marked(self):
        overlap = self.store.register_folder_source(r'D:\Temporary\Other')[0]
        self.seed(overlap, ['Anime Season 2'])
        result = self.tracker.search_destinations('ＡＮＩＭＥ')
        self.assertEqual(len(result['items']), 2)
        self.assertNotIn(self.temp_id, [r['source_id'] for r in result['items']])
        self.assertTrue(next(r for r in result['items'] if r['source_id'] == overlap)['overlap'])
        self.assertTrue(self.tracker.search_destinations('Anime', limit=1)['truncated'])
        self.assertEqual(self.tracker.search_destinations('%')['items'], [])

    def test_anime_search_and_keyset_page(self):
        self.seed(self.temp_id, ['Anime A', 'Anime B', 'Other'])
        first = self.tracker.list_anime(term='anime', limit=1)
        self.assertEqual(first['items'][0]['name'], 'Anime A')
        second = self.tracker.list_anime(term='anime', limit=1, after_path=first['next_after'])
        self.assertEqual(second['items'][0]['name'], 'Anime B')
        self.assertIsNone(second['next_after'])


if __name__ == '__main__':
    unittest.main()
