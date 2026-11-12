"""Explicit import of committed browser captures; no networking or disk indexing."""

from collections import Counter
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import sqlite3
import stat

from . import database
from .capture_format import CaptureError, MAX_FRAME_BYTES, decode_json, validate_capture

LOG = logging.getLogger(__name__)


@dataclass
class ImportReport:
    imported: int = 0
    already_imported: int = 0
    failed: int = 0
    cleanup_pending: int = 0
    issues: list[str] = field(default_factory=list)

    def message(self) -> str:
        if not (self.imported or self.already_imported or self.failed or self.cleanup_pending):
            return ""
        text = f"Browser captures: {self.imported} added to Planned; {self.already_imported} already imported; {self.failed} failed."
        if self.failed:
            counts = ", ".join(f"{code}: {count}" for code, count in sorted(Counter(self.issues).items()))
            text += f" Failed files kept in inbox ({counts})."
        if self.cleanup_pending:
            text += f" {self.cleanup_pending} committed file(s) kept for cleanup; retries will not duplicate entries."
        return text


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _not_reparse(info) -> bool:
    return not (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def read_capture_file(path: Path) -> tuple[dict, tuple]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not _not_reparse(info):
        raise CaptureError("unsafe_file", "Capture must be a regular, non-linked file.")
    if info.st_size > MAX_FRAME_BYTES:
        raise CaptureError("invalid_message", "Capture file exceeds the size limit.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(info):
            raise CaptureError("unsafe_file", "Capture changed while opening it.")
        raw = stream.read(MAX_FRAME_BYTES + 1)
    if len(raw) > MAX_FRAME_BYTES or _fingerprint(path.lstat()) != _fingerprint(info):
        raise CaptureError("invalid_message", "Capture is oversized or changed while reading.")
    capture = validate_capture(decode_json(raw))
    if path.name != capture["capture_id"] + ".json":
        raise CaptureError("invalid_message", "Capture filename must match its UUID.")
    return capture, _fingerprint(info)


def import_inbox(store: database.IndexStore, inbox: Path | None = None) -> ImportReport:
    """Only production inbox JSON files. Invalid/uncommitted files are never removed."""
    inbox = inbox if inbox is not None else database.APP_DIR / "imports" / "inbox"
    report = ImportReport()
    try:
        # Refuse linked inbox directories; no recursive traversal of captures.
        for directory in (inbox.parent, inbox):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or not _not_reparse(info):
                raise CaptureError("unsafe_inbox", "Inbox must be a regular directory.")
        paths = sorted(inbox.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return report
    except (OSError, CaptureError):
        report.failed = 1
        report.issues.append("inbox_unavailable")
        return report

    for path in paths:
        if path.suffix.lower() != ".json":
            continue  # Interrupted helper writes (.tmp) are not committed captures.
        try:
            capture, fingerprint = read_capture_file(path)
            if store.import_planned_capture(capture):
                report.imported += 1
            else:
                report.already_imported += 1
        except (CaptureError, OSError, sqlite3.Error) as error:
            code = error.code if isinstance(error, CaptureError) else "database_error" if isinstance(error, sqlite3.Error) else "file_error"
            report.failed += 1
            report.issues.append(code)
            LOG.warning("Browser capture not imported: %s (%s)", path.name, code)
            continue
        # The transaction has committed before this point, including duplicate
        # receipt verification. Failure here is recoverable on the next startup.
        try:
            if _fingerprint(path.lstat()) != fingerprint:
                raise OSError("Capture changed after import")
            path.unlink()
        except OSError:
            report.cleanup_pending += 1
    return report
