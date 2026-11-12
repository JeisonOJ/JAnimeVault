"""Filesystem traversal for a single external drive."""

from __future__ import annotations

import os
import queue

BUILTIN_EXCLUDED_DIRECTORY_NAMES = frozenset({"$recycle.bin", "system volume information"})


def normalize_relative_path(path: str) -> str:
    normalized = os.path.normpath(path).strip("\\/")
    return "" if normalized == "." else normalized.casefold()


def is_excluded_path(path: str, excluded_paths: set[str]) -> bool:
    normalized = normalize_relative_path(path)
    separator = os.sep.casefold()
    return any(normalized == excluded or normalized.startswith(excluded + separator) for excluded in excluded_paths)


def scan_drive(
    drive_id: str, label: str, letter: str, root: str, events: queue.Queue, excluded_paths: set[str] | None = None
) -> None:
    entries: list[tuple] = []
    excluded_paths = excluded_paths or set()
    try:
        for current, dirs, files in os.walk(root, topdown=True, onerror=lambda _: None):
            rel_parent = os.path.relpath(current, root)
            parent = "" if rel_parent == "." else rel_parent
            included_dirs = []
            for folder in dirs:
                path = os.path.join(parent, folder) if parent else folder
                if folder.casefold() in BUILTIN_EXCLUDED_DIRECTORY_NAMES or is_excluded_path(path, excluded_paths):
                    continue
                entries.append((drive_id, path, parent, folder, 1, None, None))
                included_dirs.append(folder)
            dirs[:] = included_dirs
            for file in files:
                full_path = os.path.join(current, file)
                path = os.path.join(parent, file) if parent else file
                try:
                    stat = os.stat(full_path)
                    size, modified = stat.st_size, stat.st_mtime
                except OSError:
                    size = modified = None
                entries.append((drive_id, path, parent, file, 0, size, modified))
            if len(entries) % 500 == 0:
                events.put(("progress", f"Indexing {letter} — {len(entries):,} items found"))
        events.put(("complete", drive_id, label, letter, entries))
    except Exception as exc:
        events.put(("error", f"Could not index {letter}: {exc}"))
