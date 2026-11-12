"""SQLite persistence for external-drive indexes."""

from __future__ import annotations

import os
import sqlite3
import hashlib
import ntpath
import uuid
import unicodedata
from pathlib import Path

from .capture_format import CaptureError, validate_capture


APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "ExternalDiskIndexer"
DB_PATH = APP_DIR / "index.db"


class IndexStore:
    def __init__(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS drives (
              id TEXT PRIMARY KEY, label TEXT, letter TEXT,
              last_indexed TEXT, file_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS entries (
              drive_id TEXT NOT NULL, path TEXT NOT NULL,
              parent TEXT NOT NULL, name TEXT NOT NULL, is_folder INTEGER NOT NULL,
              size INTEGER, modified REAL,
              PRIMARY KEY (drive_id, path)
            );
            CREATE INDEX IF NOT EXISTS entries_parent ON entries(drive_id, parent);
            CREATE INDEX IF NOT EXISTS entries_name ON entries(drive_id, name);
            CREATE TABLE IF NOT EXISTS anime_cover_cache (
              normalized_name TEXT PRIMARY KEY,
              anilist_id INTEGER,
              cover_url TEXT,
              cache_path TEXT,
              status TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS folder_sources (
              id TEXT PRIMARY KEY,
              path TEXT NOT NULL,
              normalized_path TEXT NOT NULL UNIQUE,
              label TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS source_preferences (
              source_id TEXT PRIMARY KEY,
              custom_label TEXT,
              excluded INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS folder_exclusions (
              source_id TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              normalized_path TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              PRIMARY KEY (source_id, normalized_path)
            );
            CREATE TABLE IF NOT EXISTS folder_anilist_links (
              source_id TEXT NOT NULL, normalized_path TEXT NOT NULL,
              anilist_id INTEGER NOT NULL, cover_url TEXT NOT NULL, cache_path TEXT NOT NULL,
              PRIMARY KEY (source_id, normalized_path)
            );
            CREATE TABLE IF NOT EXISTS folder_favorites (
              source_id TEXT NOT NULL, normalized_path TEXT NOT NULL,
              PRIMARY KEY (source_id, normalized_path)
            );
            CREATE TABLE IF NOT EXISTS folder_to_watch (
              source_id TEXT NOT NULL, normalized_path TEXT NOT NULL,
              PRIMARY KEY (source_id, normalized_path)
            );
            CREATE TABLE IF NOT EXISTS planned_anime (
              id TEXT PRIMARY KEY, title TEXT NOT NULL,
              anilist_id INTEGER UNIQUE, anilist_title TEXT,
              cover_url TEXT, cache_path TEXT,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS planned_import_receipts (
              capture_id TEXT PRIMARY KEY, planned_id TEXT NOT NULL,
              schema_version INTEGER NOT NULL, capture_type TEXT NOT NULL,
              captured_text TEXT NOT NULL, captured_at TEXT NOT NULL,
              imported_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(anime_cover_cache)")}
        if "matching_revision" not in columns:
            self.conn.execute("ALTER TABLE anime_cover_cache ADD COLUMN matching_revision INTEGER NOT NULL DEFAULT 0")
        for table in ("anime_cover_cache", "folder_anilist_links"):
            columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if "anilist_title" not in columns:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN anilist_title TEXT")
        self.conn.commit()

    def replace_drive(self, drive_id: str, label: str, letter: str, entries: list[tuple]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE drive_id = ?", (drive_id,))
            self.conn.executemany(
                "INSERT INTO entries(drive_id,path,parent,name,is_folder,size,modified) VALUES(?,?,?,?,?,?,?)",
                entries,
            )
            self.conn.execute(
                "INSERT INTO drives(id,label,letter,last_indexed,file_count) VALUES(?,?,?,datetime('now','localtime'),?) "
                "ON CONFLICT(id) DO UPDATE SET label=excluded.label,letter=excluded.letter,last_indexed=excluded.last_indexed,file_count=excluded.file_count",
                (drive_id, label, letter, len(entries)),
            )

    def drives(self) -> list[tuple]:
        return self.conn.execute("SELECT id,label,letter,last_indexed,file_count FROM drives ORDER BY label").fetchall()

    @staticmethod
    def _planned_title_key(title: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", title).casefold().split())

    def planned_entries(self, term: str = "") -> list[dict]:
        rows = self.conn.execute("SELECT id,title,anilist_id,anilist_title,cover_url,cache_path,created_at FROM planned_anime ORDER BY created_at,id").fetchall()
        query = self._planned_title_key(term)
        return [dict(zip(("id", "title", "anilistId", "anilistTitle", "coverUrl", "cachePath", "createdAt"), row))
                for row in rows if query in self._planned_title_key(row[1])]

    def save_planned_title(self, title: str, entry_id: str | None = None) -> tuple[str, bool]:
        title = title.strip()
        if not title:
            raise ValueError("Enter a title for the Planned entry.")
        duplicate = any(row["id"] != entry_id and self._planned_title_key(row["title"]) == self._planned_title_key(title)
                        for row in self.planned_entries())
        with self.conn:
            if entry_id is None:
                entry_id = str(uuid.uuid4())
                self.conn.execute("INSERT INTO planned_anime(id,title) VALUES(?,?)", (entry_id, title))
            elif not self.conn.execute("UPDATE planned_anime SET title=? WHERE id=?", (title, entry_id)).rowcount:
                raise ValueError("This Planned entry no longer exists.")
        return entry_id, duplicate

    def remove_planned(self, entry_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM planned_anime WHERE id=?", (entry_id,))

    def import_planned_capture(self, capture: dict) -> bool:
        """Atomically import once. Receipts survive edits/deletion of Planned entries."""
        capture = validate_capture(capture)
        original = (capture["version"], capture["type"], capture["text"], capture["captured_at"])
        with self.conn:
            # Serialize the receipt check and both inserts across app instances.
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self.conn.execute(
                "SELECT schema_version,capture_type,captured_text,captured_at FROM planned_import_receipts WHERE capture_id=?",
                (capture["capture_id"],),
            ).fetchone()
            if existing is not None:
                if existing != original:
                    raise CaptureError("id_conflict", "Capture UUID conflicts with its existing import receipt.")
                return False
            entry_id = str(uuid.uuid4())
            # Do not call save_planned_title: its connection context would commit
            # before the receipt. Only the display title loses edge whitespace.
            self.conn.execute("INSERT INTO planned_anime(id,title) VALUES(?,?)", (entry_id, capture["text"].strip()))
            self.conn.execute(
                "INSERT INTO planned_import_receipts(capture_id,planned_id,schema_version,capture_type,captured_text,captured_at) VALUES(?,?,?,?,?,?)",
                (capture["capture_id"], entry_id, *original),
            )
        return True

    def link_planned(self, entry_id: str, anime_id: int, title: str, cover_url: str, cache_path: str, *, create: bool = False) -> None:
        try:
            with self.conn:
                if create:
                    self.conn.execute("INSERT INTO planned_anime(id,title,anilist_id,anilist_title,cover_url,cache_path) VALUES(?,?,?,?,?,?)",
                                      (entry_id, title, anime_id, title, cover_url, cache_path))
                elif not self.conn.execute(
                    "UPDATE planned_anime SET anilist_id=?,anilist_title=?,cover_url=?,cache_path=? WHERE id=?",
                    (anime_id, title, cover_url, cache_path, entry_id),
                ).rowcount:
                    raise ValueError("This Planned entry no longer exists.")
        except sqlite3.IntegrityError:
            raise ValueError("This AniList entry is already in Planned.") from None

    def unlink_planned(self, entry_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE planned_anime SET anilist_id=NULL,anilist_title=NULL,cover_url=NULL,cache_path=NULL WHERE id=?", (entry_id,))

    def root_entries(self, drive_id: str) -> list[tuple]:
        return self.conn.execute("SELECT path,name,is_folder,size FROM entries WHERE drive_id=? AND parent='' ORDER BY is_folder DESC,name COLLATE NOCASE", (drive_id,)).fetchall()

    def children(self, drive_id: str, parent: str) -> list[tuple]:
        return self.conn.execute("SELECT path,name,is_folder,size FROM entries WHERE drive_id=? AND parent=? ORDER BY is_folder DESC,name COLLATE NOCASE", (drive_id, parent)).fetchall()

    def search(self, drive_id: str, term: str) -> list[tuple]:
        return self.conn.execute("SELECT path,name,is_folder,size FROM entries WHERE drive_id=? AND name LIKE ? ORDER BY is_folder DESC,name COLLATE NOCASE LIMIT 1000", (drive_id, f"%{term}%")).fetchall()

    def cover_results(self, normalized_names: list[str]) -> dict[str, tuple]:
        if not normalized_names:
            return {}
        placeholders = ",".join("?" for _ in normalized_names)
        rows = self.conn.execute(
            f"SELECT normalized_name,anilist_id,cover_url,cache_path,status,matching_revision FROM anime_cover_cache WHERE normalized_name IN ({placeholders})",
            normalized_names,
        ).fetchall()
        return {row[0]: row[1:] for row in rows}

    def cover_result(self, normalized_name: str) -> tuple | None:
        return self.conn.execute(
            "SELECT anilist_id,cover_url,cache_path,status FROM anime_cover_cache WHERE normalized_name=?",
            (normalized_name,),
        ).fetchone()

    def save_cover_result(
        self,
        normalized_name: str,
        anilist_id: int | None,
        cover_url: str | None,
        cache_path: str | None,
        status: str,
        *,
        matching_revision: int = 0,
        title: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO anime_cover_cache(normalized_name,anilist_id,cover_url,cache_path,status,matching_revision,anilist_title,updated_at) "
                "VALUES(?,?,?,?,?,?,?,datetime('now','localtime')) "
                "ON CONFLICT(normalized_name) DO UPDATE SET "
                "anilist_id=excluded.anilist_id,cover_url=excluded.cover_url,cache_path=excluded.cache_path,"
                "status=excluded.status,matching_revision=excluded.matching_revision,anilist_title=excluded.anilist_title,updated_at=excluded.updated_at",
                (normalized_name, anilist_id, cover_url, cache_path, status, matching_revision, title),
            )

    def cover_matching_revision(self, normalized_name: str) -> int:
        row = self.conn.execute(
            "SELECT matching_revision FROM anime_cover_cache WHERE normalized_name=?", (normalized_name,)
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def manual_folder_path(path: str) -> str:
        value = ntpath.normpath(path)
        if not path or ntpath.isabs(value) or ntpath.splitdrive(value)[0] or value in {".", ".."} or value.startswith("..\\"):
            raise ValueError("Expected a source-relative folder path")
        return value.casefold()

    def manual_links(self, source_id: str) -> dict[str, tuple]:
        return {row[0]: row[1:] for row in self.conn.execute(
            "SELECT normalized_path,anilist_id,cover_url,cache_path FROM folder_anilist_links WHERE source_id=?", (source_id,)
        )}

    def linked_anilist_title(self, source_id: str, path: str, cover_key: str) -> str | None:
        manual = self.conn.execute(
            "SELECT anilist_id,anilist_title FROM folder_anilist_links WHERE source_id=? AND normalized_path=?",
            (source_id, self.manual_folder_path(path)),
        ).fetchone()
        # An explicit association with missing metadata must not borrow an automatic title.
        row = manual if manual is not None else self.conn.execute(
            "SELECT anilist_id,anilist_title FROM anime_cover_cache WHERE normalized_name=? AND status='found'",
            (cover_key,),
        ).fetchone()
        if row and type(row[0]) is int and row[0] > 0 and isinstance(row[1], str) and row[1].strip():
            return row[1].strip()
        return None

    def favorite_keys(self) -> set[tuple[str, str]]:
        return set(self.conn.execute("SELECT source_id,normalized_path FROM folder_favorites").fetchall())

    def set_favorite(self, source_id: str, path: str, favorite: bool) -> None:
        key = self.manual_folder_path(path)
        with self.conn:
            if favorite:
                self.conn.execute("INSERT OR IGNORE INTO folder_favorites VALUES(?,?)", (source_id, key))
            else:
                self.conn.execute("DELETE FROM folder_favorites WHERE source_id=? AND normalized_path=?", (source_id, key))

    def favorite_entries(self, term: str = "") -> list[tuple]:
        return self._marked_entries("folder_favorites", term)

    def to_watch_keys(self) -> set[tuple[str, str]]:
        return set(self.conn.execute("SELECT source_id,normalized_path FROM folder_to_watch").fetchall())

    def set_to_watch(self, source_id: str, path: str, marked: bool) -> None:
        with self.conn:
            if marked:
                self.conn.execute("INSERT OR IGNORE INTO folder_to_watch VALUES(?,?)", (source_id, self.manual_folder_path(path)))
            else:
                self.conn.execute("DELETE FROM folder_to_watch WHERE source_id=? AND normalized_path=?", (source_id, self.manual_folder_path(path)))

    def to_watch_entries(self, term: str = "") -> list[tuple]:
        return self._marked_entries("folder_to_watch", term)

    def delete_source_to_watch(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM folder_to_watch WHERE source_id=?", (source_id,))

    def _marked_entries(self, table: str, term: str) -> list[tuple]:
        assert table in {"folder_favorites", "folder_to_watch"}
        self.conn.create_function("folder_path_key", 1, self.manual_folder_path, deterministic=True)
        return self.conn.execute(
            f"SELECT e.drive_id,e.path,e.name,e.is_folder,e.size FROM {table} f "
            "JOIN entries e ON e.drive_id=f.source_id AND e.is_folder=1 AND folder_path_key(e.path)=f.normalized_path "
            "JOIN drives d ON d.id=e.drive_id LEFT JOIN folder_sources s ON s.id=d.id "
            "LEFT JOIN source_preferences p ON p.source_id=d.id "
            "WHERE COALESCE(p.excluded,0)=0 AND (substr(d.id,1,7)<>'folder:' OR s.id IS NOT NULL) "
            "AND e.name LIKE ? ORDER BY e.name COLLATE NOCASE,e.drive_id,e.path", (f"%{term}%",)
        ).fetchall()

    def delete_source_favorites(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM folder_favorites WHERE source_id=?", (source_id,))

    def save_manual_link(self, source_id: str, path: str, anime_id: int, url: str, cache_path: str, *, title: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO folder_anilist_links(source_id,normalized_path,anilist_id,cover_url,cache_path,anilist_title) VALUES(?,?,?,?,?,?) ON CONFLICT(source_id,normalized_path) "
                "DO UPDATE SET anilist_id=excluded.anilist_id,cover_url=excluded.cover_url,cache_path=excluded.cache_path,anilist_title=excluded.anilist_title",
                (source_id, self.manual_folder_path(path), anime_id, url, cache_path, title),
            )

    def delete_manual_link(self, source_id: str, path: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM folder_anilist_links WHERE source_id=? AND normalized_path=?",
                              (source_id, self.manual_folder_path(path)))

    def delete_source_manual_links(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM folder_anilist_links WHERE source_id=?", (source_id,))

    def manual_source_active(self, source_id: str) -> bool:
        if source_id.startswith("folder:"):
            return bool(self.conn.execute("SELECT 1 FROM folder_sources WHERE id=?", (source_id,)).fetchone())
        return bool(self.conn.execute("SELECT 1 FROM drives WHERE id=?", (source_id,)).fetchone()) and not self.source_preferences().get(source_id, (None, False))[1]

    @staticmethod
    def normalize_folder_path(path: str) -> str:
        """Create a Windows-safe comparison key without changing the stored path."""
        return os.path.normpath(os.path.abspath(path)).casefold()

    def register_folder_source(self, path: str, label: str | None = None) -> tuple[str, str, str, bool]:
        """Persist a source once; returns id, display label, stored path, and whether it is new."""
        stored_path = os.path.normpath(os.path.abspath(path))
        normalized_path = self.normalize_folder_path(stored_path)
        existing = self.conn.execute(
            "SELECT id,label,path FROM folder_sources WHERE normalized_path=?", (normalized_path,)
        ).fetchone()
        if existing:
            return existing[0], existing[1], existing[2], False
        display_label = (label or os.path.basename(stored_path.rstrip("\\/")) or stored_path).strip()
        source_id = "folder:" + hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:24]
        with self.conn:
            self.conn.execute(
                "INSERT INTO folder_sources(id,path,normalized_path,label) VALUES(?,?,?,?)",
                (source_id, stored_path, normalized_path, display_label),
            )
        return source_id, display_label, stored_path, True

    def folder_sources(self) -> list[tuple]:
        """Return persisted sources with any existing index metadata, without requiring availability."""
        return self.conn.execute(
            "SELECT sources.id,sources.label,sources.path,drives.last_indexed,COALESCE(drives.file_count,0) "
            "FROM folder_sources AS sources LEFT JOIN drives ON drives.id=sources.id "
            "ORDER BY sources.label COLLATE NOCASE"
        ).fetchall()

    def source_preferences(self) -> dict[str, tuple[str | None, bool]]:
        """Return display-name overrides and persistent external-drive exclusions."""
        rows = self.conn.execute("SELECT source_id,custom_label,excluded FROM source_preferences").fetchall()
        return {source_id: (label, bool(excluded)) for source_id, label, excluded in rows}

    def rename_source(self, source_id: str, label: str) -> bool:
        """Store a non-empty display-name override when it does not collide with another custom name."""
        display_label = label.strip()
        if not display_label:
            return False
        existing = self.conn.execute(
            "SELECT source_id FROM source_preferences WHERE custom_label=? COLLATE NOCASE AND source_id<>?",
            (display_label, source_id),
        ).fetchone()
        if existing:
            return False
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_preferences(source_id,custom_label,excluded) VALUES(?,?,0) "
                "ON CONFLICT(source_id) DO UPDATE SET custom_label=excluded.custom_label",
                (source_id, display_label),
            )
        return True

    def remove_folder_source(self, source_id: str) -> None:
        """Unregister a folder without touching its existing index records."""
        with self.conn:
            self.conn.execute("DELETE FROM folder_sources WHERE id=?", (source_id,))
            self.conn.execute("DELETE FROM source_preferences WHERE source_id=?", (source_id,))

    def exclude_drive(self, source_id: str) -> None:
        """Persistently hide an automatically detected drive without deleting its index."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_preferences(source_id,custom_label,excluded) VALUES(?,NULL,1) "
                "ON CONFLICT(source_id) DO UPDATE SET excluded=1",
                (source_id,),
            )

    def include_drive(self, source_id: str) -> None:
        """Allow a previously excluded physical drive to be detected again."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_preferences(source_id,custom_label,excluded) VALUES(?,NULL,0) "
                "ON CONFLICT(source_id) DO UPDATE SET excluded=0",
                (source_id,),
            )

    @staticmethod
    def normalize_relative_path(path: str) -> str:
        """Return a Windows-safe source-relative path comparison key."""
        normalized = os.path.normpath(path).strip("\\/")
        return "" if normalized == "." else normalized.casefold()

    def folder_exclusion_paths(self, source_id: str) -> list[str]:
        return [
            row[0]
            for row in self.conn.execute(
                "SELECT normalized_path FROM folder_exclusions WHERE source_id=? ORDER BY normalized_path",
                (source_id,),
            ).fetchall()
        ]

    def folder_exclusions(self) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT source_id,relative_path FROM folder_exclusions ORDER BY source_id,relative_path COLLATE NOCASE"
        ).fetchall()

    def exclude_folder(self, source_id: str, relative_path: str) -> bool:
        """Save a rule and remove only the folder's indexed subtree for this source."""
        stored_path = os.path.normpath(relative_path).strip("\\/")
        normalized_path = self.normalize_relative_path(stored_path)
        if not normalized_path:
            return False
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO folder_exclusions(source_id,relative_path,normalized_path) VALUES(?,?,?)",
                (source_id, stored_path, normalized_path),
            )
            paths = self.conn.execute("SELECT path FROM entries WHERE drive_id=?", (source_id,)).fetchall()
            matching = [
                (path,)
                for (path,) in paths
                if self._is_excluded_relative_path(path, normalized_path)
            ]
            self.conn.executemany("DELETE FROM entries WHERE drive_id=? AND path=?", [(source_id, path) for (path,) in matching])
            self.conn.execute(
                "UPDATE drives SET file_count=(SELECT COUNT(*) FROM entries WHERE drive_id=?) WHERE id=?",
                (source_id, source_id),
            )
        return True

    def restore_folder(self, source_id: str, relative_path: str) -> bool:
        normalized_path = self.normalize_relative_path(relative_path)
        if not normalized_path:
            return False
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM folder_exclusions WHERE source_id=? AND normalized_path=?",
                (source_id, normalized_path),
            )
        return bool(cursor.rowcount)

    @classmethod
    def _is_excluded_relative_path(cls, path: str, excluded_path: str) -> bool:
        normalized = cls.normalize_relative_path(path)
        separator = os.sep.casefold()
        return normalized == excluded_path or normalized.startswith(excluded_path + separator)
