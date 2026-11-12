# JAnimeVault AniList candidate search

This guide describes Planned search (Stage 3I) and its shared Library dialog
(Stage 3I-B). Responsive sizing adapts to the application viewport in either view.

## Library folders

Indexed folder context menus append **Search AniList** in Grid and List, whether
or not a cover/link already exists. Ordinary file rows do not offer this action.
The query starts with the folder's display title. Confirmation uses the existing
folder-specific manual link pathway, keyed by source ID and normalized relative
path, never a Planned UUID. It does not create a Planned entry or change folder names.
Failed replacement keeps the old cover/link. Disconnected sources retain their
indexed records and saved associations; physical file access still requires the source.

Navigation changes, removed indexed folders/sources, resubmitted queries, and newer
requests invalidate stale work. Closing without confirmation leaves the folder
unchanged. Library search uses the same dialog, FIFO worker, four-second gate, cached
candidate thumbnails, and explicit confirmation described below for Planned.

## Planned entries

Every Planned card, manual or browser-imported, has an appended **Search AniList**
context-menu action in Grid and List mode. Existing actions remain in their order.

The action opens a modal candidate dialog and submits the entry's current title.
Edit the query and press Search/Enter to resubmit. Errors offer Retry search; empty
results invite a revised query. No search occurs on import, creation, or display.

The dialog shows up to five candidates: English/romaji/native titles, AniList ID,
season year and format where available. Thumbnails use only already validated local
ID-based cover files; other candidates show a placeholder. Searching does not
download candidate images or write the database. Field definitions follow the
[official AniList Media schema](https://docs.anilist.co/reference/object/media).

Click a candidate (or focus the list, navigate and press Enter), then explicitly
press **Link selected entry**. This also confirms replacement of any existing link.
Selecting a row alone does not link it. Cancel leaves the entry unchanged.

Confirmation queues the existing exact-ID Planned linking job. Its existing cover
validation/download/cache and unique AniList-ID constraint apply. The original title
and UUID remain intact. Failed lookup, cover download or duplicate-ID conflict keeps
the previous association; linking feedback uses the existing notification/status UI.
Search itself requires internet; existing linked covers still work from cache offline.

All searches and exact-ID links use the same FIFO worker and four-second API gate
as folder cover jobs. HTTP 403/Cloudflare blocking and 429 Retry-After update the
existing shared block/cooldown state. No parallel requests or automatic retries occur.
Cancelled queued jobs are skipped where possible; an already-running request cannot
be forcibly interrupted, but its result is ignored. Global rate-limit waits remain
in effect after cancellation. Results are not persisted or globally cached.

Service tokens and controller `(entry UUID, token)` checks reject old responses after
resubmission, switching entries, cancellation, deletion, or navigation away. Entry
existence is checked again on result delivery and confirmation. The dialog busy state
is cleared immediately on cancellation, independent of a pending HTTP request.
Confirmed link jobs retain the existing replacement/unlink/delete race protection.

## Manual checklist

1. Test Search AniList on both a manual and a browser-imported entry in Grid/List.
2. Check title prefill, edit/resubmit, no-results and failed-network/retry feedback.
3. Select a candidate, then Cancel: verify no title/link/cover changes.
4. Confirm a candidate: verify secondary AniList title, cached cover, stable UUID,
   original title, active filter and scroll anchor. No second Planned entry is created.
5. Replace a link; test a failed replacement and an AniList ID already used elsewhere.
   Verify the previous association survives failures. Verify Unlink still works.
6. Search, close/reopen, switch entries, edit/resubmit rapidly, or navigate away.
   No delayed response should populate a different dialog or link anything.
7. Keep cover jobs queued while searching: observe serial progress and expected
   request spacing. Search can wait behind existing FIFO jobs; it is not prioritized.
8. Verify Refresh imports, browser UUID idempotency, and ordinary Planned editing,
   deletion, search/filtering and Grid/List behavior remain unchanged.

Automated tests use isolated databases and mocked responses. No real AniList requests
or production data are used. Browser transport, title deduplication, automatic
matching, pagination and remote thumbnail prefetching are outside this stage.
