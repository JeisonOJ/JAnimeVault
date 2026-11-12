# Stage 3C: selection context menu

The Chrome/Edge extension offers **Add to Planned** when the browser supplies a
text selection. This context-menu workflow needs no content script or website
grant. It uses `contextMenus` alongside the existing `nativeMessaging` and `storage`
permissions. The optional floating button separately uses `scripting` and approved
website access; see [its guide](browser_floating_capture.md). Desktop import, native
registration, and inbox paths are unchanged.

## Manual validation (repeat in Edge and Chrome)

1. Open `edge://extensions` or `chrome://extensions` and reload the existing unpacked
   extension. Approve the new context-menu permission if prompted. Keep its directory
   and extension ID unchanged; no host re-registration is needed.
2. Keep the existing Stage 3B production destination configuration. With the desktop
   closed, select a Unicode title on a normal webpage, right-click it, and choose
   **Add to Planned**. No copy/paste should be necessary.
3. Pin the extension for easy access to its status badge. `OK` means the native host
   acknowledged `saved` or `already_saved`; the popup/toolbar tooltip identifies
   production versus prototype inbox. This does NOT confirm a desktop import.
4. Start the desktop. Confirm the title appears in Planned and the import summary
   reflects it. Unicode, punctuation, and whitespace are sent exactly as provided
   in the browser's `selectionText`; the existing importer trims display-title edge
   whitespace while keeping exact captured text in the receipt.
5. Use **Retry same capture UUID** in the extension popup. Restart the desktop:
   no duplicate entry should appear. Two deliberate menu captures of the same text
   after acknowledgement must instead produce distinct UUIDs/Planned entries.
6. Test a missing host in an unregistered browser/profile, or use the previously
   documented explicit unregister procedure only if desired. Menu capture should
   show `Q`: persisted in browser, not acknowledged. Restart the browser or stop its
   worker through developer tools; the popup must retain the original capture/UUID.
   Restore the host and explicitly Retry. No automatic retries are scheduled.
7. While a capture is pending, another menu capture must show `!` and explain that
   it was NOT captured; the pending text must remain unchanged. Retry it first, then
   select the new title again. This stage retains the existing one-pending-capture
   outbox, not a new multi-item queue.
8. Try a blank or >4096-character selection. Expect `!` and an explanation in the
   popup/tooltip, with no new native delivery. Storage failures also report failure,
   never successful queueing. Manual popup entry and retry remain available.

`Q` is queued in durable extension storage, `!` is failure/rejection, and `OK` is
inbox acknowledgement. The last result is saved for the popup even if the worker
is suspended. If termination occurs between persistence and acknowledgement, the
record remains pending and can be retried with the same UUID. A browser crash before
storage completes cannot guarantee capture; reopening the popup verifies the record.
Removing the extension/profile can erase undelivered browser storage.

The browser may normalize webpage selection text before exposing it; the extension
does no further normalization. Protected browser/store pages, native PDF viewers,
custom context menus and embedded frames vary by browser. The action works only
where the browser exposes its extension selection menu and `selectionText`. There
is no content-script workaround; use the existing manual popup when unavailable.
No page URL, browsing history, clipboard data, or frame contents are read.

Offline mocked tests cover menu registration/selection, exact text, distinct UUIDs,
retry identity, native/storage failures, invalid input, pending protection, and worker
recreation with retained storage. Real browser lifecycle/menus require manual testing.

References: [Chrome contextMenus](https://developer.chrome.com/docs/extensions/reference/api/contextMenus)
and [MV3 worker lifecycle](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).

The optional floating selection control is documented separately. Capture/import
does not perform automatic AniList matching, and no HTTP server is used.
