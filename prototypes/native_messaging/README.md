# Planned browser capture: source Native Messaging host

The legacy directory name and host identifier are preserved for compatibility.
Use the [source setup guide](../../docs/browser_extension_setup.md) for Edge/Chrome
registration and production capture delivery.

- `host.py`: standard-library-only, one-request binary Native Messaging host.
  It validates exact extension origins and capture schema, saves atomically,
  preserves Unicode and treats duplicate capture UUIDs idempotently.
- `prepare_host.py`: copies the host and shared schema to the per-user helper
  directory, writes a launcher using the selected Python executable and generates
  per-browser registration files. It never changes the registry automatically.
- `extension/`: Manifest V3 popup, selection context menu and optional per-site
  floating button. One durable pending message, with explicit same-UUID retry.
- `tests/`: isolated protocol, filesystem, launcher and optional Node checks.
  No real registry, browser, desktop initialization or user data is needed.

The source host uses `NativeMessagingPrototype` configuration for compatibility.
Its default test inbox is separate from the application's production inbox.
Explicitly select `--inbox production` to deliver captures to Planned.
The helper has no SQLite, Qt, AniList or desktop dependency.

From the project root:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s prototypes/native_messaging/tests -p "test_*.py"
```

The generated launcher runs Python with `-I -u`, retains browser arguments and
needs binary stdin/stdout. Diagnostics use stderr; stdout is protocol-only.
Keep the selected Python environment available. Selection alone never sends text.

The desktop imports completed captures at startup, when entering Planned, or on
**Refresh imports**. Delivery while it is closed is supported; a saved response
means inbox delivery, not database import. See [import/recovery](../../docs/browser_capture_import.md)
and [floating permissions](../../docs/browser_floating_capture.md).
