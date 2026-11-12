"""Shared, dependency-free browser capture schema (also bundled with the native host)."""

import datetime as dt
import json
import re
import unicodedata
import uuid

MAX_FRAME_BYTES = 32 * 1024
MAX_TEXT_CHARS = 4096
MAX_TEXT_BYTES = 16 * 1024
FIELDS = {"version", "type", "capture_id", "text", "captured_at"}


class CaptureError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def decode_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CaptureError("invalid_message", "Invalid UTF-8 JSON.") from exc


def validate_capture(message) -> dict:
    invalid = lambda: CaptureError("invalid_message", "Invalid capture fields or values.")
    if not isinstance(message, dict) or set(message) != FIELDS:
        raise invalid()
    if type(message["version"]) is not int or message["version"] != 1 or message["type"] != "capture":
        raise invalid()
    capture_id = message["capture_id"]
    try:
        parsed_id = uuid.UUID(capture_id) if isinstance(capture_id, str) else None
    except ValueError:
        raise invalid() from None
    if parsed_id is None or parsed_id.version != 4 or str(parsed_id) != capture_id:
        raise invalid()
    text = message["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise invalid()
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeError:
        raise invalid() from None
    if len(encoded) > MAX_TEXT_BYTES or any(
        unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in text
    ):
        raise invalid()
    timestamp = message["captured_at"]
    if not isinstance(timestamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", timestamp
    ):
        raise invalid()
    try:
        dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise invalid() from None
    return dict(message)
