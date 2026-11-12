# JAnimeVault: browser captures into Planned

This guide covers the current importer, originally introduced in Stage 3B.

For first-time installation and ownership precautions, follow the
[source extension setup guide](browser_extension_setup.md) before changing registration.

On desktop startup, entering Planned from another view, or clicking **Refresh imports**
in Planned, a background task imports completed JSON captures from:

```text
%LOCALAPPDATA%\ExternalDiskIndexer\imports\inbox\
```

It creates ordinary, independent **manual Planned entries**. It does not search
AniList, enqueue covers, scan drives, or require an internet connection. Existing
manual editing, search, deletion, and explicit AniList linking remain available.
Startup runs once; view-entry and manual checks reuse the same importer. Checks
cannot overlap. There is no watcher or periodic import timer. An empty automatic
check is silent; an empty manual check reports **No new imports.** Import failures
release the busy state so another explicit check can retry them.

## Explicitly switch the validated helper to production delivery

The Stage 3A helper still defaults to its isolated test inbox:

```text
%LOCALAPPDATA%\ExternalDiskIndexer\NativeMessagingPrototype\inbox\
```

That directory is **never automatically imported**. Existing test captures are
not moved, deleted, or treated as production data.

1. Close the desktop application.
2. From the project root, using the working Python installation, rerun preparation
   with your actual extension ID(s) and the explicit destination option:

   ```powershell
   python prototypes/native_messaging/prepare_host.py --edge-id YOUR_EDGE_EXTENSION_ID --inbox production
   ```

   If Chrome is also registered, include `--chrome-id YOUR_CHROME_EXTENSION_ID` so
   both IDs remain allowed. This updates the installed helper, bundled schema,
   and `destination.json`; no registry keys are changed. It accepts only the fixed
   `prototype` and `production` destinations, never a path from a capture message.
3. Keep the existing Stage 3A registry registration if it already points to the
   generated `host.json` at the same location. Otherwise follow the explicit,
   per-user registration steps in the prototype README; review and approve the
   `.reg` import yourself. No administrator-wide installation or EXE build is needed.
4. Reload **Planned Transport Test** on `edge://extensions` (and
   `chrome://extensions` when applicable) to refresh the popup/background script.
5. Open its popup, paste a title, and choose **Send new capture**. The acknowledgement
   must say **Production inbox: saved** or **Production inbox: already_saved**.
   If it says Prototype, stop: the updated helper/configuration is not in use.

The helper remains installed in `NativeMessagingPrototype` to preserve the verified
host registration and launcher. Only the explicitly selected capture destination
changes. The helper still has no database access and no application dependencies.

To switch back, rerun preparation with `--inbox prototype`. Already-delivered
production files remain eligible for desktop import; changing the setting does not
withdraw them.

### Existing test captures

To import a previously saved test capture intentionally, use **Retry same capture
UUID** after switching to production. This delivers only that selected last capture
to the production inbox. The original prototype file remains untouched. Alternatively,
copy a specifically chosen `<capture_id>.json` file into the production inbox yourself;
do not bulk-move the test directory or change UUIDs merely to force a retry.

## Manual end-to-end verification

1. Keep the desktop app closed. Send a Unicode title such as `勇者 — ÉTÉ 🌸` from
   the extension. Verify the production JSON file exists.
2. Launch JAnimeVault using the usual shortcut. Expect a dismissible **Browser
   captures** summary in the bottom status bar with one addition. Choose its
   **Planned** action to view it (the action is hidden when already in Planned).
3. Verify the title, placeholder, search, editing, confirmed deletion, and existing
   **Link AniList entry...** action. Import itself must not perform an AniList lookup.
4. Verify the committed capture file was removed from the production inbox. The
   exact original text is retained in the database receipt.
5. Close the desktop, retry the same UUID in the extension, and reopen it. Expect
   **already imported** in the desktop summary and no duplicate Planned entry.
   If the helper returns `saved` on this retry, that is normal: the importer already
   removed the earlier inbox file. The persistent receipt prevents duplicate import.
