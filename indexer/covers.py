"""Viewport-driven AniList cover lookup and local cache worker."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from . import database


API_URL = "https://graphql.anilist.co"
USER_AGENT = "ExternalDiskIndexerSimple/0.1 (personal desktop anime library)"
MIN_IMAGE_BYTES = 1024
REQUEST_INTERVAL_SECONDS = 4.0  # 15 AniList API requests per minute at most.
MATCHING_REVISION = 2
LOG_PATH = database.APP_DIR / "logs" / "covers.log"
_LOGGER_LOCK = threading.Lock()
SEARCH_QUERY = """
query ($search: String!) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME) {
      id
      title {
        romaji
        english
        native
      }
      synonyms
      coverImage {
        large
      }
    }
  }
}
"""


class CoverLookupError(Exception):
    """A non-retryable failure for the current lookup."""


class RateLimited(CoverLookupError):
    def __init__(self, retry_after: float) -> None:
        super().__init__("AniList rate limit reached")
        self.retry_after = retry_after


class ApiBlocked(CoverLookupError):
    """AniList or Cloudflare rejected access; do not send further API requests this run."""


@dataclass(frozen=True)
class CoverMatch:
    anilist_id: int
    cover_url: str
    title: str | None = None


def display_anilist_title(titles) -> str | None:
    if isinstance(titles, dict):
        for key in ("english", "romaji", "native"):
            value = titles.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_anilist_url(value: str) -> int:
    try:
        url = urlsplit(value.strip())
        match = re.fullmatch(r"/anime/([0-9]+)(?:/[^\s]*)?", url.path)
        if (url.scheme != "https" or url.hostname not in {"anilist.co", "www.anilist.co"}
                or url.username is not None or url.password is not None or url.port is not None
                or url.query or url.fragment or not match or int(match[1]) <= 0
                or any(c.isspace() or ord(c) < 32 for c in value.strip()) or "\\" in value):
            raise ValueError
        return int(match[1])
    except (ValueError, AttributeError):
        raise ValueError("Enter a valid HTTPS AniList anime URL") from None


def folder_cover_key(source_id: str, path: str) -> str:
    return json.dumps([source_id, database.IndexStore.manual_folder_path(path)], ensure_ascii=False)


@dataclass(frozen=True)
class ManualCoverJob:
    source_id: str
    path: str
    anime_id: int
    token: int
    repair: bool = False
    require_indexed: bool = False


@dataclass(frozen=True)
class PlannedMatch:
    anilist_id: int
    title: str
    cover_url: str


@dataclass(frozen=True)
class PlannedCoverJob:
    entry_id: str
    anime_id: int
    token: int
    create: bool


@dataclass(frozen=True)
class PlannedSearchJob:
    entry_id: str
    token: int
    query: str


def get_cover_logger() -> logging.Logger:
    """Return the persistent cover logger without adding duplicate handlers."""
    logger = logging.getLogger("external_disk_indexer.covers")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_path = database.APP_DIR / "logs" / "covers.log"
    target_path = str(log_path.resolve())
    with _LOGGER_LOCK:
        handlers = [handler for handler in logger.handlers if getattr(handler, "_cover_log_handler", False)]
        if any(getattr(handler, "_cover_log_path", None) == target_path for handler in handlers):
            return logger
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            handler._cover_log_handler = True  # type: ignore[attr-defined]
            handler._cover_log_path = target_path  # type: ignore[attr-defined]
            handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
            logger.addHandler(handler)
        except OSError:
            # Diagnostics must never prevent cover loading if app-data storage is unavailable.
            handler = logging.NullHandler()
            handler._cover_log_handler = True  # type: ignore[attr-defined]
            handler._cover_log_path = target_path  # type: ignore[attr-defined]
            logger.addHandler(handler)
    return logger


def milliseconds(duration: float) -> int:
    return round(duration * 1000)


def matching_diagnostics_enabled(name: str) -> bool:
    """Opt in to exact local titles only; malformed configuration stays disabled."""
    try:
        titles = json.loads(os.getenv("EDI_COVER_DIAGNOSTICS", "[]"))
        return isinstance(titles, list) and name in titles
    except (ValueError, TypeError):
        return False


def log_matching_diagnostic(name: str, event: str, **details) -> None:
    if not matching_diagnostics_enabled(name):
        return
    try:
        get_cover_logger().info("MATCH_DIAG %s", json.dumps(
            {"folder": name, "event": event, **details}, ensure_ascii=False
        ))
    except Exception:
        # Optional diagnostics must never alter lookup/cache behavior.
        pass


def normalize_anime_name(name: str) -> str:
    """Produce a stable local-name key without changing the displayed folder name."""
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def lookup_title(name: str) -> str:
    """Remove common bracketed release labels before searching AniList."""
    return re.sub(r"\s*(\[[^]]*\]|\([^)]*\))", "", name).strip() or name.strip()


def image_format(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"avif", b"avis"}:
        return "AVIF"
    return None


def valid_cached_image(path: str | None) -> bool:
    if not path:
        return False
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return len(data) >= MIN_IMAGE_BYTES and image_format(data) is not None


def local_file_url(path: str) -> str:
    return Path(path).resolve().as_uri()


def _is_cloudflare_1010(status: int, body: str) -> bool:
    return status == 1010 or ("cloudflare" in body.lower() and "1010" in body)


def select_conservative_match(local_name: str, media: list[dict], *, stage: str = "primary", search_text: str | None = None) -> CoverMatch | None:
    """Prefer exact titles; otherwise require a unique, near-identical title/alias."""
    local_key = comparison_title(local_name)
    if matching_diagnostics_enabled(local_name):
        # A replayable snapshot, without response metadata or image URLs.
        snapshot = [{"id": item.get("id"), "title": item.get("title"),
                     "synonyms": item.get("synonyms"),
                     "coverImage": {"large": "available" if isinstance((item.get("coverImage") or {}).get("large"), str)
                                    and (item.get("coverImage") or {}).get("large") else None}}
                    for item in media]
        log_matching_diagnostic(local_name, "candidate_snapshot", stage=stage, search=search_text if search_text is not None else lookup_title(local_name),
                                count=len(media), media=snapshot, local_sequence=sequence_markers(local_key))
    if not local_key:
        log_matching_diagnostic(local_name, "rejected", stage=stage, reason="empty normalized local title")
        return None
    scored: dict[int, tuple[float, dict]] = {}
    for item in media:
        cover_url = (item.get("coverImage") or {}).get("large")
        if not isinstance(item.get("id"), int) or not isinstance(cover_url, str) or not cover_url:
            log_matching_diagnostic(local_name, "candidate_rejected", stage=stage, id=item.get("id"), score=0,
                                    reason="missing valid integer ID or nonempty cover URL")
            continue
        title = item.get("title") or {}
        official = [value for value in title.values() if isinstance(value, str)]
        # Aliases must not erase explicit season/part identifiers on the result.
        canonical = title.get("romaji") or title.get("english") or ""
        if sequence_markers(comparison_title(canonical)) != sequence_markers(local_key):
            log_matching_diagnostic(local_name, "candidate_rejected", stage=stage, id=item["id"], score=0,
                                    sequence=sequence_markers(comparison_title(canonical)), reason="canonical sequence conflict")
            continue
        score = 0.0
        for value in official + [s for s in (item.get("synonyms") or []) if isinstance(s, str)]:
            candidate = comparison_title(value)
            if not candidate or sequence_markers(candidate) != sequence_markers(local_key):
                log_matching_diagnostic(local_name, "variant_rejected", stage=stage, id=item["id"], title=value,
                                        sequence=sequence_markers(candidate), reason="empty normalized title" if not candidate else "variant sequence conflict")
                continue
            if candidate == local_key:
                score = max(score, 2.0 if value in official else 1.0)
            elif candidate.replace(" ", "") == local_key.replace(" ", ""):
                score = max(score, 1.0)
            else:
                left, right = local_key.split(), candidate.split()
                # No prefix/substring matching or abbreviation guessing. Extra
                # subtitle/sequel words are meaningful, even on long titles.
                if len(left) != len(right) or len(local_key) < 12:
                    log_matching_diagnostic(local_name, "variant_rejected", stage=stage, id=item["id"], title=value,
                                            sequence=sequence_markers(candidate), reason="word-count mismatch" if len(left) != len(right) else "local title shorter than 12")
                    continue
                if all(SequenceMatcher(None, a, b).ratio() >= 0.8 for a, b in zip(left, right)):
                    score = max(score, SequenceMatcher(None, local_key, candidate).ratio())
                else:
                    log_matching_diagnostic(local_name, "variant_rejected", stage=stage, id=item["id"], title=value,
                                            sequence=sequence_markers(candidate), reason="per-word similarity below 0.8")
        log_matching_diagnostic(local_name, "candidate_score", stage=stage, id=item["id"], score=score,
                                sequence=sequence_markers(comparison_title(canonical)))
        if score:
            previous = scored.get(item["id"], (0.0, item))[0]
            scored[item["id"]] = (max(score, previous), item)
    if not scored:
        log_matching_diagnostic(local_name, "rejected", stage=stage, reason="no candidates with positive scores")
        return None
    ranked = sorted(scored.values(), key=lambda pair: pair[0], reverse=True)
    best_score, item = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    log_matching_diagnostic(local_name, "ranking", stage=stage, best_id=item["id"], best_score=best_score,
                            runner_up_id=ranked[1][1]["id"] if len(ranked) > 1 else None,
                            runner_up_score=runner_up, margin=best_score - runner_up)
    if best_score < 0.94 or best_score - runner_up < 0.08:
        log_matching_diagnostic(local_name, "rejected", stage=stage, reason="best score below 0.94" if best_score < 0.94 else "ambiguous: margin below 0.08")
        return None
    log_matching_diagnostic(local_name, "accepted", stage=stage, id=item["id"])
    return CoverMatch(item["id"], item["coverImage"]["large"], display_anilist_title(item.get("title")))


def comparison_title(value: str) -> str:
    """Comparison only: never change the persisted local-name cache key."""
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join("".join(c if c.isalnum() else " " for c in value).split())


def sequence_markers(value: str) -> tuple[str, ...]:
    roman = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}
    qualifiers = {"season", "part", "final", "second", "third", "sequel", "movie", "ova", "special", "r2"}
    return tuple(roman.get(token, token) for token in value.split() if token.isdigit() or token in roman or token in qualifiers)


def replay_captured_candidates(log_path: Path, name: str) -> CoverMatch | None:
    """Re-score the latest snapshot for one title. No network, cache reads, or writes."""
    captured = None
    with log_path.open(encoding="utf-8") as source:
        for line in source:
            if "MATCH_DIAG " not in line:
                continue
            try:
                record = json.loads(line.split("MATCH_DIAG ", 1)[1])
            except ValueError:
                continue
            if record.get("event") == "candidate_snapshot" and record.get("folder") == name:
                captured = record
    if captured is None:
        raise ValueError("No captured candidates for this exact folder title; no lookup was attempted")
    return select_conservative_match(name, captured["media"], stage="offline_replay", search_text=captured.get("search"))


def shortened_search_title(search_text: str) -> str | None:
    """Drop only the final word; retain at least three words and 12 characters."""
    words = search_text.split()
    shortened = " ".join(words[:-1])
    return shortened if len(words) >= 4 and len(shortened) >= 12 else None


def search_anilist(local_name: str, *, before_fallback: Callable[[], bool] | None = None) -> CoverMatch | None:
    primary = lookup_title(local_name)
    log_matching_diagnostic(local_name, "request", stage="primary", search=primary)
    media = _search_candidates(primary)
    match = select_conservative_match(local_name, media, stage="primary", search_text=primary)
    fallback = shortened_search_title(primary) if not media else None
    if fallback is None:
        return match
    if before_fallback is not None:
        if not before_fallback():
            return None
    else:
        # Standalone callers also wait; the application always supplies the
        # FIFO worker's shared limiter, covering the next job as well.
        time.sleep(REQUEST_INTERVAL_SECONDS)
    log_matching_diagnostic(local_name, "request", stage="fallback", search=fallback)
    media = _search_candidates(fallback)
    return select_conservative_match(local_name, media, stage="fallback", search_text=fallback)


def _search_candidates(search_text: str, *, anime_id: int | None = None, review: bool = False) -> list[dict]:
    query = SEARCH_QUERY if anime_id is None else SEARCH_QUERY.replace("$search: String!", "$id: Int!").replace("search: $search", "id: $id")
    variables = {"search": search_text} if anime_id is None else {"id": anime_id}
    if review:
        query = query.replace("synonyms", "synonyms\n      seasonYear\n      format")
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        API_URL,
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if error.code == 429:
            try:
                retry_after = float(error.headers.get("Retry-After", "60"))
            except ValueError:
                retry_after = 60.0
            raise RateLimited(retry_after) from error
        if error.code == 403 or _is_cloudflare_1010(error.code, body):
            raise ApiBlocked("AniList access was blocked") from error
        raise CoverLookupError(f"AniList HTTP error {error.code}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise CoverLookupError(f"AniList request failed: {error}") from error

    errors = result.get("errors")
    if errors:
        message = "; ".join(str(error.get("message", "Unknown API error")) for error in errors)
        if "cloudflare" in message.lower() and "1010" in message:
            raise ApiBlocked(message)
        raise CoverLookupError(message)
    media = result.get("data", {}).get("Page", {}).get("media")
    if not isinstance(media, list):
        raise CoverLookupError("AniList returned an invalid media response")
    return media


def lookup_anilist_id(anime_id: int) -> CoverMatch:
    media = _search_candidates("", anime_id=anime_id)
    for item in media:
        url = (item.get("coverImage") or {}).get("large")
        if item.get("id") == anime_id and isinstance(url, str) and url:
            return CoverMatch(anime_id, url, display_anilist_title(item.get("title")))
    raise CoverLookupError("AniList anime entry or cover was not found")


def lookup_planned_id(anime_id: int) -> PlannedMatch:
    for item in _search_candidates("", anime_id=anime_id):
        titles = item.get("title") or {}
        title = titles.get("english") or titles.get("romaji") or titles.get("native")
        url = (item.get("coverImage") or {}).get("large")
        if item.get("id") == anime_id and isinstance(title, str) and title.strip() and isinstance(url, str) and url:
            return PlannedMatch(anime_id, title, url)
    raise CoverLookupError("AniList anime title or cover was not found")


def search_planned_candidates(query: str) -> list[dict]:
    results = []
    for item in _search_candidates(query, review=True)[:5]:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
            continue
        titles = item.get("title")
        if not isinstance(titles, dict):
            continue
        names = list(dict.fromkeys(value.strip()[:500] for key in ("english", "romaji", "native")
                                   if isinstance(value := titles.get(key), str) and value.strip()))
        if not names:
            continue
        results.append({"id": item["id"], "title": names[0], "alternate": " / ".join(names[1:]),
                        "year": str(item["seasonYear"]) if type(item.get("seasonYear")) is int else "",
                        "format": item.get("format", "") if isinstance(item.get("format"), str) else ""})
    return results


def download_cover(url: str, destination: Path) -> None:
    request = Request(url, headers={"Accept": "image/*", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=20) as response:
            image_data = response.read()
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        if error.code == 403 or _is_cloudflare_1010(error.code, body):
            raise ApiBlocked("Cover download was blocked") from error
        raise CoverLookupError(f"Cover HTTP error {error.code}") from error
    except (URLError, TimeoutError) as error:
        raise CoverLookupError(f"Cover download failed: {error}") from error

    if len(image_data) < MIN_IMAGE_BYTES or image_format(image_data) is None:
        raise CoverLookupError("Cover download was not a valid image")
    temporary_path = destination.with_name(destination.name + ".part")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_bytes(image_data)
        os.replace(temporary_path, destination)
    except OSError as error:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CoverLookupError(f"Could not save cover: {error}") from error


class CoverService:
    """A serial, rate-limited background queue for covers requested by the visible grid."""

    def __init__(
        self,
        *,
        searcher: Callable[[str], CoverMatch | None] | None = None,
        downloader: Callable[[str, Path], None] = download_cover,
        store_factory: Callable[[], database.IndexStore] = database.IndexStore,
        cache_dir: Path | None = None,
        request_interval: float = REQUEST_INTERVAL_SECONDS,
        exact_lookup: Callable[[int], CoverMatch] = lookup_anilist_id,
        planned_lookup: Callable[[int], PlannedMatch] = lookup_planned_id,
        candidate_search: Callable[[str], list[dict]] = search_planned_candidates,
    ) -> None:
        self.events: queue.Queue = queue.Queue()
        self._searcher = searcher if searcher is not None else lambda name: search_anilist(name, before_fallback=self._prepare_fallback)
        self._downloader = downloader
        self._exact_lookup = exact_lookup
        self._planned_lookup = planned_lookup
        self._candidate_search = candidate_search
        self._candidate_version = 0
        self._planned_versions: dict[str, int] = {}
        self._store_factory = store_factory
        self._cache_dir = cache_dir or database.APP_DIR / "cache" / "covers"
        self._request_interval = request_interval
        self._queue: queue.Queue[tuple[str, str, float] | ManualCoverJob | PlannedCoverJob | PlannedSearchJob | None] = queue.Queue()
        self._lock = threading.Lock()
        self._manual_versions: dict[str, int] = {}
        self._manual_attempted: set[str] = set()
        self._manual_pending: set[tuple[str, int]] = set()
        self._queued: set[str] = set()
        self._terminal: set[str] = set()
        self._next_request_at = 0.0
        self._api_blocked = False
        self._stopped = threading.Event()
        retry_title = os.getenv("EDI_COVER_DIAGNOSTIC_RETRY", "")
        self._diagnostic_retry_title = retry_title if matching_diagnostics_enabled(retry_title) else ""
        self._diagnostic_retry_used = False
        self._logger = get_cover_logger()
        self._worker = threading.Thread(target=self._run, daemon=True, name="cover-loader")
        self._worker.start()

    def link_planned(self, entry_id: str, anime_id: int, *, create: bool = False) -> int | None:
        with self._lock:
            if self._stopped.is_set():
                return None
            token = self._planned_versions.get(entry_id, 0) + 1
            self._planned_versions[entry_id] = token
        self._queue.put(PlannedCoverJob(entry_id, anime_id, token, create))
        return token

    def cancel_planned_search(self) -> None:
        with self._lock:
            self._candidate_version += 1

    def search_planned(self, entry_id: str, query: str) -> int | None:
        with self._lock:
            if self._stopped.is_set():
                return None
            self._candidate_version += 1
            token = self._candidate_version
        self._queue.put(PlannedSearchJob(entry_id, token, query))
        return token

    def _process_planned_search(self, job: PlannedSearchJob) -> None:
        try:
            with self._lock:
                if job.token != self._candidate_version or self._stopped.is_set(): return
            if self._api_blocked:
                raise ApiBlocked("AniList access is blocked. Try again after restarting the application.")
            self._wait_for_rate_limit()
            with self._lock:
                if job.token != self._candidate_version or self._stopped.is_set(): return
            results = self._candidate_search(job.query)
            for candidate in results:
                path = self._cache_dir / f"{candidate['id']}.jpg"
                candidate["localCoverUrl"] = local_file_url(str(path)) if valid_cached_image(str(path)) else ""
            error = ""
        except Exception as exc:
            with self._lock:
                if isinstance(exc, RateLimited):
                    self._next_request_at = max(self._next_request_at, time.monotonic() + exc.retry_after)
                if isinstance(exc, ApiBlocked): self._api_blocked = True
            results, error = [], str(exc) or type(exc).__name__
        with self._lock:
            if job.token == self._candidate_version and not self._stopped.is_set():
                self.events.put(("planned_candidates", job.entry_id, job.token, results, error))

    def invalidate_planned(self, entry_id: str) -> None:
        with self._lock:
            self._planned_versions[entry_id] = self._planned_versions.get(entry_id, 0) + 1

    def _process_planned(self, job: PlannedCoverJob) -> None:
        store = self._store_factory()
        try:
            with self._lock:
                if self._planned_versions.get(job.entry_id) != job.token:
                    return
            if self._api_blocked:
                raise ApiBlocked("AniList access is blocked")
            self._wait_for_rate_limit()
            with self._lock:
                if self._stopped.is_set() or self._planned_versions.get(job.entry_id) != job.token:
                    return
            match = self._planned_lookup(job.anime_id)
            if match.anilist_id != job.anime_id or not match.title.strip():
                raise CoverLookupError("AniList returned an invalid entry")
            destination = self._cache_dir / f"{match.anilist_id}.jpg"
            if not valid_cached_image(str(destination)):
                self._downloader(match.cover_url, destination)
            if not valid_cached_image(str(destination)):
                raise CoverLookupError("Planned cover failed validation")
            with self._lock:
                if self._stopped.is_set() or self._planned_versions.get(job.entry_id) != job.token:
                    return
                store.link_planned(job.entry_id, match.anilist_id, match.title, match.cover_url, str(destination), create=job.create)
                self.events.put(("planned_ready", job.entry_id, job.token, "Planned AniList entry saved."))
        except (CoverLookupError, ValueError) as error:
            with self._lock:
                if isinstance(error, RateLimited):
                    self._next_request_at = max(self._next_request_at, time.monotonic() + error.retry_after)
                if isinstance(error, ApiBlocked):
                    self._api_blocked = True
                if self._planned_versions.get(job.entry_id) == job.token:
                    self.events.put(("planned_failed", job.entry_id, job.token, str(error)))
            self._logger.warning("Planned AniList lookup failed: %s", error)
        finally:
            store.conn.close()

    def link_folder(self, source_id: str, path: str, anime_id: int, *, repair: bool = False, require_indexed: bool = False) -> bool:
        key = folder_cover_key(source_id, path)
        with self._lock:
            if self._stopped.is_set() or (repair and (key in self._manual_attempted or any(pending_key == key for pending_key, _ in self._manual_pending))):
                return False
            token = self._manual_versions.get(key, 0) + 1
            self._manual_versions[key] = token
            if repair:
                self._manual_attempted.add(key)
            else:
                self._manual_attempted.discard(key)
            self._manual_pending.add((key, token))
        self._queue.put(ManualCoverJob(source_id, path, anime_id, token, repair, require_indexed))
        return True

    def invalidate_manual(self, source_id: str, path: str | None = None) -> None:
        with self._lock:
            for key in list(self._manual_versions):
                if (path is not None and key == folder_cover_key(source_id, path)) or (path is None and json.loads(key)[0] == source_id):
                    self._manual_versions[key] += 1

    def _process_manual(self, job: ManualCoverJob) -> None:
        key = folder_cover_key(job.source_id, job.path)
        store = self._store_factory()
        try:
            with self._lock:
                if self._manual_versions.get(key) != job.token:
                    return
            previous = store.manual_links(job.source_id).get(store.manual_folder_path(job.path))
            if job.repair and (not previous or previous[0] != job.anime_id):
                return
            if self._api_blocked:
                raise ApiBlocked("AniList access is blocked")
            # A repair already has a verified exact association and image URL.
            if job.repair:
                match = CoverMatch(previous[0], previous[1], store.linked_anilist_title(job.source_id, job.path, ""))
            else:
                self._wait_for_rate_limit()
                if self._stopped.is_set():
                    return
                match = self._exact_lookup(job.anime_id)
                if match.anilist_id != job.anime_id:
                    raise CoverLookupError("AniList returned a different entry")
            destination = self._cache_dir / f"{match.anilist_id}.jpg"
            if not valid_cached_image(str(destination)):
                self._downloader(match.cover_url, destination)
            if not valid_cached_image(str(destination)):
                raise CoverLookupError("Manual cover failed validation")
            with self._lock:
                if self._stopped.is_set() or self._manual_versions.get(key) != job.token or not store.manual_source_active(job.source_id):
                    return
                if job.require_indexed and not store.conn.execute(
                    "SELECT 1 FROM entries WHERE drive_id=? AND path=? AND is_folder=1", (job.source_id, job.path)).fetchone():
                    self.events.put(("manual_failed", job.source_id, job.path, "Indexed folder is no longer available."))
                    return
                store.save_manual_link(job.source_id, job.path, match.anilist_id, match.cover_url, str(destination), title=match.title)
                self.events.put(("manual_ready", job.source_id, job.path, job.token))
        except CoverLookupError as error:
            with self._lock:
                if isinstance(error, RateLimited):
                    self._next_request_at = max(self._next_request_at, time.monotonic() + error.retry_after)
                if isinstance(error, ApiBlocked):
                    self._api_blocked = True
                if self._manual_versions.get(key) == job.token:
                    self.events.put(("manual_failed", job.source_id, job.path, str(error)))
            self._logger.warning("Manual cover lookup failed: %s", error)
        finally:
            store.conn.close()
            with self._lock:
                self._manual_pending.discard((key, job.token))

    def diagnostic_retry_available(self, name: str) -> bool:
        """Developer opt-in: one exact title, once per service lifetime."""
        with self._lock:
            return bool(self._diagnostic_retry_title and name == self._diagnostic_retry_title
                        and not self._diagnostic_retry_used and not self._api_blocked
                        and not self._stopped.is_set())

    def enqueue(self, name: str) -> bool:
        key = normalize_anime_name(name)
        if not key:
            return False
        with self._lock:
            if self._stopped.is_set() or key in self._queued or key in self._terminal:
                return False
            self._queued.add(key)
        self._queue.put((key, name, time.monotonic()))
        self._logger.info("%s | Enqueued", name)
        return True

    def process_events(self) -> list[tuple]:
        events: list[tuple] = []
        try:
            while True:
                events.append(self.events.get_nowait())
        except queue.Empty:
            pass
        return events

    def shutdown(self) -> None:
        self._stopped.set()
        self._queue.put(None)

    def _run(self) -> None:
        while not self._stopped.is_set():
            job = self._queue.get()
            if job is None:
                return
            if isinstance(job, PlannedSearchJob):
                self._process_planned_search(job)
                continue
            if isinstance(job, PlannedCoverJob):
                try:
                    self._process_planned(job)
                except Exception as error:
                    self.events.put(("planned_failed", job.entry_id, job.token, str(error)))
                continue
            if isinstance(job, ManualCoverJob):
                try:
                    self._process_manual(job)
                except Exception as error:
                    self.events.put(("manual_failed", job.source_id, job.path, str(error)))
                continue
            key, name, enqueued_at = job
            try:
                self._process_job(key, name, enqueued_at)
            except Exception as error:
                # A cover failure must never terminate the serial worker or affect indexing.
                self.events.put(("failed", key, f"Cover lookup failed: {error}"))
                self._logger.error("%s | Failed unexpectedly | Total: %d ms | Error: %s", name, milliseconds(time.monotonic() - enqueued_at), error)
            finally:
                with self._lock:
                    self._queued.discard(key)
                    self._terminal.add(key)

    def _process_job(self, key: str, name: str, enqueued_at: float) -> None:
        processing_started = time.monotonic()
        queue_wait = processing_started - enqueued_at
        self._logger.info("%s | Processing started | Queue wait: %d ms", name, milliseconds(queue_wait))
        store = self._store_factory()
        try:
            existing = store.cover_result(key)
            diagnostic_retry = False
            if existing and existing[3] == "no_match":
                with self._lock:
                    if (self._diagnostic_retry_title and name == self._diagnostic_retry_title
                            and not self._diagnostic_retry_used and not self._api_blocked
                            and not self._stopped.is_set()):
                        self._diagnostic_retry_used = True
                        diagnostic_retry = True
                if diagnostic_retry:
                    log_matching_diagnostic(name, "worker_diagnostic_retry", reason="explicit one-time no_match bypass")
            if matching_diagnostics_enabled(name):
                revision = store.cover_matching_revision(key)
                log_matching_diagnostic(name, "worker_cache", status=existing[3] if existing else None,
                                        revision=revision, required_revision=MATCHING_REVISION,
                                        diagnostic_retry=diagnostic_retry,
                                        lookup_skipped=bool(existing and not diagnostic_retry and not (existing[3] == "no_match" and revision < MATCHING_REVISION)))
            if existing and not diagnostic_retry and not (existing[3] == "no_match" and store.cover_matching_revision(key) < MATCHING_REVISION):
                anilist_id, cover_url, cache_path, status = existing
                if status == "found" and valid_cached_image(cache_path):
                    self.events.put(("ready", key, local_file_url(cache_path)))
                    self._logger.info("%s | Cache hit | Total: %d ms", name, milliseconds(time.monotonic() - processing_started))
                elif status == "found" and cover_url and anilist_id:
                    title = store.conn.execute("SELECT anilist_title FROM anime_cover_cache WHERE normalized_name=?", (key,)).fetchone()[0]
                    self._download_and_save(store, key, name, processing_started, anilist_id, cover_url, title)
                elif status == "found":
                    store.save_cover_result(key, anilist_id, cover_url, cache_path, "failed")
                    self.events.put(("failed", key, "Cached cover entry is incomplete"))
                    self._logger.error("%s | Failed | Total: %d ms | Error: Cached cover entry is incomplete", name, milliseconds(time.monotonic() - processing_started))
                else:
                    self._logger.info("%s | Cached result: %s | Total: %d ms", name, status, milliseconds(time.monotonic() - processing_started))
                return
            with self._lock:
                blocked = self._api_blocked
            if blocked:
                store.save_cover_result(key, None, None, None, "blocked")
                self.events.put(("failed", key, "AniList access is blocked"))
                self._logger.warning("%s | Blocked | Total: %d ms | Error: AniList access is blocked", name, milliseconds(time.monotonic() - processing_started))
                return
            rate_wait = self._wait_for_rate_limit()
            if self._stopped.is_set():
                return
            self._logger.info("%s | Search started | Queue wait: %d ms | Rate-limit wait: %d ms", name, milliseconds(queue_wait), milliseconds(rate_wait))
            search_started = time.monotonic()
            try:
                match = self._searcher(name)
            except RateLimited as error:
                search_duration = time.monotonic() - search_started
                with self._lock:
                    self._next_request_at = max(self._next_request_at, time.monotonic() + error.retry_after)
                store.save_cover_result(key, None, None, None, "failed")
                self.events.put(("failed", key, str(error)))
                self._logger.warning("%s | Failed | Search: %d ms | Total: %d ms | Error: %s", name, milliseconds(search_duration), milliseconds(time.monotonic() - processing_started), error)
                return
            except ApiBlocked as error:
                search_duration = time.monotonic() - search_started
                with self._lock:
                    self._api_blocked = True
                store.save_cover_result(key, None, None, None, "blocked")
                self.events.put(("failed", key, str(error)))
                self._logger.warning("%s | Blocked | Search: %d ms | Total: %d ms | Error: %s", name, milliseconds(search_duration), milliseconds(time.monotonic() - processing_started), error)
                return
            except CoverLookupError as error:
                search_duration = time.monotonic() - search_started
                store.save_cover_result(key, None, None, None, "failed")
                self.events.put(("failed", key, str(error)))
                self._logger.error("%s | Failed | Search: %d ms | Total: %d ms | Error: %s", name, milliseconds(search_duration), milliseconds(time.monotonic() - processing_started), error)
                return
            search_duration = time.monotonic() - search_started
            if self._stopped.is_set():
                return
            if not match:
                store.save_cover_result(key, None, None, None, "no_match", matching_revision=MATCHING_REVISION)
                self._logger.info("%s | No match | Search: %d ms | Total: %d ms", name, milliseconds(search_duration), milliseconds(time.monotonic() - processing_started))
                return
            self._logger.info("%s | Match found | Search: %d ms", name, milliseconds(search_duration))
            self._download_and_save(store, key, name, processing_started, match.anilist_id, match.cover_url, match.title)
        finally:
            store.conn.close()

    def _prepare_fallback(self) -> bool:
        self._wait_for_rate_limit()
        return not self._stopped.is_set()

    def _wait_for_rate_limit(self) -> float:
        wait_started = time.monotonic()
        with self._lock:
            wait_for = self._next_request_at - time.monotonic()
        if wait_for > 0:
            self._stopped.wait(wait_for)
        with self._lock:
            self._next_request_at = time.monotonic() + self._request_interval
        return time.monotonic() - wait_started

    def _download_and_save(self, store: database.IndexStore, key: str, name: str, processing_started: float, anilist_id: int, cover_url: str, title: str | None = None) -> None:
        path = self._cache_dir / f"{anilist_id}.jpg"
        download_started = time.monotonic()
        try:
            if not valid_cached_image(str(path)):
                self._downloader(cover_url, path)
            if not valid_cached_image(str(path)):
                raise CoverLookupError("Cached cover failed validation")
        except ApiBlocked as error:
            store.save_cover_result(key, anilist_id, cover_url, str(path), "blocked")
            self.events.put(("failed", key, str(error)))
            self._logger.warning("%s | Blocked | Download: %d ms | Total: %d ms | Error: %s", name, milliseconds(time.monotonic() - download_started), milliseconds(time.monotonic() - processing_started), error)
            return
        except CoverLookupError as error:
            store.save_cover_result(key, anilist_id, cover_url, str(path), "failed")
            self.events.put(("failed", key, str(error)))
            self._logger.error("%s | Failed | Download: %d ms | Total: %d ms | Error: %s", name, milliseconds(time.monotonic() - download_started), milliseconds(time.monotonic() - processing_started), error)
            return
        store.save_cover_result(key, anilist_id, cover_url, str(path), "found", title=title)
        self.events.put(("ready", key, local_file_url(str(path))))
        self._logger.info("%s | Download completed | Download: %d ms", name, milliseconds(time.monotonic() - download_started))
        self._logger.info("%s | Cover saved | Total: %d ms", name, milliseconds(time.monotonic() - processing_started))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Replay a selected title's captured AniList candidates offline.")
    parser.add_argument("--replay", type=Path, required=True, help="covers.log (or a rotated copy) containing a snapshot")
    parser.add_argument("--title", required=True, help="Exact local folder title")
    args = parser.parse_args()
    os.environ["EDI_COVER_DIAGNOSTICS"] = json.dumps([args.title])
    try:
        replayed = replay_captured_candidates(args.replay, args.title)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Offline replay: {error}\n")
    print(f"Offline replay: {'matched ID ' + str(replayed.anilist_id) if replayed else 'no_match'}. Cache unchanged.")
