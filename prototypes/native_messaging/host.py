"""Isolated, one-request Native Messaging capture host; standard library only."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import struct
import sys
import tempfile

# Load only the schema, not the application. prepare_host.py bundles an identical
# copy beside the installed helper; source-tree execution uses the original.
_format_path = Path(__file__).with_name("capture_format.py")
if not _format_path.is_file():
    _format_path = Path(__file__).resolve().parents[2] / "indexer" / "capture_format.py"
_spec = importlib.util.spec_from_file_location("_planned_capture_format", _format_path)
_format = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_format)
CaptureError = _format.CaptureError
decode_json = _format.decode_json
validate_capture = _format.validate_capture
MAX_FRAME_BYTES = _format.MAX_FRAME_BYTES
MAX_TEXT_CHARS = _format.MAX_TEXT_CHARS
MAX_TEXT_BYTES = _format.MAX_TEXT_BYTES

HOST_NAME = "com.externaldiskindexer.planned_transport_test"
HEADER = struct.Struct("=I")  # Native byte order, exactly four bytes.
END_OF_INPUT = object()
LOG = logging.getLogger("planned_transport_test")


def prototype_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA", "")
    if not root or not Path(root).is_absolute():
        raise CaptureError("configuration_error", "An absolute LOCALAPPDATA is required.")
    return Path(root) / "ExternalDiskIndexer" / "NativeMessagingPrototype"


def _read_exact(stream, count: int, *, clean_eof: bool = False) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < count:
        part = stream.read(count - len(chunks))
        if not part:
            if clean_eof and not chunks:
                return None
            raise CaptureError("protocol_error", "Truncated Native Messaging frame.")
        chunks.extend(part)
    return bytes(chunks)


def read_message(stream):
    header = _read_exact(stream, HEADER.size, clean_eof=True)
    if header is None:
        return END_OF_INPUT
    size = HEADER.unpack(header)[0]
    if not 0 < size <= MAX_FRAME_BYTES:
        raise CaptureError("protocol_error", "Message size exceeds prototype limits.")
    return decode_json(_read_exact(stream, size))


def write_message(stream, message: dict) -> None:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    stream.write(HEADER.pack(len(raw)))
    stream.write(raw)
    stream.flush()


def _check_existing(path: Path, capture: dict) -> bool:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FRAME_BYTES + 1)
    except FileNotFoundError:
        return False
    try:
        existing = validate_capture(decode_json(raw)) if len(raw) <= MAX_FRAME_BYTES else None
    except CaptureError:
        existing = None
    if existing != capture:
        raise CaptureError("id_conflict", "Capture UUID already exists with different content.")
    return True


def _publish(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)  # Windows rename fails if destination exists.
    else:
        os.link(source, destination)  # Equivalent no-overwrite publication for tests.


def save_capture(inbox: Path, capture: dict) -> str:
    """Flush before atomic publication; never overwrite an existing UUID, even in a race."""
    capture = validate_capture(capture)
    inbox.mkdir(parents=True, exist_ok=True)
    destination = inbox / (capture["capture_id"] + ".json")
    if _check_existing(destination, capture):
        return "already_saved"
    raw = json.dumps(capture, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=inbox, prefix=".capture-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish a complete file, never overwriting a concurrent capture.
            _publish(temporary, destination)
        except FileExistsError:
            _check_existing(destination, capture)
            return "already_saved"
        return "saved"
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def serve(source, destination, inbox: Path, *, inbox_name: str = "prototype") -> int:
    """sendNativeMessage starts one process per message; reply once and exit."""
    try:
        message = read_message(source)
        if message is END_OF_INPUT:
            return 0
        capture = validate_capture(message)
        status = save_capture(inbox, capture)
        response = {"ok": True, "capture_id": capture["capture_id"], "status": status, "inbox": inbox_name}
        LOG.info("capture %s", status)  # No selected text or paths in diagnostics.
    except CaptureError as exc:
        response = {"ok": False, "error": exc.code, "message": str(exc)}
        LOG.warning("capture rejected: %s", exc.code)
    except OSError:
        response = {"ok": False, "error": "write_failed", "message": "Could not persist the capture. Check inbox permissions and free space."}
        LOG.error("capture write failed")
    write_message(destination, response)
    return 0 if response["ok"] else 1


def read_origin_config(root: Path) -> dict:
    try:
        with (root / "host.json").open("rb") as stream:
            config = decode_json(stream.read(8193))
        if not isinstance(config, dict) or config.get("name") != HOST_NAME:
            raise ValueError("Invalid host configuration")
        allowed = config.get("allowed_origins")
        if not isinstance(allowed, list) or not allowed or not all(
            isinstance(item, str) and re.fullmatch(r"chrome-extension://[a-p]{32}/", item)
            for item in allowed
        ):
            raise ValueError("Invalid allowed origins")
        return config
    except (OSError, ValueError, CaptureError) as exc:
        raise CaptureError("unauthorized", "Caller is not an explicitly configured extension.") from exc


def validate_origin(origin: str, root: Path) -> None:
    if origin not in read_origin_config(root)["allowed_origins"]:
        raise CaptureError("unauthorized", "Caller is not an explicitly configured extension.")


def capture_destination(root: Path, *, required=False) -> tuple[Path, str]:
    try:
        with (root / "destination.json").open("rb") as stream:
            config = decode_json(stream.read(1025))
    except FileNotFoundError:
        if required:
            raise CaptureError("configuration_error", "Capture destination is missing; rerun explicit setup.")
        config = {"inbox": "prototype"}  # Existing Stage 3A installs remain isolated.
    except (OSError, CaptureError) as exc:
        raise CaptureError("configuration_error", "Invalid capture destination configuration.") from exc
    if not isinstance(config, dict) or set(config) != {"inbox"} or config["inbox"] not in ("prototype", "production"):
        raise CaptureError("configuration_error", "Choose prototype or production inbox explicitly.")
    if config["inbox"] == "production":
        return root.parent / "imports" / "inbox", "production"
    return root / "inbox", "prototype"


def main() -> int:
    if os.name == "nt":
        import ctypes
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        ctypes.windll.user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        window = ctypes.windll.kernel32.GetConsoleWindow()
        LOG.info("console attached=%s visible=%s", bool(window), bool(window and ctypes.windll.user32.IsWindowVisible(window)))
    try:
        root = prototype_dir()
        origin = sys.argv[1] if len(sys.argv) > 1 else ""
        validate_origin(origin, root)
        inbox, name = capture_destination(root)
        return serve(sys.stdin.buffer, sys.stdout.buffer, inbox, inbox_name=name)
    except CaptureError as exc:
        LOG.warning("host rejected: %s", exc.code)
        write_message(sys.stdout.buffer, {"ok": False, "error": exc.code, "message": str(exc)})
        return 1


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[planned-transport] %(levelname)s %(message)s")
    raise SystemExit(main())
