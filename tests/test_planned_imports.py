"""Isolated browser-inbox/SQLite/Qt tests; no real captures, API calls, or disk scans."""

import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import indexer.database as database
from indexer.database import IndexStore
from indexer.capture_format import CaptureError, MAX_FRAME_BYTES
from indexer.planned_imports import ImportReport, import_inbox


def sample(**changes):
    capture = {"version": 1, "type": "capture", "capture_id": str(uuid.uuid4()),
               "text": "  勇者 — ÉTÉ 🌸\nRe:Zero\t ", "captured_at": "2026-11-07T12:34:56.789Z"}
    return {**capture, **changes}


class PlannedImportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / ".planned-import-test-runtime"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.original = database.APP_DIR, database.DB_PATH
        database.APP_DIR = Path(self.temp.name).resolve() / "ExternalDiskIndexer"
        database.DB_PATH = database.APP_DIR / "index.db"
        self.inbox = database.APP_DIR / "imports" / "inbox"
        self.store = IndexStore()

    def tearDown(self):
        self.store.conn.close()
        database.APP_DIR, database.DB_PATH = self.original
        self.temp.cleanup()

    def write(self, capture=None, *, raw=None, filename=None):
        capture = capture or sample()
        self.inbox.mkdir(parents=True, exist_ok=True)
        path = self.inbox / (filename or capture["capture_id"] + ".json")
        path.write_bytes(raw if raw is not None else json.dumps(capture, ensure_ascii=False).encode("utf-8"))
        return path

    def receipt(self, capture):
        return self.store.conn.execute("SELECT planned_id,schema_version,capture_type,captured_text,captured_at FROM planned_import_receipts WHERE capture_id=?", (capture["capture_id"],)).fetchone()

    def test_unicode_exact_receipt_and_manual_entry_no_metadata(self):
        capture = sample()
        path = self.write(capture)
        report = import_inbox(self.store)
        self.assertEqual((report.imported, report.failed), (1, 0))
        entry = self.store.planned_entries()[0]
        self.assertEqual(entry["title"], capture["text"].strip())
        self.assertIsNone(entry["anilistId"])
        self.assertIsNone(entry["cachePath"])
        self.assertNotEqual(entry["id"], capture["capture_id"])
        self.assertEqual(self.receipt(capture), (entry["id"], 1, "capture", capture["text"], capture["captured_at"]))
        self.assertFalse(path.exists())
        self.assertIn("1 added", report.message())

    def test_restart_and_duplicate_uuid_do_not_duplicate(self):
        capture = sample()
        self.write(capture)
        import_inbox(self.store)
        self.store.conn.close()
        self.store = IndexStore()
        path = self.write(capture)
        report = import_inbox(self.store)
        self.assertEqual((report.imported, report.already_imported), (0, 1))
        self.assertEqual(len(self.store.planned_entries()), 1)
        self.assertFalse(path.exists())

    def test_receipts_survive_edit_deletion_and_do_not_resurrect(self):
        capture = sample()
        self.store.import_planned_capture(capture)
        entry_id = self.receipt(capture)[0]
        self.store.save_planned_title("My edited title", entry_id)
        self.assertFalse(self.store.import_planned_capture(capture))
        self.assertEqual(self.store.planned_entries()[0]["title"], "My edited title")
        self.store.remove_planned(entry_id)
        self.write(capture)
        self.assertEqual(import_inbox(self.store).already_imported, 1)
        self.assertEqual(self.store.planned_entries(), [])
        self.assertEqual(self.receipt(capture)[0], entry_id)

    def test_duplicate_titles_different_uuids_are_independent(self):
        for _ in range(2):
            self.write(sample(text="Same title"))
        self.assertEqual(import_inbox(self.store).imported, 2)
        self.assertEqual(len(self.store.planned_entries()), 2)

    def test_uuid_content_conflict_preserved_without_replacement(self):
        capture = sample()
        self.store.import_planned_capture(capture)
        path = self.write({**capture, "text": "Changed content"})
        report = import_inbox(self.store)
        self.assertEqual((report.failed, report.issues), (1, ["id_conflict"]))
        self.assertTrue(path.exists())
        self.assertEqual(self.store.planned_entries()[0]["title"], capture["text"].strip())

    def test_transaction_failure_rolls_back_entry_and_receipt_then_retries(self):
        capture = sample()
        path = self.write(capture)
        self.store.conn.executescript("CREATE TRIGGER stop_receipt BEFORE INSERT ON planned_import_receipts BEGIN SELECT RAISE(ABORT,'injected failure'); END;")
        self.assertEqual(import_inbox(self.store).failed, 1)
        self.assertEqual(self.store.planned_entries(), [])
        self.assertIsNone(self.receipt(capture))
        self.assertTrue(path.exists())
        self.store.conn.execute("DROP TRIGGER stop_receipt")
        self.assertEqual(import_inbox(self.store).imported, 1)
        self.assertFalse(path.exists())

    def test_commit_is_visible_before_cleanup(self):
        capture = sample()
        path = self.write(capture)
        actual_unlink = Path.unlink
        def checked_unlink(target, *args, **kwargs):
            if target == path:
                conn = sqlite3.connect(database.DB_PATH)
                try:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM planned_import_receipts").fetchone()[0], 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM planned_anime").fetchone()[0], 1)
                finally:
                    conn.close()
            return actual_unlink(target, *args, **kwargs)
        with patch.object(Path, "unlink", checked_unlink):
            self.assertEqual(import_inbox(self.store).imported, 1)

    def test_crash_after_commit_or_cleanup_failure_is_idempotent(self):
        capture = sample()
        path = self.write(capture)
        with patch.object(Path, "unlink", side_effect=PermissionError("injected cleanup failure")):
            report = import_inbox(self.store)
        self.assertEqual((report.imported, report.cleanup_pending), (1, 1))
        self.assertTrue(path.exists())
        self.store.conn.close()
        self.store = IndexStore()
        report = import_inbox(self.store)
        self.assertEqual((report.imported, report.already_imported), (0, 1))
        self.assertFalse(path.exists())
        self.assertEqual(len(self.store.planned_entries()), 1)

    def test_changed_file_not_removed_after_commit(self):
        capture = sample()
        path = self.write(capture)
        original = self.store.import_planned_capture
        def changed_after_commit(value):
            result = original(value)
            path.write_bytes(b"different payload after commit")
            return result
        with patch.object(self.store, "import_planned_capture", changed_after_commit):
            report = import_inbox(self.store)
        self.assertEqual((report.imported, report.cleanup_pending), (1, 1))
        self.assertEqual(path.read_bytes(), b"different payload after commit")

    def test_malformed_files_do_not_block_valid_and_tmp_is_ignored(self):
        malformed = [b"{", b"\xff", b"null", b'{"a":1,"a":2}', b"NaN"]
        for index, raw in enumerate(malformed):
            self.write(raw=raw, filename=f"broken-{index}.json")
        unfinished = self.write(raw=b"{", filename=".capture-interrupted.tmp")
        self.write()
        report = import_inbox(self.store)
        self.assertEqual((report.imported, report.failed), (1, len(malformed)))
        self.assertTrue(unfinished.exists())
        self.assertEqual(len(list(self.inbox.glob("*.json"))), len(malformed))

    def test_unsafe_schema_filename_size_and_controls(self):
        invalid = [sample(extra="unsupported"), sample(version=True), sample(capture_id="../../bad"),
                   sample(captured_at="2026-02-30T00:00:00Z"), sample(text="\x00"),
                   sample(text="a" * 4097), sample(text=" ")]
        for index, capture in enumerate(invalid):
            self.write(capture, filename=f"invalid-{index}.json")
        self.write(raw=b"x" * (MAX_FRAME_BYTES + 1), filename="oversized.json")
        self.write(sample(), filename=str(uuid.uuid4()) + ".json")  # Filename/payload mismatch.
        report = import_inbox(self.store)
        self.assertEqual(report.failed, len(invalid) + 2)
        self.assertEqual(self.store.planned_entries(), [])

    def test_json_directory_not_followed(self):
        directory = self.inbox / "not-a-file.json"
        directory.mkdir(parents=True)
        self.assertEqual(import_inbox(self.store).issues, ["unsafe_file"])
        self.assertTrue(directory.exists())

    def test_symlink_not_followed(self):
        target = self.write()
        link = self.inbox / (str(uuid.uuid4()) + ".json")
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlink creation not permitted on this Windows installation")
        report = import_inbox(self.store)
        self.assertEqual((report.imported, report.failed), (1, 1))
        self.assertTrue(link.is_symlink())

    def test_missing_inbox_no_notification_and_no_directory_creation(self):
        self.assertEqual(import_inbox(self.store).message(), "")
        self.assertFalse(self.inbox.exists())

    def test_prototype_captures_are_never_imported_implicitly(self):
        prototype = database.APP_DIR / "NativeMessagingPrototype" / "inbox"
        prototype.mkdir(parents=True)
        path = prototype / (str(uuid.uuid4()) + ".json")
        path.write_text(json.dumps(sample()), encoding="utf-8")
        self.assertEqual(import_inbox(self.store).imported, 0)
        self.assertTrue(path.exists())

    def test_reindex_and_source_removal_do_not_change_imports_or_receipts(self):
        from indexer.service import IndexerService
        capture = sample()
        self.store.import_planned_capture(capture)
        receipt = self.receipt(capture)
        source, label, path, _ = self.store.register_folder_source(str(database.APP_DIR / "missing-library"))
        self.store.replace_drive(source, label, path, [])
        IndexerService(self.store).remove_source(source)
        self.store.replace_drive("TEST_DRIVE", "Test", "X:", [])
        self.store.replace_drive("TEST_DRIVE", "Test", "X:", [])
        IndexerService(self.store).remove_source("TEST_DRIVE")
        self.assertEqual(self.receipt(capture), receipt)
        self.assertEqual(self.store.planned_entries()[0]["id"], receipt[0])

    def test_windows_reparse_file_rejected_before_opening(self):
        import stat
        from types import SimpleNamespace
        from indexer.planned_imports import read_capture_file
        if not hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
            self.skipTest("Windows reparse attribute check")
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, "lstat", return_value=info), patch("indexer.planned_imports.os.open") as opened:
            with self.assertRaises(CaptureError):
                read_capture_file(self.inbox / "linked.json")
            opened.assert_not_called()

    def test_concurrent_importers_one_receipt_and_entry(self):
        capture = sample()
        def attempt(_):
            store = IndexStore()
            try:
                return store.import_planned_capture(capture)
            finally:
                store.conn.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(8)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(self.store.planned_entries()), 1)

    def test_additive_migration_keeps_existing_planned_data(self):
        entry_id, _ = self.store.save_planned_title("Existing Planned entry")
        self.store.conn.execute("DROP TABLE planned_import_receipts")
        self.store.conn.commit()
        self.store.conn.close()
        self.store = IndexStore()
        self.assertEqual(self.store.planned_entries()[0]["id"], entry_id)
        self.assertTrue(self.store.import_planned_capture(sample()))
        self.assertEqual(len(self.store.planned_entries()), 2)

    def test_startup_import_and_gui_thread_notification_without_network(self):
        from PySide6.QtCore import QCoreApplication, QThread
        from indexer.service import IndexerService
        from ui.controller import AppController
        app = QCoreApplication.instance() or QCoreApplication([])
        self.write()
        cover = Mock()
        cover.process_events.return_value = []
        with patch("ui.controller.IndexerService", return_value=IndexerService(self.store)), patch("ui.controller.CoverService", return_value=cover):
            controller = AppController()
        controller.poll_drives = Mock()
        controller._request_items = Mock()
        controller.selectPlanned()
        controller._request_items.reset_mock()
        threads = []
        controller.captureImportMessageChanged.connect(lambda: threads.append(QThread.currentThread()))
        try:
            controller.start()
            controller._start_capture_import()  # Once per startup, even with duplicate calls.
            controller._capture_import_thread.join(5)
            self.assertFalse(controller._capture_import_thread.is_alive())
            app.processEvents()
            self.assertIn("1 added to Planned", controller.captureImportMessage)
            self.assertEqual(threads, [controller.thread()])
            controller._request_items.assert_called_once()
            cover.enqueue.assert_not_called()
            cover.link_planned.assert_not_called()
            controller.dismissCaptureImportMessage()
            self.assertEqual(controller.captureImportMessage, "")
            controller.shutdown()
            controller._apply_capture_imports(ImportReport(imported=2))
            self.assertEqual(controller.captureImportMessage, "")
        finally:
            controller.shutdown()

    @unittest.skipUnless(os.name == "nt", "Windows Native Messaging host")
    def test_native_delivery_to_production_then_import_and_redelivery(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prototypes" / "native_messaging"))
        import host
        import prepare_host
        import io
        root = database.APP_DIR / "NativeMessagingPrototype"
        prepare_host.prepare(root, Path(sys.executable), {"edge": "a" * 32}, inbox="production")
        old_capture = sample(text="Do not implicitly import prototype captures")
        host.save_capture(root / "inbox", old_capture)
        capture = sample()
        wire = io.BytesIO()
        host.write_message(wire, capture)
        environment = {**os.environ, "LOCALAPPDATA": str(database.APP_DIR.parent)}
        for expected_count in (1, 0):
            process = subprocess.run([sys.executable, "-I", "-u", str(root / "host.py"),
                                      "chrome-extension://" + "a" * 32 + "/"],
                                     input=wire.getvalue(), capture_output=True, env=environment,
                                     timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", "replace"))
            reply = host.read_message(io.BytesIO(process.stdout))
            self.assertEqual((reply["ok"], reply["inbox"]), (True, "production"))
            self.assertEqual(import_inbox(self.store).imported, expected_count)
            self.assertEqual(len(self.store.planned_entries()), 1)
        self.assertTrue((root / "inbox" / (old_capture["capture_id"] + ".json")).exists())


class ImportQMLTests(unittest.TestCase):
    def test_main_qml_load_and_import_status(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--qml-scenario"],
                                cwd=Path(__file__).resolve().parents[1], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Main QML startup import and notification passed", result.stdout)


def qml_scenario():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QEvent, QObject, QUrl, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
    from indexer.service import IndexerService
    from ui.controller import AppController

    root = Path("tests/.planned-import-test-runtime")
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        database.APP_DIR = Path(directory).resolve()
        database.DB_PATH = database.APP_DIR / "index.db"
        store = IndexStore()
        inbox = database.APP_DIR / "imports" / "inbox"
        inbox.mkdir(parents=True)
        capture = sample(text='<b>Literal imported title</b> 🌸')
        (inbox / (capture["capture_id"] + ".json")).write_text(json.dumps(capture), encoding="utf-8")
        cover = Mock()
        cover.process_events.return_value = []
        with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService", return_value=cover):
            app = QGuiApplication([])
            QQuickStyle.setStyle("Basic")
            controller = AppController()
        controller.poll_drives = Mock()  # No drive discovery/indexing.
        engine = QQmlApplicationEngine()
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
        engine.rootContext().setContextProperty("controller", controller)
        engine.load(QUrl.fromLocalFile(str(Path("ui/qml/Main.qml").resolve())))
        assert engine.rootObjects(), warnings
        window = engine.rootObjects()[0]
        controller.start()
        deadline = time.monotonic() + 5
        while not controller.captureImportMessage and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert "1 added to Planned" in controller.captureImportMessage
        assert window.findChild(QObject, "captureImportBanner") is None
        panel = window.findChild(QObject, "libraryStatusPanel")
        summary = window.findChild(QObject, "libraryStatusMessage")
        assert panel.property("visible") and panel.property("height") == 64
        assert summary.property("text") == controller.captureImportMessage
        from PySide6.QtQml import QQmlExpression
        from PySide6.QtTest import QTest
        def evaluate(code):
            expression = QQmlExpression(engine.contextForObject(window), window, code)
            result = expression.evaluate()
            assert not expression.hasError(), expression.error().toString()
            return result[0]
        assert evaluate("statusPlanned.visible && feedbackDismiss.visible")
        controller.dismissCaptureImportMessage(); app.processEvents()
        assert evaluate("libraryStatusPanel.idleReady && !feedbackDismiss.visible && !statusPlanned.visible")
        assert evaluate('libraryStatusPanel.border.color.toString()') == "#315d4c"
        assert evaluate('libraryStatusIndicator.color.toString()') == "#55d69b"
        controller._set_status("Loading…", mode="operation"); app.processEvents()
        assert not evaluate("libraryStatusPanel.idleReady")
        assert evaluate('libraryStatusPanel.border.color.toString()') == "#262c3e"
        assert evaluate('libraryStatusIndicator.color.toString()') == "#929bb0"
        controller._set_status("Completed operation", "success")
        app.processEvents()
        assert summary.property("text") == "Completed operation"
        assert evaluate('libraryStatusPanel.semanticColor.toString()') == "#55d69b"
        controller._expire_feedback(controller.notificationRevision, "success"); app.processEvents()
        assert controller.notificationMessage == "" and controller.status == "Ready"
        assert summary.property("text") == "Ready"
        controller._set_status("Warning operation", "warning"); app.processEvents()
        assert evaluate('libraryStatusPanel.semanticColor.toString()') == "#edc779"
        controller._set_status("Failed operation", "error"); app.processEvents()
        controller._expire_feedback(controller.notificationRevision, "error"); app.processEvents()
        assert summary.property("text") == "Failed operation"
        assert evaluate('libraryStatusPanel.semanticColor.toString()') == "#f0a0ad"
        dismiss = evaluate("feedbackDismiss")
        center = dismiss.mapToScene(dismiss.boundingRect().center()).toPoint()
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier, center)
        app.processEvents()
        assert controller.status == "Ready" and controller.notificationMessage == ""
        assert evaluate('libraryStatusPanel.border.color.toString()') == "#315d4c"
        # Repeat identical notifications without waiting for the suppression window.
        for severity, message in [("error", "Invalid URL"), ("error", "Invalid URL"),
                                  ("success", "Completed operation"), ("error", "Invalid URL"),
                                  ("warning", "Warning operation")] * 2:
            old = controller._status_revision
            controller._set_status(message, severity); QTest.qWait(30)
            assert summary.property("text") == message
            assert dismiss.property("visible"), (severity, message, controller.notificationMessage)
            current = controller.notificationRevision
            controller._expire_feedback(old, "success"); app.processEvents()
            assert dismiss.property("visible") and controller.notificationRevision == current
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                             dismiss.mapToScene(dismiss.boundingRect().center()).toPoint())
            app.processEvents()
            assert controller.status == "Ready" and not dismiss.property("visible")
        controller._set_status("Pressed warning", "warning"); app.processEvents()
        QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, center)
        controller._set_status("New error during press", "error"); app.processEvents()
        QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, center)
        assert controller.status == "New error during press"
        assert controller.notificationMessage == "New error during press"
        controller.dismissNotification(); app.processEvents()
        for _ in range(3):
            controller._apply_capture_imports(ImportReport(imported=2)); QTest.qWait(30)
            assert evaluate("statusPlanned.visible && feedbackDismiss.visible")
            assert summary.property("text") == controller.captureImportMessage
            QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                             dismiss.mapToScene(dismiss.boundingRect().center()).toPoint())
            app.processEvents()
            assert controller.status == "Ready" and not dismiss.property("visible")
        controller._apply_capture_imports(ImportReport(imported=2))
        QTest.qWait(60)
        planned_button = evaluate("statusPlanned")
        assert planned_button.property("visible")
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                         planned_button.mapToScene(planned_button.boundingRect().center()).toPoint())
        assert controller.currentView == "planned"
        deadline = time.monotonic() + 5
        while not controller.plannedEntries and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert controller.plannedEntries[0]["title"] == capture["text"]
        cover.enqueue.assert_not_called()
        cover.link_planned.assert_not_called()
        controller.dismissCaptureImportMessage()
        app.processEvents()
        assert panel.property("visible") and not dismiss.property("visible")
        # Reduced-size/long text: actions fit, body elides, panel never resizes.
        window.resize(900, 600)
        controller._set_status("Long message " * 200, "error"); QTest.qWait(60)
        assert panel.property("height") == 64
        assert summary.property("truncated")
        bounds = summary.mapRectToScene(summary.boundingRect())
        button_bounds = dismiss.mapRectToScene(dismiss.boundingRect())
        assert bounds.right() < button_bounds.left()
        assert button_bounds.right() <= window.width()
        controller._drive_detection_failure = "Persistent storage failure"
        controller.dismissNotification(); app.processEvents()
        assert summary.property("text") == "Persistent storage failure"
        assert not dismiss.property("visible")
        controller._set_status("Unrelated success", "success")
        controller._show_current_state(); app.processEvents()
        heading = window.findChild(QObject, "libraryStatusHeading")
        assert heading.property("text") == "Persistent storage failure"
        assert summary.property("text") == "Unrelated success"
        assert evaluate('libraryStatusPanel.border.color.toString()') == "#f0a0ad"
        controller._set_status("Persistent storage failure", "error")
        controller._expire_status(controller.notificationRevision); app.processEvents()
        assert heading.property("text") == "Library status"
        assert summary.property("text") == "Persistent storage failure"
        controller.shutdown()
        controller._capture_import_thread.join(5)
        window.close()
        window.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        del engine
        store.conn.close()
        # Also reproduced by loading git HEAD's Main.qml with a manual Planned
        # entry: Qt Basic menu warnings are pre-existing, not importer warnings.
        known = (
            "qrc:/qt-project.org/imports/QtQuick/Controls/Basic/MenuItem.qml:12: TypeError: Cannot read property 'max' of undefined",
            "qrc:/qt-project.org/imports/QtQuick/Controls/Basic/MenuItem.qml:12: TypeError: Cannot call method 'max' of undefined",
        )
        assert not [warning for warning in warnings if warning not in known], warnings
        print(f"Existing Qt Basic menu warnings: {len(warnings)}")
    print("Main QML startup import and notification passed")


if __name__ == "__main__" and "--qml-scenario" in sys.argv:
    qml_scenario()