6. Edit or delete that Planned entry, then repeat delivery of the same UUID. It must
   neither overwrite the edited title nor resurrect the deleted entry. A genuinely
   new capture uses a new UUID and is an independent entry, even for identical text.
7. Send while the desktop is already open: enter Planned from another view, or click
   **Refresh imports** if it is already open. Verify new entries appear without
   changing the search or losing your current Grid/List browsing anchor. The button
   is disabled during a check. Captures arriving after a check's inbox snapshot may
   require another explicit check. No automatic AniList matching occurs.
8. Optionally put a deliberately malformed `broken.json` in the production inbox
   beside a valid capture. Restart: the valid capture imports, the failed count is
   visible, and `broken.json` stays available for diagnosis. Remove only your
   deliberately created test file afterward, or correct it to the validated schema
   and UUID filename. Interrupted `.tmp` files are ignored, not imported or deleted.

Browser registration, native delivery, and console visibility need manual browser
verification after updating the helper. Automated tests never use the real database
or existing prototype captures and do not modify the registry.

## Storage and failure behavior

The additive `planned_import_receipts` table stores:

- `capture_id`: primary key / idempotency key.
- `planned_id`: the independent Planned UUID created for the capture.
- `schema_version`, `capture_type`, `captured_text`, `captured_at`: original capture
  fields, including the exact Unicode text and surrounding whitespace.
- `imported_at`: receipt creation timestamp.

Only the Planned display title is trimmed, matching existing manual creation.
Receipts remain after Planned editing, linking, unlinking, deletion, source removal,
and re-indexing. They intentionally have no cascading relationship to indexed rows.
Original text is recoverable using a read-only query such as:

```sql
SELECT capture_id, planned_id, captured_text, captured_at, imported_at
FROM planned_import_receipts;
```

Receipt verification, Planned insertion, and receipt insertion share one SQLite
transaction. A capture is removed only after commit. If cleanup fails or the app
exits after commit, the receipt prevents a duplicate on the next startup. Conflicting
content under an existing UUID is rejected and kept, not overwritten.

The importer shares the native host's strict schema/size validation. It does not
follow linked files or linked inbox directories, traverse subdirectories, accept
nonmatching UUID filenames, or process temporary files. Planned displays user titles
as plain text rather than interpreting HTML-like selections.

Failures are counted in the bottom status bar using diagnostic categories such as
`invalid_message`, `id_conflict`, `database_error`, or `file_error`. Failed files stay
in the inbox and are retried on the next startup; they do not block valid files.
Standard Python diagnostics also report filenames/codes without captured text.
There is no import-specific persistent log file or automatic repair of malformed data.

Same-user malicious filesystem manipulation is not a security boundary. As in the
native helper, atomicity and hardware power-loss durability depend on the filesystem
and SQLite. Two app instances serialize receipt creation, preventing duplicate rows;
they may both observe a file and one may report that cleanup is pending.

## Offline validation

```powershell
python -m unittest discover -s tests -p 'test_*.py' -v
python -m unittest discover -s prototypes/native_messaging/tests -p 'test_*.py' -v
```

Tests cover isolated databases/inboxes, atomic rollback, post-commit cleanup failure,
restarts, UUID conflicts, Unicode, malformed and oversized captures, temporary files,
source independence, native-host production delivery/redelivery, UI-thread updates,
and headless Main.qml loading. The real symlink test is skipped when Windows does
not grant symlink creation; a mocked reparse-point rejection test still runs.

Some earlier Qt environments reproduced Basic MenuItem `max` warnings. The import
check allows only those known warnings and fails on other QML warnings; warning
counts depend on the Qt environment, and no production warnings are suppressed.

Import itself does not match AniList titles automatically. Imported entries can use
the existing [explicit AniList candidate search](planned_anilist_search.md) afterward.
Browser [context-menu capture](browser_selection_capture.md) and the optional
[floating selection button](browser_floating_capture.md) are implemented separately.
