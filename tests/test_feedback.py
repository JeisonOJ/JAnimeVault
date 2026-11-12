"""Explicit UI feedback metadata; isolated SQLite and mocked background services."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication
import indexer.database as db
from indexer.service import IndexerService
from indexer.planned_imports import ImportReport
from ui.controller import AppController


class FeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        root = Path("tests/.feedback-runtime"); root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.old = db.APP_DIR, db.DB_PATH
        db.APP_DIR = Path(self.temp.name); db.DB_PATH = db.APP_DIR / "index.db"
        self.store = db.IndexStore()
        self.cover = Mock(); self.cover.process_events.return_value = []
        with patch("ui.controller.IndexerService", return_value=IndexerService(self.store)), patch("ui.controller.CoverService", return_value=self.cover):
            self.controller = AppController()
        self.controller._request_items = Mock()

    def tearDown(self):
        self.controller.shutdown(); self.store.conn.close()
        db.APP_DIR, db.DB_PATH = self.old
        self.temp.cleanup()

    def test_levels_dismissal_deduplication_and_priority(self):
        c = self.controller
        c._set_status("Saved", "success")
        self.assertEqual((c.notificationMessage, c.notificationLevel), ("Saved", "success"))
        c.dismissNotification()
        self.assertEqual(c.status, "Ready")
        c._set_status("Saved", "success")
        self.assertEqual(c.notificationMessage, "Saved")
        c._set_status("Warning", "warning"); c._set_status("Error", "error")
        c._set_status("Later success", "success")
        self.assertEqual(c.notificationMessage, "Error")
        self.assertEqual(c.status, "Later success")
        for i in range(50): c._set_status(str(i), "error")
        self.assertLessEqual(len(c._notice_seen), 32)

    def test_invalid_url_and_refresh_without_sources(self):
        c = self.controller
        c.linkPlannedEntry("", "https://bad.test/")
        self.assertEqual(c.notificationLevel, "error")
        self.cover.link_planned.assert_not_called()
        c.dismissNotification(); c.poll_drives = Mock()
        c.refreshLibrary(); c.poll_drives.assert_called_once()
        c._handle_drive_poll_result(False, "")
        self.assertEqual(c.notificationLevel, "warning")
        self.assertIn("No available", c.notificationMessage)

    def test_routine_polls_and_import_do_not_duplicate_notices(self):
        c = self.controller
        for _ in range(3): c._handle_drive_poll_result(False, "")
        c._handle_drive_poll_result(False, "offline storage")
        self.assertEqual(c.notificationMessage, "")
        c._apply_capture_imports(ImportReport(imported=2))
        self.assertIn("2 added", c.captureImportMessage)
        self.assertEqual(c.notificationMessage, c.captureImportMessage)
        self.assertEqual(c.notificationTarget, "planned")
        c._apply_capture_imports(ImportReport(failed=1, issues=["file_error"]))
        self.assertEqual(c.captureImportLevel, "error")

    def test_old_link_result_cannot_overwrite_new_error(self):
        c = self.controller
        entry, _ = self.store.save_planned_title("Title")
        self.cover.link_planned.return_value = 7
        c.linkPlannedEntry(entry, "https://anilist.co/anime/7/")
        c._set_status("Newer error", "error")
        self.cover.process_events.return_value = [("planned_failed", entry, 7, "Older failure")]
        c.process_cover_events()
        self.assertEqual(c.notificationMessage, "Newer error")
        self.assertEqual(c.status, "Older failure")

    def test_idle_defaults_and_explicit_status_lifetimes(self):
        c = self.controller
        self.assertEqual((c.status, c.statusLevel), ("Ready", "info"))
        from PySide6.QtCore import QTimer
        schedule = QTimer.singleShot
        from PySide6.QtTest import QTest
        with patch("ui.controller.QTimer.singleShot", side_effect=lambda duration, context, callback: schedule(20, context, callback)) as timer:
            c.service.source_root = Mock(return_value=None)
            c.openCardInExplorer("DISCONNECTED", "Anime")
            self.assertEqual(c.statusLevel, "warning")
            self.assertEqual(timer.call_args.args[0], 10000)
            QTest.qWait(70)
            self.assertEqual((c.status, c.statusLevel), ("Ready", "info"))
            self.assertEqual(c.notificationMessage, "")
            c._set_status("Failed event", "error")
            QTest.qWait(70)
            self.assertEqual((c.status, c.statusLevel), ("Ready", "info"))
            self.assertEqual(c.notificationMessage, "Failed event")
            c.dismissNotification()
            c._set_status("Completed", "success")
            self.assertEqual(timer.call_args.args[0], 4500)
            QTest.qWait(70)
            self.assertEqual(c.status, "Ready")

    def test_older_timeout_and_banner_dismissal_do_not_clear_newer_status(self):
        c = self.controller
        c._set_status("Old event", "warning")
        old = c._status_revision
        old_notice = c.notificationRevision
        c._set_status("Newer event", "error")
        c._expire_status(old)
        c.dismissNotification(old_notice)
        self.assertEqual(c.status, "Newer event")
        self.assertEqual(c.notificationMessage, "Newer event")
        c._set_status("Loading…", mode="operation")
        c._expire_status(c._status_revision)
        self.assertEqual(c.status, "Loading…")

    def test_indexing_and_link_work_resume_after_event_expiry(self):
        c = self.controller
        c.service.indexing.add("TEST")
        c._set_status("Progress: scanning", mode="operation")
        c._expire_status(c._status_revision)
        self.assertEqual(c.status, "Progress: scanning")
        c._set_status("Completed unrelated action", "success")
        c._expire_status(c._status_revision)
        self.assertEqual(c.status, "Indexing library…")
        c.service.indexing.clear()
        c._planned_pending["entry"] = 8
        c._set_status("Temporary event", "warning")
        c._expire_status(c._status_revision)
        self.assertEqual(c.status, "Looking up AniList entry…")
        c._planned_pending.clear()
        c._set_status("Finished work")
        c._expire_status(c._status_revision)
        self.assertEqual(c.status, "Ready")

    def test_persistent_failure_survives_expiry_until_recovery_without_spam(self):
        c = self.controller
        c._handle_drive_poll_result(False, "storage service unavailable")
        c._expire_status(c._status_revision)
        self.assertIn("Drive detection failed", c.status)
        self.assertEqual(c.statusLevel, "error")
        self.assertEqual(c.notificationMessage, "")
        c._set_status("Transient event", "warning")
        c._expire_status(c._status_revision)
        self.assertIn("Drive detection failed", c.status)
        c._handle_drive_poll_result(False, "")
        self.assertEqual(c.status, "Ready")

    def test_acknowledgement_recovers_only_matching_event(self):
        c = self.controller
        for severity in ("success", "warning", "error"):
            c._set_status("Event " + severity, severity)
            revision = c.notificationRevision
            c.dismissNotification(revision)
            self.assertEqual((c.status, c.statusLevel), ("Ready", "info"))
            self.assertEqual(c.notificationMessage, "")
            c._set_status("New status", mode="operation")
            c._expire_status(revision)
            self.assertEqual(c.status, "New status")
        c._set_status("Older warning", "warning")
        token = c.notificationRevision
        c._set_status("New event without banner", notify=False)
        c.dismissNotification(token)
        self.assertEqual(c.status, "New event without banner")

    def test_acknowledgement_restores_work_and_persistent_failure(self):
        c = self.controller
        c.service.indexing.add("TEST")
        c._set_status("Warning during scan", "warning")
        c.dismissNotification(c.notificationRevision)
        self.assertEqual(c.status, "Indexing library…")
        c.service.indexing.clear()
        c._manual_status_pending.add(("TEST", "Anime"))
        c._set_status("Warning during link", "warning")
        c.dismissNotification()
        self.assertEqual(c.status, "Looking up AniList entry…")
        c._manual_status_pending.clear()
        c._drive_detection_failure = "Ongoing storage failure"
        c._set_status("Unrelated error", "error")
        c.dismissNotification()
        self.assertEqual((c.status, c.statusLevel), ("Ongoing storage failure", "error"))

    def test_automatic_expiry_and_already_recovered_status(self):
        c = self.controller
        c._set_status("Auto timeout", "success")
        revision = c.notificationRevision
        c.expireNotification(revision)
        self.assertEqual(c.status, "Auto timeout")
        c._expire_status(revision)
        self.assertEqual(c.status, "Ready")
        c._set_status("Persistent banner", "error")
        token = c.notificationRevision
        c._expire_status(token)
        ready_revision = c._status_revision
        c.dismissNotification(token)
        self.assertEqual(c._status_revision, ready_revision)
        self.assertEqual(c.status, "Ready")

    def test_import_uses_one_notification_and_dismissal_recovers_operations(self):
        c = self.controller
        c._apply_capture_imports(ImportReport(imported=2, already_imported=1))
        self.assertEqual(c.status, c.notificationMessage)
        self.assertEqual(c.captureImportMessage, c.notificationMessage)
        token = c.notificationRevision
        c.service.indexing.add("TEST")
        c.dismissNotification(token)
        self.assertEqual(c.status, "Indexing library…")
        self.assertEqual((c.notificationTarget, c.captureImportMessage), ("", ""))
        c._set_status("Library error", "error")
        c.dismissCaptureImportMessage()
        self.assertEqual(c.notificationMessage, "Library error")
        self.assertEqual(c.notificationTarget, "")

    def test_controller_expiry_replaces_removed_banner_timer(self):
        c = self.controller
        c._set_status("Success", "success")
        old = c.notificationRevision
        c._set_status("Warning", "warning")
        current = c.notificationRevision
        c._expire_feedback(old, "success")
        self.assertEqual(c.notificationMessage, "Warning")
        c._expire_feedback(current, "warning")
        self.assertEqual((c.notificationMessage, c.status), ("", "Ready"))
        c._set_status("Error", "error")
        current = c.notificationRevision
        c._expire_feedback(current, "error")
        self.assertEqual((c.notificationMessage, c.status), ("Error", "Ready"))
        c._set_status("Linking", mode="operation")
        revision = c._status_revision
        c.dismissNotification(current)
        self.assertEqual((c.status, c._status_revision), ("Linking", revision))

    def test_immediate_notifications_after_dismissal_and_stale_timers(self):
        c = self.controller
        # Reuse messages, including a success displayed before the first error.
        c._set_status("Saved", "success")
        for severity, message in [("error", "Invalid URL"), ("error", "Invalid URL"),
                                  ("success", "Saved"), ("error", "Invalid URL"),
                                  ("warning", "Unavailable")] * 3:
            old = c.notificationRevision
            c.dismissNotification(old)
            c._set_status(message, severity)
            current = c.notificationRevision
            self.assertEqual(c.notificationMessage, message)
            self.assertGreater(current, old)
            c._expire_feedback(old, "success")
            c.dismissNotification(old)
            self.assertEqual(c.notificationRevision, current)
            self.assertEqual(c.notificationMessage, message)
        c.dismissNotification()
        for _ in range(3):
            c._apply_capture_imports(ImportReport(imported=2))
            self.assertEqual(c.notificationTarget, "planned")
            self.assertEqual(c.captureImportMessage, c.notificationMessage)
            self.assertTrue(c.notificationMessage)
            c.dismissNotification()
            self.assertEqual(c.status, "Ready")
