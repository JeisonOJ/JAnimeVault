"""UI-independent orchestration of drive detection and indexing."""

from __future__ import annotations

import queue
import os
import threading

from .database import IndexStore
from .drives import connected_removable_drives
from .scanner import scan_drive


class IndexerService:
    def __init__(self, store: IndexStore | None = None) -> None:
        self.store = store or IndexStore()
        self.events: queue.Queue = queue.Queue()
        self.connected: dict[str, tuple[str, str, str]] = {}
        self.folder_sources: dict[str, tuple[str, str]] = {}
        self.indexing: set[str] = set()
        self.refresh_folder_sources()

    def refresh_folder_sources(self) -> None:
        self.folder_sources = {source_id: (label, path) for source_id, label, path, _, _ in self.store.folder_sources()}

    def add_folder_source(self, path: str) -> tuple[str, str, str, bool, bool]:
        """Register a folder and begin its first scan when it is available."""
        source_id, label, stored_path, added = self.store.register_folder_source(path)
        self.refresh_folder_sources()
        available = os.path.isdir(stored_path)
        if available and added:
            self.start_index(source_id, label, stored_path, stored_path)
        return source_id, label, stored_path, added, available

    def check_drives(self) -> bool:
        """Detect new connections and begin their index jobs. Returns whether any are new."""
        preferences = self.store.source_preferences()
        now = {
            drive_id: details
            for drive_id, details in connected_removable_drives().items()
            if not preferences.get(drive_id, (None, False))[1]
        }
        newly_seen = set(now) - set(self.connected)
        self.connected = now
        for drive_id in newly_seen:
            letter, label, root = now[drive_id]
            self.start_index(drive_id, label, letter, root)
        return bool(newly_seen)

    def start_index(self, drive_id: str, label: str, letter: str, root: str) -> None:
        if drive_id in self.indexing:
            return
        self.indexing.add(drive_id)
        exclusions = set(self.store.folder_exclusion_paths(drive_id))
        threading.Thread(
            target=scan_drive,
            args=(drive_id, label, letter, root, self.events, exclusions),
            daemon=True,
        ).start()

    def reindex(self, drive_id: str) -> bool:
        """Start a fresh scan for a connected indexed drive, if it is connected."""
        if drive_id not in self.connected:
            source = self.folder_sources.get(drive_id)
            if not source:
                return False
            label, root = source
            if not os.path.isdir(root):
                return False
            self.start_index(drive_id, label, root, root)
            return True
        letter, label, root = self.connected[drive_id]
        self.start_index(drive_id, label, letter, root)
        return True

    def source_root(self, source_id: str) -> str | None:
        if source_id in self.connected:
            return self.connected[source_id][2]
        source = self.folder_sources.get(source_id)
        if source and os.path.isdir(source[1]):
            return source[1]
        return None

    def remove_source(self, source_id: str) -> bool:
        """Unregister a manual folder or persistently exclude a physical drive."""
        self.store.delete_source_manual_links(source_id)
        self.store.delete_source_favorites(source_id)
        self.store.delete_source_to_watch(source_id)
        if source_id in self.folder_sources:
            self.store.remove_folder_source(source_id)
            self.refresh_folder_sources()
            return True
        self.store.exclude_drive(source_id)
        self.connected.pop(source_id, None)
        return True

    def rename_source(self, source_id: str, label: str) -> bool:
        return self.store.rename_source(source_id, label)

    def exclude_folder(self, source_id: str, relative_path: str) -> bool:
        return self.store.exclude_folder(source_id, relative_path)

    def restore_folder(self, source_id: str, relative_path: str) -> bool:
        return self.store.restore_folder(source_id, relative_path)

    def process_events(self) -> list[tuple]:
        """Persist finished scans and return progress/completion/error notifications."""
        notifications: list[tuple] = []
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "complete":
                    _, drive_id, label, letter, entries = event
                    self.store.replace_drive(drive_id, label, letter, entries)
                    self.indexing.discard(drive_id)
                    notifications.append(("complete", drive_id, label, letter, len(entries)))
                else:
                    notifications.append(event)
        except queue.Empty:
            pass
        return notifications
