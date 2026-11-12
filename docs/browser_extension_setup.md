# Edge and Chrome setup from source

The extension is optional. The desktop works without it. The source host uses only
Python's standard library and communicates locally through Native Messaging; it
does not run an HTTP server or access the application database.

## 1. Install the desktop environment

Follow the [README](../README.md) to create `.venv` and install requirements.
Keep the environment and checkout available. Browser delivery uses the selected
Python even when JAnimeVault is closed. Use `python.exe`, not `pythonw.exe`, for
host preparation because Native Messaging requires binary stdin/stdout.

## 2. Load the unpacked extension and record your IDs

- Edge: open `edge://extensions`, enable **Developer mode**, choose **Load unpacked**
  and select [prototypes/native_messaging/extension](../prototypes/native_messaging/extension).
- Chrome: open `chrome://extensions` and do the same.

The extension currently appears as **Planned Transport Test**. Copy **ID** from
each browser's extension card/details. IDs can differ between browsers, profiles,
computers and installations, and changing the extension directory can change one.
Use your own IDs: exactly 32 lowercase letters `a`–`p`, not another user's ID.
Managed browser policy may disallow extensions or Native Messaging.

## 3. Check existing registration before changing setup

The shared host name is `com.externaldiskindexer.planned_transport_test`.
Before proceeding, check whether either of these per-user registry locations
already has that host registered (for example, inspect them in Registry Editor):

```text
HKEY_CURRENT_USER\Software\Microsoft\Edge\NativeMessagingHosts\com.externaldiskindexer.planned_transport_test
HKEY_CURRENT_USER\Software\Google\Chrome\NativeMessagingHosts\com.externaldiskindexer.planned_transport_test
```

If a previous/different installation owns the registration, stop and preserve its
default value and type before deliberately switching. Do not delete the shared
key. Preparation also overwrites files in the existing source helper directory;
review and back up that directory first if it already exists. Do not delete either
inbox or the database. Close browsers and helper processes before setup changes.

## 4. Generate source helper files

From PowerShell in the project root, choose **one** command and replace its ID(s):

```powershell
# Edge only
.\.venv\Scripts\python.exe .\prototypes\native_messaging\prepare_host.py --edge-id YOUR_EDGE_EXTENSION_ID --inbox production
```

```powershell
# Chrome only
.\.venv\Scripts\python.exe .\prototypes\native_messaging\prepare_host.py --chrome-id YOUR_CHROME_EXTENSION_ID --inbox production
```

```powershell
# Both browsers: prepare once with both real IDs
.\.venv\Scripts\python.exe .\prototypes\native_messaging\prepare_host.py --edge-id YOUR_EDGE_EXTENSION_ID --chrome-id YOUR_CHROME_EXTENSION_ID --inbox production
```

This copies the host and validation schema, writes a launcher containing your
resolved Python path, and generates `host.json`, `destination.json` and registration
files under `%LOCALAPPDATA%\ExternalDiskIndexer\NativeMessagingPrototype`.
It does **not** change the registry automatically. The default without `--inbox
production` is a separate test inbox, which the desktop does not import.

Review `host.json`: the host name must match above, the launcher must point to the
generated `launch_host.cmd`, and allowed origins must contain your exact extension
IDs (Edge also uses the `chrome-extension://ID/` form). Review `destination.json`
for `production` and the launcher for the intended `python.exe` path. Paths with
spaces/Unicode are quoted by the generator. Keep that Python installation intact.

## 5. Approve registration yourself

After reviewing ownership and file contents, open the generated `register-edge.reg`
and/or `register-chrome.reg` in a text editor. They should change only the matching
HKCU host default value to this directory's `host.json`. If correct, explicitly
approve importing the intended file through Windows. No machine-wide install is
needed; do not use elevation simply to bypass policy restrictions.

If the existing registration already points to this same `host.json`, updating the
helper/destination does not require importing it again. The generator also creates
`unregister-*.reg` files: do **not** import them blindly. They delete a shared key
and can remove another installation's registration. For a later switch, restore
the previously recorded binding only after checking current ownership.

## 6. Test capture and import

Reopen the browser and send a harmless title from the popup. Verify **Production
inbox: saved** or **Production inbox: already_saved**. **Queued** means stored in
the browser, not delivered. Fix delivery and use **Retry same capture UUID**.
The helper can save while the desktop is closed, but it does not create a Planned
entry itself. Open JAnimeVault, enter Planned, or click **Refresh imports**.

Context-menu capture needs no site grant. For the optional floating button, enable
an individual website in the popup, approve access and reload. Selection alone
never submits text. See [permissions and page limits](browser_floating_capture.md).

## Troubleshooting

- Host missing: verify the browser-specific registration, current extension ID,
  generated launcher and saved Python path. Moving/removing `.venv` breaks delivery.
- Unauthorized: regenerate with your current exact IDs, including both browsers if
  configured. Do not use wildcards. Recheck ownership before changing files.
- Delivered but absent: confirm the production destination and use Refresh imports.
  See [capture import/recovery](browser_capture_import.md).
- Browser policy or registry access denied: ask your administrator; do not disable
  protections or overwrite unrelated registrations.
- The helper is not a GUI: double-clicking `host.py` or the launcher is not a
  registration or delivery test. Its stdout is a binary browser protocol.

No setup commands are run by reading these instructions or running the offline
tests. Actual browser registration and end-to-end testing require your approval.
