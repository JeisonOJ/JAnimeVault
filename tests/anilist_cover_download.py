"""One-request AniList cover-download and local-cache smoke test."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://graphql.anilist.co"
SEARCH_TITLE = "Re:Zero"
TARGET_ID = 21355
TARGET_ENGLISH_TITLE = "Re:ZERO -Starting Life in Another World-"
USER_AGENT = "ExternalDiskIndexerSimple/0.1 (personal desktop anime library)"
CACHE_PATH = Path(__file__).parent / "cache" / "covers" / f"{TARGET_ID}.jpg"
MIN_IMAGE_BYTES = 1024

QUERY = """
query ($search: String!) {
  Page(perPage: 10) {
    media(search: $search, type: ANIME) {
      id
      title {
        romaji
        english
      }
      coverImage {
        large
      }
    }
  }
}
"""


def is_cloudflare_1010(status: int, body: str) -> bool:
    return status == 1010 or ("cloudflare" in body.lower() and "1010" in body)


def print_http_error(source: str, error: HTTPError) -> None:
    body = error.read().decode("utf-8", errors="replace")
    if is_cloudflare_1010(error.code, body):
        print(f"{source} was blocked by Cloudflare error 1010.", file=sys.stderr)
    elif error.code == 429:
        retry_after = error.headers.get("Retry-After", "an unspecified delay")
        print(f"{source} rate limit reached. Retry after: {retry_after}.", file=sys.stderr)
    else:
        print(f"{source} HTTP error: {error.code} {error.reason}", file=sys.stderr)
    print(f"Response body: {body}", file=sys.stderr)


def image_format(data: bytes) -> str | None:
    """Identify common image formats from their binary file signatures."""
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


def validate_image(path: Path) -> tuple[bool, int, str | None]:
    try:
        data = path.read_bytes()
    except OSError:
        return False, 0, None
    image_type = image_format(data)
    return len(data) >= MIN_IMAGE_BYTES and image_type is not None, len(data), image_type


def find_re_zero(media: list[dict]) -> dict | None:
    for item in media:
        title = item.get("title") or {}
        if item.get("id") == TARGET_ID or title.get("english") == TARGET_ENGLISH_TITLE:
            return item
    return None


def fetch_anilist_result() -> dict | None:
    payload = json.dumps({"query": QUERY, "variables": {"search": SEARCH_TITLE}}).encode("utf-8")
    request = Request(
        API_URL,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        # This is intentionally the script's only AniList API request.
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as error:
        print_http_error("AniList API", error)
        return None
    except URLError as error:
        print(f"Could not connect to AniList API: {error.reason}", file=sys.stderr)
        return None
    except TimeoutError:
        print("AniList API request timed out.", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print("AniList API returned an invalid JSON response.", file=sys.stderr)
        return None

    if result.get("errors"):
        messages = [str(error.get("message", "Unknown API error")) for error in result["errors"]]
        if any("cloudflare" in message.lower() and "1010" in message for message in messages):
            print("AniList API was blocked by Cloudflare error 1010.", file=sys.stderr)
        else:
            print("AniList returned GraphQL errors:", file=sys.stderr)
        for message in messages:
            print(f"- {message}", file=sys.stderr)
        return None

    media = result.get("data", {}).get("Page", {}).get("media")
    if not isinstance(media, list):
        print("AniList API response did not contain a media result list.", file=sys.stderr)
        return None
    target = find_re_zero(media)
    if not target:
        print(f"Could not find AniList ID {TARGET_ID} in search results for {SEARCH_TITLE!r}.", file=sys.stderr)
    return target


def download_cover(url: str, destination: Path) -> tuple[bool, int, str | None]:
    request = Request(url, headers={"Accept": "image/*", "User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=20) as response:
            image_data = response.read()
    except HTTPError as error:
        print_http_error("Cover download", error)
        return False, 0, None
    except URLError as error:
        print(f"Could not download the cover image: {error.reason}", file=sys.stderr)
        return False, 0, None
    except TimeoutError:
        print("Cover image download timed out.", file=sys.stderr)
        return False, 0, None

    image_type = image_format(image_data)
    if len(image_data) < MIN_IMAGE_BYTES or image_type is None:
        print("Cover download was not a valid image file based on its size and signature.", file=sys.stderr)
        return False, len(image_data), image_type

    temporary_path = destination.with_name(destination.name + ".part")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_bytes(image_data)
        os.replace(temporary_path, destination)
    except OSError as error:
        print(f"Could not save the cover image: {error}", file=sys.stderr)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False, len(image_data), image_type
    return True, len(image_data), image_type


def main() -> int:
    anime = fetch_anilist_result()
    if not anime:
        return 1

    title = anime.get("title") or {}
    cover_url = (anime.get("coverImage") or {}).get("large")
    if not isinstance(cover_url, str) or not cover_url:
        print("The selected AniList result did not include a large cover image URL.", file=sys.stderr)
        return 1

    valid_cache, file_size, image_type = validate_image(CACHE_PATH)
    if valid_cache:
        action = "reused from cache"
    else:
        downloaded, file_size, image_type = download_cover(cover_url, CACHE_PATH)
        if not downloaded:
            return 1
        action = "downloaded"

    display_title = title.get("english") or title.get("romaji") or "Unknown title"
    print(f"Anime:      {display_title}")
    print(f"AniList ID: {anime.get('id')}")
    print(f"Image URL:  {cover_url}")
    print(f"Local path: {CACHE_PATH}")
    print(f"File size:  {file_size:,} bytes ({image_type})")
    print(f"Status:     {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
