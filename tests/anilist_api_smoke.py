"""One-request manual smoke test for AniList anime title search."""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://graphql.anilist.co"
SEARCH_TITLE = "Re"
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


def print_api_errors(errors: list[dict]) -> None:
    print("AniList returned GraphQL errors:", file=sys.stderr)
    for error in errors:
        print(f"- {error.get('message', 'Unknown API error')}", file=sys.stderr)


def main() -> int:
    payload = json.dumps({"query": QUERY, "variables": {"search": SEARCH_TITLE}}).encode("utf-8")
    request = Request(
        API_URL,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ExternalDiskIndexerSimple/0.1 (personal desktop anime library)",
        },
        method="POST",
    )

    try:
        # This is intentionally the script's only network request.
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        if error.code == 429:
            retry_after = error.headers.get("Retry-After", "an unspecified delay")
            print(f"AniList rate limit reached. Retry after: {retry_after}.", file=sys.stderr)
        else:
            print(f"AniList HTTP error: {error.code} {error.reason}", file=sys.stderr)
        print(f"Response body: {response_body}", file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Could not connect to AniList: {error.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("AniList request timed out.", file=sys.stderr)
        return 1
    except json.JSONDecodeError:
        print("AniList returned an invalid JSON response.", file=sys.stderr)
        return 1

    if result.get("errors"):
        print_api_errors(result["errors"])
        return 1

    media = result.get("data", {}).get("Page", {}).get("media", [])
    if not media:
        print(f'No anime matches found for "{SEARCH_TITLE}".')
        return 0

    print(f'AniList matches for "{SEARCH_TITLE}":\n')
    for item in media:
        title = item.get("title") or {}
        cover = item.get("coverImage") or {}
        print(f"ID:      {item.get('id')}")
        print(f"Romaji:  {title.get('romaji') or '—'}")
        print(f"English: {title.get('english') or '—'}")
        print(f"Cover:   {cover.get('large') or '—'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
