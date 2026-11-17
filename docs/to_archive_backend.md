# To Archive backend (v1.1.0 foundation)

This backend compares **saved index records**, not live media. It never scans,
opens, extracts, copies, moves or deletes media. The To Archive controller/QML view
uses this interface without starting scans or requesting new covers.

## Using the view

1. Right-click an already-indexed manual source in the Library sidebar and choose
   **Use as To Archive source**. Its sidebar name gains a subtle role indicator;
   it remains available in Library. Use the same action on another source to switch.
2. Open **To Archive**. The overview shows 25 anime per page. The complete filtered
   temporary source is ranked before pagination: assignment needed, recognized
   pending episodes, uncertain/review, then no recognized pending episodes. Titles
   sort alphabetically inside each group. Unknown counts never mean zero pending.
   Use the normal search field to filter immediate folders.
3. Explicitly **Confirm destination** for a suggestion, or **Choose destination**
   to search eligible indexed folder names at any depth. Known overlapping roots
   cannot be confirmed. Replacing/unlinking a mapping changes only tracking data.
4. Expand **Episode details** to inspect identifiers and original filenames.
   Review unknowns, nested-folder warnings, disconnected state and saved timestamps.
   Potentially pending identifiers are not proof that media is absent.
5. **Next page**/**Previous** navigate; **Refresh** retains the current page/search.
   **Re-index source** explicitly invokes the existing manual-source scanner for
   the designation only (disabled while unavailable/indexing). Opening the page
   never starts it. Completion refreshes saved results. **Clear source**
   removes only the active designation; previous mappings survive for later use.

The designation is promoted above ordinary sidebar sources while keeping the
normal relative ordering; clearing it restores that ordering. Copy the displayed
folder name with its copy icon or right-click **Copy name**.

The keyed presentation model updates/moves rows rather than resetting the list.
Expanded state and detail text are cached by source/folder identity. Metadata or
mapping refreshes retain the viewport anchor, search and page; changed expanded
comparisons reload lazily. If an anchor leaves the current page after re-ranking,
the nearest surviving neighbor retains reading context; page bounds are clamped
if the page no longer exists. Normal viewport-edge clamping still applies.

Episode wheel events go to exactly one scroll surface: the inner details while
it can scroll in that direction, otherwise the outer page. Scrollbars and keyboard
Up/Down/Page Up/Page Down remain available. Warnings distinguish unavailable
destinations, stale snapshots, unverified overlap, unknown filenames and missing
associations. The timestamp/staleness warning applies even when connected.

Reads and mutations run on a serialized background executor with worker-owned
database connections. Overview, picker and detail responses use independent request
tokens plus the normal navigation generation. Dialog actions capture the temporary
source ID; obsolete source contexts cannot confirm/unlink a different folder.
Index completion and source changes refresh the page through existing controller
events. Shutdown stops new work and waits for the bounded archive job to finish.

Global ranking is implemented in `ui/archive_presentation.py`, not in episode
parsing or association rules. It compares all immediate anime in one index snapshot
and keeps compact sort keys; only the selected page retains rich results. This
can cost more CPU/SQLite reads on very large temporary sources, but does not scan
media or hold all filename trees in memory. No ranking result modifies mappings.

The view uses cached manual-cover overrides before automatic cached images; a
missing manual image stays a placeholder. It never repairs/fetches covers itself.
Details display up to 200 identifiers per group and 200 unknown filenames per side,
and at most ten duplicate filenames per identifier; overflow is labeled.

## Integration interface

Use `ArchiveTracker(store, availability=None)` from `indexer.archive_tracking`.
Provide a worker-owned `IndexStore`; do not share a connection between threads.
The tracker does not open a database, start services or check drive availability.

Optional availability is a snapshot keyed by source ID, with `connected` and
`root` values. Supply a physical drive root only when its current identity has
been verified. Missing connection state is `None`, not falsely connected. Manual
source roots come from their saved registration. Never resolve a disconnected
drive using its last-known drive letter alone.

| API | Contract |
| --- | --- |
| `active_source()` | Selected source ID or `None` |
| `designate(source_id)` | Require a registered, indexed manual source; `None` clears designation, not mappings |
| `sources()` | Eligible registered source metadata, including saved index timestamp |
| `list_anime(after_path='', limit=100, term='')` | Immediate subfolders with literal normalized substring search; `next_after` is a keyset cursor; maximum page 200 |
| `match(path, limit=50)` | Resolve mapping or suggest exact-name candidates at any indexed depth; maximum 200 candidates |
| `search_destinations(term, limit=50)` | Bounded manual folder picker, excluding the temporary source; known overlaps remain labeled and cannot be confirmed |
| `confirm(path, destination_source, destination_path)` | Persist explicit association; reject self/known root overlap and missing/ambiguous indexed paths |
| `unlink(path)` | Remove active source folder's association only |
| `compare(path, file_limit=5000)` | Lazy direct-child comparison; maximum 20,000 child records per side |

Paths are source-relative. Identity uses the same Windows normalization/case
folding as manual AniList links, independent of source display labels. Invalid
arguments raise `ValueError`; SQLite errors propagate for the controller to report.
Use the controller's existing generation-token pattern when integrating async UI.

Matching states: `confirmed`, `suggested`, `multiple_candidates`, `no_match`,
`invalid_destination`, `overlap_review`. Suggestions are never persisted or
compared automatically. A missing confirmed destination never falls back to a
different exact-name candidate. Manual confirmation can select different titles;
explicit conflicting folder season markers still prevent episode comparison.

Read methods use SQLite savepoints for a consistent snapshot even during a
concurrent reindex. Query results are bounded: folder pages, candidate lists and
direct-child details are separate. The partial folder index avoids scanning file
records for candidate matching; Unicode normalization still requires a scan of
indexed folder names per requested anime (no whole-library Python list). Source
metadata is loaded once per call. A future performance change should be driven by
measured library sizes, not a new cache now.

Truncated candidate sets are marked ambiguous. Truncated file sets return
`comparison_state='incomplete_review'`, with no episode result, rather than infer
missing episodes from incomplete input. No confirmed destination means
`comparison=None`. Results retain original filenames grouped under canonical
episode IDs, distinct `archived`, `pending`, `both` lists and counts, and unknown
filenames with reasons. No inference is made from folder counts or file sizes.

## Parsing and review

`parse_episode(filename)` is pure and returns `Episode(identifier, reason, season)`.
It supports bare numbers, explicit episode/season markers and title ` - NN`
release suffixes with bracketed metadata and vN revisions. Decimal strings are
normalized without floating point. Seasons are not silently converted to episode
numbers. Supported video/archive extensions are explicit constants in the module.

`compare_episodes(...)` is also pure. Explicit SxxExx files need matching folder
season context (`Season 2` or `S2`); otherwise they remain review items. Bare
numeric aliases such as `01` and `1` share an ID. Multiple encodes/archives retain
all filenames but contribute one identifier. Matching identifiers do not prove
equal contents or a complete/healthy archive.

Packs, ranges, multipart archives, ambiguous specials, unsupported filenames and
episode-like bracket metadata remain review items. Ambiguous resolution-like
numbers also remain review items. This is deliberately not a universal fansub
parser. Nested directories are flagged, not traversed or folded into counts;
even an empty nested folder conservatively requests review. Unknown destination
files could contain an otherwise apparently pending episode; pending identifiers
are provisional and the result carries review status.

## Persistence and lifecycle

Additive `IndexStore` initialization creates:

- `archive_tracking_settings(singleton, source_id)` with a singleton constraint.
- `archive_folder_links(source_id, normalized_path, destination_source_id,
  destination_path, confirmed_at)` independent of replaceable entry rows.
- A partial covering index on existing folder records.

No existing cover, Planned or mark data is changed. Mapping history survives
source switches, restart and reindexing. Explicit manual-source removal or drive
exclusion clears designation/mappings involving that source in the removal
transaction. Disconnection does not. Folder exclusions/missing entries invalidate
the result without destroying its stored association.

## Limits and safety

Only actual indexed folder rows can be destinations; source roots themselves are
not synthesized as anime folders. Overlapping known Windows roots are rejected
conservatively, including parent/child roots. Junctions, aliases and disconnected
volume-to-letter relationships cannot be proved from the existing index. Unknown
roots are exposed as `overlap=None` in suggestions and `overlap_unverified=True`
after explicit confirmation; the UI must retain that warning. No filesystem probes
are used to remove that uncertainty.

All results remain `potentially_stale=True` with `last_indexed`, regardless of
connected state: the existing scanner does not certify index completeness.
Reindexing remains the existing explicit workflow; this feature never starts it.
The UI should show the saved timestamp, unknowns, overlap warnings and nested
content warnings rather than promise that an episode is safely archived.
