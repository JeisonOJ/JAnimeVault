"""Focused tests for non-blocking controller drive polling."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication


class _Store:
    def archive_source_id(self):
        return None

    def drives(self) -> list[tuple]:
        return []

    def folder_sources(self) -> list[tuple]:
        return []

    def source_preferences(self) -> dict:
        return {}

    def folder_exclusions(self) -> list[tuple]:
        return []


class _Service:
    def __init__(self, check) -> None:
        self.store = _Store()
        self.connected: dict = {}
        self.indexing: set = set()
        self.check_drives = check

    def refresh_folder_sources(self) -> None:
        pass


class DrivePollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _wait_for(self, condition, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_poll_guard_prevents_overlapping_workers(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def slow_check() -> bool:
            nonlocal calls
            calls += 1
            started.set()
            release.wait(1)
            return False

        service = _Service(slow_check)
        with patch("ui.controller.IndexerService", return_value=service):
            from ui.controller import AppController

            controller = AppController()
            refreshes = 0

            def refreshed() -> None:
                nonlocal refreshes
                refreshes += 1

            controller._refresh_drives = refreshed
            controller.poll_drives()
            self.assertTrue(started.wait(1))
            controller.poll_drives()
            controller.poll_drives()
            self.assertEqual(calls, 1)
            release.set()
            self.assertTrue(self._wait_for(lambda: refreshes == 1))
            self.assertEqual(calls, 1)
            controller.shutdown()

    def test_poll_exception_returns_to_gui_thread_without_crashing(self) -> None:
        def failing_check() -> bool:
            raise RuntimeError("storage service unavailable")

        service = _Service(failing_check)
        with patch("ui.controller.IndexerService", return_value=service):
            from ui.controller import AppController

            controller = AppController()
            controller._refresh_drives = lambda: None
            controller.poll_drives()
            self.assertTrue(self._wait_for(lambda: "Drive detection failed" in controller.status))
            self.assertIn("storage service unavailable", controller.status)
            controller.shutdown()

    def test_shutdown_suppresses_a_late_worker_result(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def slow_check() -> bool:
            started.set()
            release.wait(1)
            return True

        service = _Service(slow_check)
        with patch("ui.controller.IndexerService", return_value=service):
            from ui.controller import AppController

            controller = AppController()
            refreshes = 0

            def refreshed() -> None:
                nonlocal refreshes
                refreshes += 1

            controller._refresh_drives = refreshed
            controller.poll_drives()
            self.assertTrue(started.wait(1))
            controller.shutdown()
            release.set()
            self._wait_for(lambda: not controller._drive_poll_in_flight)
            self.app.processEvents()
            self.assertEqual(refreshes, 0)


if __name__ == "__main__":
    unittest.main()
