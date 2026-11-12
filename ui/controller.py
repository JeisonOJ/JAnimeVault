"""Qt-facing controller that adapts the indexer backend for QML."""

from __future__ import annotations

import os
import threading
import uuid
import time

from PySide6.QtCore import QUrl, QUrlQuery, QObject, Property, QTimer, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from indexer.covers import MATCHING_REVISION, CoverService, local_file_url, normalize_anime_name, valid_cached_image, log_matching_diagnostic, matching_diagnostics_enabled, parse_anilist_url, folder_cover_key
from indexer.database import IndexStore
from indexer.drives import POLL_SECONDS
from indexer.service import IndexerService
from indexer.planned_imports import ImportReport, import_inbox


class AppController(QObject):
    """Owns UI state while keeping drive/index operations in IndexerService."""

    drivesChanged = Signal()
    itemsChanged = Signal()
    plannedChanged = Signal()
    plannedBusyChanged = Signal()
    plannedSearchChanged = Signal()
    _plannedQueryFinished = Signal(int, object)
    statusChanged = Signal()
    notificationChanged = Signal()
    selectionChanged = Signal()
    indexingChanged = Signal()
    exclusionsChanged = Signal()
    _queryFinished = Signal(int, object)
    _eventsReady = Signal(object)
    _drivePollFinished = Signal(bool, str)
    coverReady = Signal(str, str)
    captureImportMessageChanged = Signal()
    importsBusyChanged = Signal()
    _captureImportsFinished = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.service = IndexerService()
        self.cover_service = CoverService()
        self._drives: list[dict] = []
        self._items: list[dict] = []
        self._planned_entries: list[dict] = []
        self._planned_pending: dict[str, int] = {}
        self._candidate_request = None
        self._folder_search_target = None
        self._folder_candidate_links = {}
        self._planned_search_state = {"entryId": "", "query": "", "busy": False, "results": [], "error": ""}
        self._cover_urls: dict[str, str] = {}
        self._unlinked_folders: set[str] = set()
        self._excluded_folders: list[dict] = []
        self._status = "Ready"
        self._status_mode = "idle"
        self._status_revision = 0
        self._drive_detection_failure = ""
        self._manual_status_pending = set()
        self._notification = ""
        self._notification_status_revision = -1
        self._notification_level = "success"
        self._notification_target = ""
        self._status_level = "info"
        self._notice_seen = {}
        self._notice_generation = 0
        self._link_notices = {}
        self._user_refresh = False
        self._selected_drive = ""
        self._current_view = "source"
        self._favorites_search = ""
        self._to_watch_search = ""
        self._planned_search = ""
        self._source_search = ""
        self._current_path = ""
        self._search_term = ""
        self._query_number = 0
        self._events_processing = False
        self._drive_poll_lock = threading.Lock()
        self._drive_poll_in_flight = False
        self._shutting_down = False
        self._capture_import_started = False
        self._capture_import_thread = None
        self._capture_import_running = False
        self._capture_import_manual = False
        self._queryFinished.connect(self._apply_query)
        self._plannedQueryFinished.connect(self._apply_planned_query)
        self._eventsReady.connect(self._handle_events)
        self._drivePollFinished.connect(self._handle_drive_poll_result)
        self._captureImportsFinished.connect(self._apply_capture_imports)
        self.selectionChanged.connect(self._check_folder_search_context)

        self._drive_timer = QTimer(self)
        self._drive_timer.setInterval(POLL_SECONDS * 1000)
        self._drive_timer.timeout.connect(self.poll_drives)
        self._event_timer = QTimer(self)
        self._event_timer.setInterval(150)
        self._event_timer.timeout.connect(self.process_backend_events)
        self._cover_event_timer = QTimer(self)
        self._cover_event_timer.setInterval(150)
        self._cover_event_timer.timeout.connect(self.process_cover_events)

        self._refresh_drives()

    @Property("QVariantList", notify=drivesChanged)
    def drives(self) -> list[dict]:
        return self._drives

    @Property("QVariantList", notify=itemsChanged)
    def items(self) -> list[dict]:
        return self._items

    @Property("QVariantList", notify=plannedChanged)
    def plannedEntries(self) -> list[dict]:
        return self._planned_entries

    @Property(bool, notify=plannedBusyChanged)
    def plannedBusy(self) -> bool:
        return bool(self._planned_pending)

    @Property("QVariantMap", notify=plannedSearchChanged)
    def plannedSearch(self):
        return self._planned_search_state

    def _planned_entry_exists(self, entry_id):
        return bool(self.service.store.conn.execute("SELECT 1 FROM planned_anime WHERE id=?", (entry_id,)).fetchone())

    def _folder_search_context(self):
        return (self.currentView, self.selectedDrive, self.currentPath, self.searchTerm)

    def _folder_target_valid(self, target):
        source, path, context = target
        return (context == self._folder_search_context()
                and self.service.store.manual_source_active(source)
                and bool(self.service.store.conn.execute(
                    "SELECT 1 FROM entries WHERE drive_id=? AND path=? AND is_folder=1", (source, path)).fetchone()))

    def _check_folder_search_context(self):
        cancelled_link = False
        if self._folder_search_target and not self._folder_target_valid(self._folder_search_target):
            self.cancelPlannedSearch()
        for key, target in list(self._folder_candidate_links.items()):
            if not self._folder_target_valid(target):
                self.cover_service.invalidate_manual(*key)
                self._folder_candidate_links.pop(key, None)
                self._manual_status_pending.discard(key)
                self._link_notices.pop(key, None)
                cancelled_link = True
        if cancelled_link and self._status_mode == "operation":
            self._show_current_state()

    @Slot(str, str)
    def beginFolderSearch(self, source_id, path):
        self.cancelPlannedSearch()
        if self.currentView == "planned": return
        try:
            key = self.service.store.manual_folder_path(path)
        except ValueError:
            return
        row = next((row for row in self.service.store.conn.execute(
            "SELECT path,name FROM entries WHERE drive_id=? AND is_folder=1", (source_id,))
            if self.service.store.manual_folder_path(row[0]) == key), None)
        if not row: return
        target = (source_id, row[0], self._folder_search_context())
        if not self._folder_target_valid(target): return
        if (source_id, row[0]) in self._folder_candidate_links:
            self.cover_service.invalidate_manual(source_id, row[0])
            self._folder_candidate_links.pop((source_id, row[0]), None)
            self._manual_status_pending.discard((source_id, row[0]))
            self._link_notices.pop((source_id, row[0]), None)
        self._folder_search_target = target
        self._planned_search_state = {"entryId": folder_cover_key(source_id, row[0]), "query": row[1], "busy": False, "results": [], "error": ""}
        self.searchPlannedCandidates(row[1])

    def _candidate_target_valid(self):
        if self._folder_search_target:
            return self._folder_target_valid(self._folder_search_target)
        return self.currentView == "planned" and self._planned_entry_exists(self._planned_search_state["entryId"])

    @Slot()
    def cancelPlannedSearch(self):
        if self._candidate_request is None and not self._planned_search_state["entryId"]:
            return
        self.cover_service.cancel_planned_search()
        self._candidate_request = None
        self._folder_search_target = None
        self._planned_search_state = {"entryId": "", "query": "", "busy": False, "results": [], "error": ""}
        self.plannedSearchChanged.emit()

    @Slot(str)
    def beginPlannedSearch(self, entry_id):
        self.cancelPlannedSearch()
        if self.currentView != "planned": return
        row = self.service.store.conn.execute("SELECT title FROM planned_anime WHERE id=?", (entry_id,)).fetchone()
        if not row: return
        self._planned_search_state = {"entryId": entry_id, "query": row[0], "busy": False, "results": [], "error": ""}
        self.searchPlannedCandidates(row[0])

    @Slot(str)
    def searchPlannedCandidates(self, query):
        if self._planned_search_state["busy"] and query.strip() == self._planned_search_state["query"]:
            return
        entry = self._planned_search_state["entryId"]
        if not entry or not self._candidate_target_valid():
            self.cancelPlannedSearch(); return
        self.cover_service.cancel_planned_search()
        self._candidate_request = None
        query = query.strip()
        self._planned_search_state = {"entryId": entry, "query": query, "busy": False, "results": [], "error": ""}
        if not query or len(query) > 256:
            self._planned_search_state["error"] = "Enter a search query of 1–256 characters."
        else:
            token = self.cover_service.search_planned(entry, query)
            if token is None:
                self._planned_search_state["error"] = "AniList worker is unavailable."
            else:
                self._candidate_request = (entry, token)
                self._planned_search_state["busy"] = True
        self.plannedSearchChanged.emit()

    @Slot(int)
    def confirmPlannedCandidate(self, anime_id):
        state = self._planned_search_state
        if state["busy"] or not any(candidate["id"] == anime_id for candidate in state["results"]): return
        entry = state["entryId"]
        if not self._candidate_target_valid():
            self.cancelPlannedSearch(); return
        if self._folder_search_target:
            target = self._folder_search_target
            source, path, _ = target
            self.cancelPlannedSearch()
            self._link_anilist_entry(source, path, f"https://anilist.co/anime/{anime_id}/", require_indexed=True)
            if (source, path) in self._manual_status_pending:
                self._folder_candidate_links[(source, path)] = target
            return
        self.linkPlannedEntry(entry, f"https://anilist.co/anime/{anime_id}/")

    @Slot(str, str)
    def linkPlannedEntry(self, entry_id: str, url: str) -> None:
        if entry_id == self._planned_search_state["entryId"] and entry_id:
            self.cancelPlannedSearch()
        try:
            anime_id = parse_anilist_url(url)
        except ValueError as error:
            self._set_status(str(error), "error")
            return
        create = not entry_id
        if create:
            entry_id = str(uuid.uuid4())
        elif not any(row["id"] == entry_id for row in self.service.store.planned_entries()):
            self._set_status("This Planned entry no longer exists.", "error")
            return
        token = self.cover_service.link_planned(entry_id, anime_id, create=create)
        if token is not None:
            self._planned_pending[entry_id] = token
            self._notice_generation += 1
            self._link_notices[("planned", entry_id)] = self._notice_generation
            self.plannedBusyChanged.emit()
            self._set_status("Looking up Planned AniList entry… Existing data is kept until success.", mode="operation")

    @Slot(str)
    def unlinkPlannedEntry(self, entry_id: str) -> None:
        if entry_id == self._planned_search_state["entryId"]: self.cancelPlannedSearch()
        self.cover_service.invalidate_planned(entry_id)
        self._planned_pending.pop(entry_id, None)
        self._link_notices.pop(("planned", entry_id), None)
        self.plannedBusyChanged.emit()
        self.service.store.unlink_planned(entry_id)
        self._set_status("AniList entry unlinked. Original Planned title kept.", "success")
        if self.currentView == "planned":
            self._request_items()

    @Slot()
    def selectPlanned(self) -> None:
        entering = self.currentView != "planned"
        self._select_global_view("planned")
        if entering:
            self._run_capture_import()

    @Slot(str, str)
    def savePlannedTitle(self, entry_id: str, title: str) -> None:
        try:
            _, duplicate = self.service.store.save_planned_title(title, entry_id or None)
        except ValueError as error:
            self._set_status(str(error), "error")
            return
        self._set_status("Saved to Planned. Another entry has the same title; both were kept." if duplicate else "Planned entry saved.", "warning" if duplicate else "success")
        if self.currentView == "planned":
            self._request_items()

    @Slot(str)
    def removePlanned(self, entry_id: str) -> None:
        if entry_id == self._planned_search_state["entryId"]: self.cancelPlannedSearch()
        self.cover_service.invalidate_planned(entry_id)
        self._planned_pending.pop(entry_id, None)
        self._link_notices.pop(("planned", entry_id), None)
        self.plannedBusyChanged.emit()
        self.service.store.remove_planned(entry_id)
        self._set_status("Removed from Planned.", "success")
        if self.currentView == "planned":
            self._request_items()

    @Property("QVariantList", notify=exclusionsChanged)
    def excludedFolders(self) -> list[dict]:
        return self._excluded_folders

    @Slot(result="QVariantList")
    def refreshExcludedFolders(self) -> list[dict]:
        """Refresh the dialog snapshot from the existing SQLite-backed query."""
        self._refresh_exclusions()
        return self._excluded_folders

    @Property(str, notify=statusChanged)
    def status(self) -> str:
        return self._status

    @Property(str, notify=selectionChanged)
    def selectedDrive(self) -> str:
        return self._selected_drive

    @Property(str, notify=selectionChanged)
    def currentView(self) -> str:
        return self._current_view

    @Slot()
    def selectFavorites(self) -> None:
        self._select_global_view("favorites")

    @Slot()
    def selectToWatch(self) -> None:
        self._select_global_view("to_watch")

    @Slot()
    def selectLibrary(self) -> None:
        self._select_global_view("source")

    def _select_global_view(self, view: str) -> None:
        if self.currentView == view:
            return
        if self.currentView == "planned": self.cancelPlannedSearch()
        search_fields = {"source": "_source_search", "favorites": "_favorites_search", "to_watch": "_to_watch_search", "planned": "_planned_search"}
        setattr(self, search_fields[self.currentView], self._search_term)
        self._search_term = getattr(self, search_fields[view])
        self._current_view = view
        self.selectionChanged.emit()
        self._request_items()

    @Property(str, notify=selectionChanged)
    def currentPath(self) -> str:
        return self._current_path

    @Property(str, notify=selectionChanged)
    def searchTerm(self) -> str:
        return self._search_term

    @Property(bool, notify=indexingChanged)
    def isIndexing(self) -> bool:
        return bool(self.service.indexing)

    @Property(bool, notify=selectionChanged)
    def isSearching(self) -> bool:
        return bool(self._search_term)

    @Property(bool, notify=selectionChanged)
    def canNavigateUp(self) -> bool:
        return self.currentView == "source" and bool(self._current_path) and not self.isSearching

    @Property("QVariantList", notify=selectionChanged)
    def breadcrumbs(self) -> list[dict]:
        if self.currentView != "source":
            return []
        drive = next((item for item in self._drives if item["id"] == self._selected_drive), None)
        if not drive:
            return []
        crumbs = [{"label": drive["label"], "path": ""}]
        current = ""
        for part in self._current_path.split(os.sep):
            if part:
                current = os.path.join(current, part) if current else part
                crumbs.append({"label": part, "path": current})
        return crumbs

    @Slot()
    def start(self) -> None:
        self._start_capture_import()
        self.poll_drives()
        self._drive_timer.start()
        self._event_timer.start()
        self._cover_event_timer.start()

    @Property(str, notify=notificationChanged)
    def notificationMessage(self):
        return self._notification

    @Property(str, notify=notificationChanged)
    def notificationLevel(self):
        return self._notification_level

    @Property(str, notify=statusChanged)
    def statusLevel(self):
        return self._status_level

    @Property(str, notify=statusChanged)
    def statusMode(self):
        return self._status_mode

    @Property(str, notify=notificationChanged)
    def notificationTarget(self):
        return self._notification_target

    @Property(str, notify=captureImportMessageChanged)
    def captureImportLevel(self):
        return self._notification_level if self._notification_target == "planned" else "success"

    @Property(int, notify=notificationChanged)
    def notificationRevision(self):
        return self._notification_status_revision

    @Slot()
    @Slot(int)
    def dismissNotification(self, revision: int = -1):
        self._clear_notification(revision, acknowledge=True)

    @Slot(int)
    def expireNotification(self, revision: int):
        self._clear_notification(revision, acknowledge=False)

    def _clear_notification(self, revision: int, *, acknowledge: bool):
        if not self._notification or (revision >= 0 and revision != self._notification_status_revision):
            return
        status_revision = self._notification_status_revision
        self._notification = ""
        self._notification_status_revision = -1
        self._notification_target = ""
        if acknowledge:
            # The existing expiry guard checks revision, event lifetime and shutdown,
            # then restores actual work/failure state rather than assuming idle.
            self._expire_status(status_revision)
        self.notificationChanged.emit()
        self.captureImportMessageChanged.emit()

    @Slot()
    def refreshLibrary(self):
        self._user_refresh = True
        self.poll_drives()

    @Property(str, notify=captureImportMessageChanged)
    def captureImportMessage(self) -> str:
        # Compatibility accessors share the single notification; no separate inbox banner state.
        return self._notification if self._notification_target == "planned" else ""

    @Slot()
    def dismissCaptureImportMessage(self) -> None:
        if self._notification_target == "planned":
            self.dismissNotification(self.notificationRevision)

    def _start_capture_import(self) -> None:
        if self._capture_import_started or self._shutting_down:
            return
        self._capture_import_started = True
        self._run_capture_import()

    @Property(bool, notify=importsBusyChanged)
    def importsBusy(self) -> bool:
        return self._capture_import_running

    @Slot()
    def refreshImports(self) -> None:
        self._run_capture_import(manual=True)

    def _run_capture_import(self, *, manual: bool = False) -> None:
        if self._capture_import_running or self._shutting_down:
            return
        self._capture_import_running = True
        self._capture_import_manual = manual
        self.importsBusyChanged.emit()
        try:
            self._capture_import_thread = threading.Thread(target=self._import_captures_worker, daemon=True)
            self._capture_import_thread.start()
        except RuntimeError:
            self._apply_capture_imports(ImportReport(failed=1, issues=["worker_unavailable"]))

    def _import_captures_worker(self) -> None:
        store = None
        try:
            store = IndexStore()  # Private worker connection; never share UI transactions.
            report = import_inbox(store)
        except Exception:
            report = ImportReport(failed=1, issues=["import_unavailable"])
        finally:
            if store is not None:
                try:
                    store.conn.close()
                except Exception:
                    report.failed += 1
                    report.issues.append("connection_close_failed")
        if not self._shutting_down:
            try:
                self._captureImportsFinished.emit(report)
            except RuntimeError:
                pass  # QObject may be gone during application shutdown.

    @Slot(object)
    def _apply_capture_imports(self, report: ImportReport) -> None:
        manual = self._capture_import_manual
        self._capture_import_manual = False
        self._capture_import_running = False
        if self._shutting_down:
            return
        self.importsBusyChanged.emit()
        message = report.message() or ("No new imports." if manual else "")
        if not message:
            return
        level = "error" if report.failed else "warning" if report.cleanup_pending else "success"
        self._set_status(message, level, target="planned")
        if report.imported and self.currentView == "planned":
            self._request_items()

    @Slot()
    def poll_drives(self) -> None:
        """Start one non-blocking drive-detection check, if one is not running."""
        with self._drive_poll_lock:
            if self._shutting_down or self._drive_poll_in_flight:
                return
            self._drive_poll_in_flight = True
        try:
            threading.Thread(target=self._check_drives_worker, daemon=True).start()
        except RuntimeError as exc:
            with self._drive_poll_lock:
                self._drive_poll_in_flight = False
            self._drive_detection_failure = f"Drive detection failed: {exc}"
            self._set_status(self._drive_detection_failure, "error", mode="event" if self._user_refresh else "persistent", notify=self._user_refresh)
            self._user_refresh = False

    def _check_drives_worker(self) -> None:
        """Run the potentially slow Windows storage query outside the Qt GUI thread."""
        newly_seen = False
        error = ""
        try:
            newly_seen = self.service.check_drives()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
        finally:
            with self._drive_poll_lock:
                self._drive_poll_in_flight = False
                shutting_down = self._shutting_down
        if not shutting_down:
            try:
                self._drivePollFinished.emit(newly_seen, error)
            except RuntimeError:
                # The QObject can be deleted while a final daemon worker exits.
                pass

    @Slot(bool, str)
    def _handle_drive_poll_result(self, newly_seen: bool, error: str) -> None:
        """Apply a completed drive poll on the controller's Qt thread."""
        if self._shutting_down:
            return
        if error:
            self._drive_detection_failure = f"Drive detection failed: {error}"
            self._set_status(self._drive_detection_failure, "error", mode="event" if self._user_refresh else "persistent", notify=self._user_refresh)
        else:
            self._drive_detection_failure = ""
            if self._status_mode == "persistent":
                self._show_current_state()
        if not error and newly_seen:
            drive_names = [label for drive_id, (letter, label, _) in self.service.connected.items() if drive_id in self.service.indexing]
            self._set_status(f"Indexing {'; '.join(drive_names)}…" if drive_names else "New drive detected; indexing has started.", mode="operation")
            self.indexingChanged.emit()
        self._refresh_drives()
        if getattr(self, "_user_refresh", False) and not error and not any(d["connected"] for d in self._drives):
            self._set_status("No available library sources to refresh. Connect a drive or add an available folder.", "warning")
        self._user_refresh = False

    @Slot()
    def shutdown(self) -> None:
        """Stop scheduling work; daemon workers are allowed to finish without blocking exit."""
        self._drive_timer.stop()
        self._event_timer.stop()
        self._cover_event_timer.stop()
        self.cancelPlannedSearch()
        self.cover_service.shutdown()
        with self._drive_poll_lock:
            self._shutting_down = True

    @Slot(str)
    def selectDrive(self, drive_id: str) -> None:
        if self.currentView == "planned": self.cancelPlannedSearch()
        if drive_id == self._selected_drive and self.currentView == "source":
            return
        if self.currentView != "source":
            setattr(self, {"favorites": "_favorites_search", "to_watch": "_to_watch_search", "planned": "_planned_search"}[self.currentView], self._search_term)
            self._search_term = self._source_search if drive_id == self._selected_drive else ""
            self._current_view = "source"
            if drive_id == self._selected_drive:
                self.selectionChanged.emit()
                self._request_items()
                return
        self._selected_drive = drive_id
        self._current_path = ""
        self._search_term = ""
        self.selectionChanged.emit()
        self._request_items()

    @Slot(str)
    def openFolder(self, path: str) -> None:
        if not self._selected_drive:
            return
        self._current_path = path
        self._search_term = ""
        self.selectionChanged.emit()
        self._request_items()

    @Slot()
    def navigateUp(self) -> None:
        if not self.canNavigateUp:
            return
        self._current_path = os.path.dirname(self._current_path)
        self.selectionChanged.emit()
        self._request_items()

    @Slot(str)
    def navigateTo(self, path: str) -> None:
        self.openFolder(path)

    @Slot(str)
    def search(self, term: str) -> None:
        if not self._selected_drive and self.currentView == "source":
            return
        self._search_term = term.strip()
        self.selectionChanged.emit()
        self._request_items()

    @Slot()
    def clearSearch(self) -> None:
        if not self._selected_drive and self.currentView == "source":
            return
        self._search_term = ""
        self.selectionChanged.emit()
        self._request_items()

    @Slot()
    def reindexSelected(self) -> None:
        if self.currentView != "source" or not self._selected_drive:
            return
        if not self.service.reindex(self._selected_drive):
            self._set_status("Make this library source available before re-indexing it.", "warning")
            return
        source = next((item for item in self._drives if item["id"] == self._selected_drive), None)
        label = source["label"] if source else "library source"
        self._set_status(f"Indexing {label}…", mode="operation")
        self.indexingChanged.emit()
        self._refresh_drives()

    @staticmethod
    def _local_folder_path(folder_url: str) -> str:
        """Convert the FolderDialog URL to a native local filesystem path."""
        value = (folder_url or "").strip()
        if not value:
            return ""
        url = QUrl.fromUserInput(value)
        if not url.isLocalFile():
            return ""
        return os.path.normpath(url.toLocalFile())

    @Slot(str)
    def addFolderSource(self, folder_url: str) -> None:
        """Register a user-selected folder and index only that folder when available."""
        path = self._local_folder_path(folder_url)
        if not path:
            self._set_status("Choose a valid local folder.", "warning")
            return
        try:
            source_id, label, _, added, available = self.service.add_folder_source(path)
        except OSError as error:
            self._set_status(f"Could not add folder: {error}", "error")
            return
        self._refresh_drives()
        self.selectDrive(source_id)
        if not added:
            self._set_status(f"{label} is already in your library.")
        elif available:
            self._set_status(f"Indexing {label}…", mode="operation")
            self.indexingChanged.emit()
        else:
            self._set_status(f"{label} is unavailable. It remains saved in your library.", "warning")

    @Slot(str)
    def openSourceInExplorer(self, source_id: str) -> None:
        """Open the currently available source root without relying on stale drive letters."""
        root = self.service.source_root(source_id)
        if not root:
            self._set_status("Make this library source available before opening it.", "warning")
            return
        try:
            os.startfile(root)
        except OSError as error:
            self._set_status(f"Could not open this library source: {error}", "error")

    @Slot(str)
    def openFolderInExplorer(self, relative_path: str) -> None:
        AppController.openCardInExplorer(self, self._selected_drive, relative_path)

    @Slot(str, str)
    def openCardInExplorer(self, source_id: str, relative_path: str) -> None:
        root = self.service.source_root(source_id)
        if not root:
            self._set_status("Make this library source available before opening it.", "warning")
            return
        path = os.path.join(root, relative_path)
        if not os.path.isdir(path):
            self._set_status("This folder is no longer available.", "warning")
            return
        try:
            os.startfile(path)
        except OSError as error:
            self._set_status(f"Could not open this folder: {error}", "error")

    @Slot(str)
    def copyFolderName(self, name: str) -> None:
        if not name:
            return
        QGuiApplication.clipboard().setText(name)
        self._set_status("Folder name copied.")

    @staticmethod
    def web_search_url(name: str) -> QUrl:
        url = QUrl("https://www.google.com/search")
        query = QUrlQuery()
        query.addQueryItem("q", name)
        url.setQuery(query)
        return url

    @Slot(str)
    @Slot(str, str, str)
    def searchFolderOnWeb(self, name: str, source_id: str = "", path: str = "") -> None:
        if source_id and path:
            row = self.service.store.conn.execute(
                "SELECT name FROM entries WHERE drive_id=? AND path=? AND is_folder=1", (source_id, path)
            ).fetchone()
            if row:
                name = row[0]
                manual = self.service.store.manual_links(source_id).get(self.service.store.manual_folder_path(path))
                if manual or folder_cover_key(source_id, path) not in self._unlinked_folders:
                    name = self.service.store.linked_anilist_title(source_id, path, normalize_anime_name(name)) or name
        if not name:
            return
        if not QDesktopServices.openUrl(self.web_search_url(name)):
            self._set_status("Could not open the web search.", "error")

    @Slot(str)
    def excludeFolder(self, relative_path: str) -> None:
        self.excludeCardFolder(self._selected_drive, relative_path)

    @Slot(str, str)
    def excludeCardFolder(self, source_id: str, relative_path: str) -> None:
        if not source_id or not relative_path:
            return
        if not self.service.exclude_folder(source_id, relative_path):
            self._set_status("Could not exclude this folder.", "error")
            return
        if source_id == self._selected_drive and (self._current_path == relative_path or self._current_path.startswith(relative_path + os.sep)):
            self._current_path = ""
            self.selectionChanged.emit()
        self._refresh_drives()
        self._request_items()
        self._set_status("Folder excluded from future indexing.", "success")

    @Slot(str, str)
    def restoreExcludedFolder(self, source_id: str, relative_path: str) -> None:
        if not self.service.restore_folder(source_id, relative_path):
            return
        self._refresh_exclusions()
        self._set_status("Folder restored. Re-index its library source to include it again.", "success")

    @Slot(str, str)
    def renameSource(self, source_id: str, label: str) -> None:
        if not self.service.rename_source(source_id, label):
            self._set_status("Choose a unique, non-empty library name.", "warning")
            return
        self._refresh_drives()
        self._set_status("Library source renamed.", "success")

    @Slot(str)
    def removeSource(self, source_id: str) -> None:
        source = next((item for item in self._drives if item["id"] == source_id), None)
        if not source:
            return
        self._manual_status_pending = {key for key in self._manual_status_pending if key[0] != source_id}
        self.cover_service.invalidate_manual(source_id)
        self.service.remove_source(source_id)
        if self._selected_drive == source_id:
            self._selected_drive = ""
            self._current_path = ""
            self._source_search = ""
            if self.currentView == "source":
                self._search_term = ""
                self._items = []
            self.selectionChanged.emit()
            self.itemsChanged.emit()
        self._refresh_drives()
        if self.currentView != "source":
            self._request_items()
        self._set_status(f"Removed {source['label']} from the library. Indexed records were kept.")

    @Slot(str, str, bool)
    def setFavorite(self, source_id: str, path: str, favorite: bool) -> None:
        if favorite and not self.service.store.manual_source_active(source_id):
            return
        self.service.store.set_favorite(source_id, path, favorite)
        self._request_items()

    @Slot(str, str, bool)
    def setToWatch(self, source_id: str, path: str, marked: bool) -> None:
        if marked and not self.service.store.manual_source_active(source_id):
            return
        self.service.store.set_to_watch(source_id, path, marked)
        self._request_items()

    @Slot(str, str, bool)
    def openCardItem(self, source_id: str, path: str, is_folder: bool) -> None:
        self.selectDrive(source_id)
        self.openItem(path, is_folder)

    @Slot(str, bool)
    def openItem(self, path: str, is_folder: bool) -> None:
        if is_folder:
            self.openFolder(path)
            return
        root = self.service.source_root(self._selected_drive)
        if not root:
            self._set_status("Make this library source available before opening this item.", "warning")
            return
        full_path = os.path.join(root, path)
        if os.path.exists(full_path):
            os.startfile(full_path)
        else:
            self._set_status("This item is no longer available on the connected drive.", "warning")

    @Slot()
    def process_backend_events(self) -> None:
        if self._events_processing:
            return
        self._events_processing = True
        threading.Thread(target=self._collect_events, daemon=True).start()

    def _collect_events(self) -> None:
        self._eventsReady.emit(self.service.process_events())

    @Slot()
    def process_cover_events(self) -> None:
        """Apply completed cover downloads without reloading the grid model."""
        self._check_folder_search_context()
        for event in self.cover_service.process_events():
            if event[0] == "planned_candidates":
                if self._candidate_request != (event[1], event[2]): continue
                if not self._candidate_target_valid():
                    self.cancelPlannedSearch(); continue
                self._candidate_request = None
                self._planned_search_state = {**self._planned_search_state, "busy": False, "results": event[3], "error": event[4]}
                self.plannedSearchChanged.emit()
                continue
            if event[0] in {"planned_ready", "planned_failed"}:
                if self._planned_pending.get(event[1]) != event[2]:
                    continue
                self._planned_pending.pop(event[1], None)
                self.plannedBusyChanged.emit()
                fresh = self._link_notices.pop(("planned", event[1]), None) == self._notice_generation
                self._set_status(event[3], ("success" if event[0] == "planned_ready" else "error") if fresh else None)
                if self.currentView == "planned":
                    self._request_items()
                continue
            if event[0] == "ready":
                _, key, url = event
                self._cover_urls[key] = url
                self.coverReady.emit(key, url)
            elif event[0] == "manual_ready":
                self._folder_candidate_links.pop((event[1], event[2]), None)
                self._manual_status_pending.discard((event[1], event[2]))
                if self._selected_drive == event[1] or self.currentView != "source":
                    self._request_items()
                fresh = self._link_notices.pop((event[1], event[2]), None) == self._notice_generation
                self._set_status("AniList entry linked.", "success" if fresh else None)
            elif event[0] == "manual_failed":
                self._folder_candidate_links.pop((event[1], event[2]), None)
                self._manual_status_pending.discard((event[1], event[2]))
                fresh = self._link_notices.pop((event[1], event[2]), None) == self._notice_generation
                self._set_status(f"AniList link unchanged: {event[3]}", "error" if fresh else None)

    @Slot(str, str, str)
    def linkAniListEntry(self, source_id: str, path: str, url: str) -> None:
        AppController._link_anilist_entry(self, source_id, path, url)

    def _link_anilist_entry(self, source_id, path, url, *, require_indexed=False):
        if self._folder_search_target and self._folder_search_target[:2] == (source_id, path):
            self.cancelPlannedSearch()
        self._folder_candidate_links.pop((source_id, path), None)
        try:
            anime_id = parse_anilist_url(url)
            if not self.service.store.manual_source_active(source_id):
                raise ValueError("Library source is no longer registered")
            folder_cover_key(source_id, path)
        except ValueError as error:
            self._set_status(str(error), "error")
            return
        kwargs = {"require_indexed": True} if require_indexed else {}
        if self.cover_service.link_folder(source_id, path, anime_id, **kwargs):
            self._manual_status_pending.add((source_id, path))
            self._notice_generation += 1
            self._link_notices[(source_id, path)] = self._notice_generation
            self._set_status("Linking AniList entry… The current cover is kept until the new cover is ready.", mode="operation")

    @Slot(str, str)
    def unlinkAniListEntry(self, source_id: str, path: str) -> None:
        if self._folder_search_target and self._folder_search_target[:2] == (source_id, path):
            self.cancelPlannedSearch()
        self._folder_candidate_links.pop((source_id, path), None)
        self._manual_status_pending.discard((source_id, path))
        self.cover_service.invalidate_manual(source_id, path)
        self.service.store.delete_manual_link(source_id, path)
        self._unlinked_folders.add(folder_cover_key(source_id, path))
        if self._selected_drive == source_id or self.currentView != "source":
            self._request_items()
        self._set_status("AniList entry unlinked.", "success")

    @Slot(object)
    def _handle_events(self, events: list[tuple]) -> None:
        self._events_processing = False
        for event in events:
            if event[0] == "progress":
                self._set_status(event[1], mode="operation")
            elif event[0] == "complete":
                _, drive_id, label, letter, count = event
                self._set_status(f"Finished indexing {label} ({letter}): {count:,} items")
                self._refresh_drives()
                self.indexingChanged.emit()
                if self._selected_drive == drive_id or self.currentView != "source":
                    self._request_items()
            elif event[0] == "error":
                self._set_status(event[1], "error" if not self._notification else None)
                self.indexingChanged.emit()

    def _refresh_drives(self) -> None:
        previous_sources = self._drives
        connected = self.service.connected
        self.service.refresh_folder_sources()
        folder_rows = self.service.store.folder_sources()
        preferences = self.service.store.source_preferences()
        folder_ids = {source_id for source_id, _, _, _, _ in folder_rows}
        stored_ids: set[str] = set()
        self._drives = [
            {
                "id": drive_id,
                "label": preferences.get(drive_id, (None, False))[0] or label,
                "letter": letter,
                "lastIndexed": last_indexed or "Not indexed yet",
                "fileCount": file_count,
                "connected": drive_id in connected,
                "indexing": drive_id in self.service.indexing,
                "kind": "drive",
                "sourcePath": "",
            }
            for drive_id, label, letter, last_indexed, file_count in self.service.store.drives()
            if not drive_id.startswith("folder:") and drive_id not in folder_ids and not preferences.get(drive_id, (None, False))[1]
        ]
        stored_ids.update(drive["id"] for drive in self._drives)
        for drive_id, (letter, label, _) in connected.items():
            if drive_id not in stored_ids and not preferences.get(drive_id, (None, False))[1]:
                self._drives.append(
                    {
                        "id": drive_id,
                        "label": preferences.get(drive_id, (None, False))[0] or label,
                        "letter": letter,
                        "lastIndexed": "Indexing…",
                        "fileCount": 0,
                        "connected": True,
                        "indexing": drive_id in self.service.indexing,
                        "kind": "drive",
                        "sourcePath": "",
                    }
                )
        for source_id, label, path, last_indexed, file_count in folder_rows:
            self._drives.append(
                {
                    "id": source_id,
                    "label": preferences.get(source_id, (None, False))[0] or label,
                    "letter": "",
                    "lastIndexed": last_indexed or "Not indexed yet",
                    "fileCount": file_count,
                    "connected": os.path.isdir(path),
                    "indexing": source_id in self.service.indexing,
                    "kind": "folder",
                    "sourcePath": path,
                }
            )
        self._refresh_exclusions()
        self.drivesChanged.emit()
        if self.currentView != "source" and previous_sources != self._drives:
            self._request_items()
        if self.currentView == "source" and not self._selected_drive and self._drives:
            self.selectDrive(self._drives[0]["id"])

    def _refresh_exclusions(self) -> None:
        labels = {source["id"]: source["label"] for source in self._drives}
        rows = self.service.store.folder_exclusions()
        self._excluded_folders = [
            {
                "sourceId": source_id,
                "sourceLabel": labels.get(source_id, source_id),
                "path": relative_path,
                "name": os.path.basename(relative_path),
            }
            for source_id, relative_path in rows
        ]
        self.exclusionsChanged.emit()

    def _request_items(self) -> None:
        self._query_number += 1
        query_number = self._query_number
        drive_id = self._selected_drive
        path = self._current_path
        term = self._search_term
        if not drive_id and self.currentView == "source":
            self._items = []
            self.itemsChanged.emit()
            return
        threading.Thread(target=self._query_items, args=(query_number, drive_id, path, term, self.currentView, list(self._drives)), daemon=True).start()

    def _query_items(self, query_number: int, drive_id: str, path: str, term: str, view: str = "source", sources: list | None = None) -> None:
        store = IndexStore()
        try:
            if view == "planned":
                entries = store.planned_entries(term)
                for entry in entries:
                    entry["localCoverUrl"] = local_file_url(entry["cachePath"]) if valid_cached_image(entry["cachePath"]) else ""
                self._plannedQueryFinished.emit(query_number, entries)
                return
            source_rows = (store.favorite_entries(term) if view == "favorites" else store.to_watch_entries(term)) if view != "source" else [
                (drive_id, *row) for row in (store.search(drive_id, term) if term else (store.children(drive_id, path) if path else store.root_entries(drive_id)))
            ]
            rows = [row[1:] for row in source_rows]
            cover_keys = list({normalize_anime_name(name) for _, name, is_folder, _ in rows if is_folder and normalize_anime_name(name)})
            cover_results = store.cover_results(cover_keys)
            manual_links = {source_id: store.manual_links(source_id) for source_id in {row[0] for row in source_rows}}
            favorites = store.favorite_keys()
            to_watch = store.to_watch_keys()
            source_info = {source["id"]: source for source in (sources or [])}
            for _, name, is_folder, _ in rows:
                if is_folder and matching_diagnostics_enabled(name):
                    cached = cover_results.get(normalize_anime_name(name))
                    stale = bool(cached and cached[3] == "no_match" and cached[4] < MATCHING_REVISION)
                    diagnostic_retry = bool(cached and cached[3] == "no_match" and self.cover_service.diagnostic_retry_available(name))
                    skipped = bool(cached and (cached[3] in {"no_match", "failed", "blocked"} and not stale
                                              and not diagnostic_retry or self._cached_cover_url(cached[:4])))
                    log_matching_diagnostic(name, "controller_cache", status=cached[3] if cached else None,
                                            revision=cached[4] if cached else None, required_revision=MATCHING_REVISION,
                                            lookup_skipped=skipped, legacy_no_match_eligible=stale, diagnostic_retry=diagnostic_retry)
            # Only legacy negative results become eligible for one new lookup.
            cover_results = {
                key: result[:3] + ("",) if result[3] == "no_match" and result[4] < MATCHING_REVISION else result[:4]
                for key, result in cover_results.items()
            }
            items = [
                {
                    "path": item_path,
                    "sourceId": source_id,
                    "sourceLabel": source_info.get(source_id, {}).get("label", source_id),
                    "sourceStatus": ("Available" if source_info.get(source_id, {}).get("connected") else "Unavailable") if source_id.startswith("folder:") else ("Connected" if source_info.get(source_id, {}).get("connected") else "Disconnected"),
                    "isFavorite": (source_id, store.manual_folder_path(item_path)) in favorites if is_folder else False,
                    "isToWatch": (source_id, store.manual_folder_path(item_path)) in to_watch if is_folder else False,
                    "folderKey": folder_cover_key(source_id, item_path) if is_folder else "",
                    "manualId": manual_links[source_id].get(store.manual_folder_path(item_path), (0,))[0] if is_folder else 0,
                    "name": name,
                    "isFolder": bool(is_folder),
                    "size": format_size(size),
                    "coverKey": normalize_anime_name(name) if is_folder else "",
                    "coverUrl": self._cached_cover_url(cover_results.get(normalize_anime_name(name))) if is_folder else "",
                    "coverStatus": (cover_results.get(normalize_anime_name(name)) or (None, None, None, ""))[3] if is_folder else "",
                }
                for source_id, item_path, name, is_folder, size in source_rows
            ]
            for item in items:
                if item["manualId"]:
                    manual = manual_links[item["sourceId"]][store.manual_folder_path(item["path"])]
                    item["coverUrl"] = local_file_url(manual[2]) if valid_cached_image(manual[2]) else ""
                    item["coverStatus"] = "manual"
            self._queryFinished.emit(query_number, items)
        finally:
            store.conn.close()

    @Slot(int, object)
    def _apply_query(self, query_number: int, items: list[dict]) -> None:
        if query_number != self._query_number:
            return
        self._items = items
        self._cover_urls.update({item["coverKey"]: item["coverUrl"] for item in items if item["coverKey"] and item["coverUrl"] and not item.get("manualId")})
        self.itemsChanged.emit()

    @Slot(int, object)
    def _apply_planned_query(self, query_number: int, entries: list[dict]) -> None:
        if query_number != self._query_number:
            return
        self._planned_entries = entries
        self.plannedChanged.emit()

    @Slot(int, int)
    def requestCoversForRange(self, first_visible: int, last_visible: int) -> None:
        """Queue only visible folder cards plus four upcoming cards for cover lookup."""
        if self.currentView == "planned":
            return
        if not self._items:
            return
        start = max(0, first_visible)
        end = min(len(self._items) - 1, max(start, last_visible) + 4)
        for item in self._items[start : end + 1]:
            if item.get("manualId"):
                if not item["coverUrl"]:
                    self.cover_service.link_folder(item["sourceId"], item["path"], item["manualId"], repair=True)
                continue
            if item.get("folderKey") in self._unlinked_folders:
                continue
            if not item["isFolder"] or item["coverUrl"]:
                if item["isFolder"]:
                    log_matching_diagnostic(item["name"], "controller_skip", reason="cached cover URL available")
                continue
            if item["coverStatus"] in {"no_match", "failed", "blocked"}:
                if item["coverStatus"] == "no_match" and self.cover_service.diagnostic_retry_available(item["name"]):
                    log_matching_diagnostic(item["name"], "controller_diagnostic_retry", reason="explicit one-time no_match bypass")
                else:
                    log_matching_diagnostic(item["name"], "controller_skip", reason="terminal cache status", status=item["coverStatus"])
                    continue
            self.cover_service.enqueue(item["name"])

    @Slot(str, result=str)
    def coverUrlFor(self, cover_key: str) -> str:
        return self._cover_urls.get(cover_key, "")

    @staticmethod
    def _cached_cover_url(result: tuple | None) -> str:
        if not result:
            return ""
        _, _, cache_path, status = result
        return local_file_url(cache_path) if status == "found" and valid_cached_image(cache_path) else ""

    def _show_current_state(self) -> None:
        if self.isIndexing:
            self._set_status("Indexing library…", mode="operation")
        elif self._planned_pending or self._manual_status_pending:
            self._set_status("Looking up AniList entry…", mode="operation")
        elif self._drive_detection_failure:
            self._set_status(self._drive_detection_failure, "error", mode="persistent", notify=False)
        else:
            self._set_status("Ready", mode="idle")

    def _expire_status(self, revision: int) -> None:
        if not self._shutting_down and revision == self._status_revision and self._status_mode == "event":
            self._show_current_state()

    def _expire_feedback(self, revision: int, severity: str | None) -> None:
        self._expire_status(revision)
        if severity != "error":
            self.expireNotification(revision)

    def _set_status(self, status: str, severity: str | None = None, *, mode: str = "event", notify: bool = True, target: str = "") -> None:
        # Lifetime is explicit and independent of severity or message wording.
        previous_mode = self._status_mode
        self._status_revision += 1
        self._status_mode = mode
        if mode == "event":
            revision = self._status_revision
            duration = 10000 if severity in {"warning", "error"} else 4500
            QTimer.singleShot(duration, self, lambda: self._expire_feedback(revision, severity))
        if self._status != status or self._status_level != (severity or "info") or previous_mode != mode:
            self._status = status
            self._status_level = severity or "info"
            self.statusChanged.emit()
        if not notify or severity not in {"success", "warning", "error"}:
            return
        self._notice_generation += 1
        now = time.monotonic()
        key = (severity, status)
        # Acknowledged/expired notifications must not suppress a new dismissible event.
        if self._notification and now - self._notice_seen.get(key, -float("inf")) < 30:
            return
        priorities = {"success": 0, "warning": 1, "error": 2}
        if self._notification and priorities[severity] < priorities[self._notification_level]:
            return
        self._notice_seen[key] = now
        if len(self._notice_seen) > 32:
            del self._notice_seen[next(iter(self._notice_seen))]
        self._notification = status
        self._notification_status_revision = self._status_revision
        self._notification_level = severity
        self._notification_target = target
        self.notificationChanged.emit()
        self.captureImportMessageChanged.emit()


def format_size(size: int | None) -> str:
    if size is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""
