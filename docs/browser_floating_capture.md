# Stage 3D: opt-in floating selection button

## Enable in Edge or Chrome

1. Reload the existing unpacked extension at `edge://extensions` or
   `chrome://extensions`. Accept the `scripting` permission if the browser requests it.
   Keep the existing extension ID, native registration and production inbox configuration.
2. Open the extension popup. Enter an exact website origin, for example
   `https://www.youtube.com`, `https://x.com`, or `https://example.com`.
   Do not include a page path, credentials, wildcard, query, or custom port.
3. Click **Enable on this website**, approve the browser's website-access prompt,
   then reload that webpage. Selecting ordinary text now shows **+ Add to Planned**.
   Selection alone does not save or send selected text.
4. Click the button, or Tab to it and activate it with Enter/Space. The button shows
   inbox acknowledgement, browser-only queueing, or an error. Check the popup for
   recovery/retry. Acknowledgement does not mean the desktop has imported the entry.
5. To revoke, enter the same origin and click **Revoke this website**. Browser extension
   site-access controls can also remove grants. Existing overlays receive a teardown
   message; captures are independently denied after revocation. Reload open pages
   to unload remaining content-script instances. Removing a grant conservatively
   stops all currently injected overlays; reload other approved sites to resume them.

No website grants are requested automatically. Optional `http://*/*` and `https://*/*`
manifest declarations allow requesting individual sites, but are NOT all-sites grants.
The popup only requests an exact hostname; subdomains need separate approval.
Browsers may present broader scheme wording or manage withheld site access differently.
If access is denied or withheld, reload after fixing it or use the existing context menu.
Wildcard grants are not used to register overlays. No `activeTab`, history, cookies,
clipboard access, or required all-sites host permissions are requested.

`scripting` registers one persistent, top-frame-only **ISOLATED** content script for
approved origins. Registration is updated on permission changes/install/update.
Enabling requires a page reload, which avoids inspecting or injecting across open tabs.
The existing selection context menu and manual popup work without any overlay grant.

The button is positioned above the visible selection toward its right edge, with
a below-selection fallback and viewport clamping. Scrolling/resizing dismisses it;
reselect text to show it at the updated position. It is not fixed to a window corner.

## Security and privacy

- Webpages remain untrusted, even after approval. The overlay uses safe DOM methods,
  constant styles and closed Shadow DOM. No HTML interpolation, remote script, eval,
  MAIN-world code, or window.postMessage bridge exists.
- Only a trusted button activation can submit. Synthetic clicks, repeated activation
  of a consumed selection, stale selections and concurrent clicks are rejected.
- The worker validates exact message fields, types, character/UTF-8 limits, extension
  identity, top-frame/tab context, HTTP(S) sender origin and current permission.
  Content scripts cannot invoke popup retry, state access, permission configuration,
  or arbitrary native commands. The Python host revalidates the existing capture schema.
- No selected text is sent on selection changes: a permission check without text is
  performed before display. The sender URL is used transiently for permission checking;
  it is not collected, logged, stored in captures, or sent to the native host.
- Captures contain only the exact selected text and existing UUID/time/schema metadata.
  Inputs, password fields, textareas, select controls, contenteditable, textbox roles,
  and recognized Monaco/CodeMirror editors are excluded conservatively. Arbitrary
  proprietary editors cannot all be recognized; keep sensitive websites unapproved.
- Native delivery and durable storage are unchanged. The one-pending-capture limit
  still applies: new captures are rejected rather than overwriting an undelivered one.
  Worker failure requires checking the popup before retrying; a lost acknowledgement
  can safely use the original UUID. No automatic retries or matching are added.

**Browser limitations:** Shadow DOM is styling isolation, not a security boundary.
Pages can remove, obscure, move, or imitate DOM overlays; `isTrusted` does not prove
that a human was not tricked into clicking. The background cannot independently
attest user intent from a content-script message. Keep the context-menu/popup workflow
for hostile or sensitive pages; no DOM-based overlay can fully eliminate clickjacking.
Pages can also alter selection text before a click. Snapshot/current-selection checks
prevent ordinary stale submissions, not malicious page manipulation.

Protected browser/store pages, native PDF viewers, inaccessible or embedded frames,
canvas editors and closed-shadow selections are unsupported. Only the main frame is
injected. Tiny viewports, competing page controls and very high page stacking layers
can still obstruct placement. Scroll, resize, zoom viewport changes, navigation,
selection collapse, outside click and Escape dismiss the button; reselect to show it.
Reload pages after updating the extension to remove old script instances.

## Manual checklist (repeat in both browsers)

- First deny a permission request: no overlay; right-click capture still works.
- Approve YouTube, X, and an ordinary text-heavy site individually, then reload.
- Select video titles/descriptions, posts, Japanese/accented titles, emoji, punctuation
  and multiline text. Check captured JSON/receipt text against the selection.
- Test mouse and keyboard selection, Tab/Enter activation, and rapid double clicks.
- Try screen-edge selections, scrolling, zoom, resize, Escape, outside click, collapse
  and repeated SPA navigation. There should be one overlay and no detached button.
- Verify inputs/editors show no overlay, and unapproved sites receive no script.
- Revoke permission while a page is open: no subsequent overlay delivery; reload.
- With the desktop closed, make several distinct captures, waiting for each acknowledgement.
  Start the desktop and check Planned imports. No AniList lookup should occur.
- Test native-host unavailability: confirm queued feedback, no overwrite of pending
  capture, browser restart persistence and same-UUID popup retry after recovery.
- Reload/restart the browser; approved origins persist. Verify no PowerShell/console
  windows appear. Verify manual popup and context-menu capture still work.

## Offline checks

Run the existing application and transport suites as documented for Stage 3B.
The transport suite also runs mocked worker/permission tests and an isolated DOM
model: exact Unicode, literal script-like text, trusted/synthetic clicks, duplicate
listeners/submissions, stale selections, viewport bounds, dismissal, permission
denial/revocation, forged senders/fields, native failure/retry, and worker recreation.
These mocks do not prove real browser rendering, permission prompts, trusted event
behavior, clickjacking resistance, or browser suspension behavior. Those require the
manual checklist. Desktop data and AniList are not accessed by these tests.

References: [Chrome scripting](https://developer.chrome.com/docs/extensions/reference/api/scripting)
and [optional permissions](https://developer.chrome.com/docs/extensions/reference/api/permissions).
