# JAnimeVault Windows launch (source checkout)

No EXE build or extra runtime dependency is needed. Keep using the same Python
installation that runs PySide6 successfully in VS Code. Use its `pythonw.exe`
for console-free launching, with the absolute `main.py` path as the argument
and the repository directory as **Start in**. No special Qt environment variables
were set in the inspected launch environment. Do not add Qt plugin path overrides.

## Create a shortcut for a fresh checkout

On the desktop, choose **New > Shortcut** and enter the full interpreter and script
paths, both quoted. For the README's virtual environment, the location is:

```text
"<project>\.venv\Scripts\pythonw.exe" "<project>\main.py"
```

Replace `<project>` with the actual absolute checkout directory; do not type the
placeholder literally. Name it **JAnimeVault**. In its Properties, set **Start in**
to that directory and **Change Icon** to `<project>\assets\branding\janimevault.ico`.
If a shortcut already exists, update it instead of creating a duplicate.

## Update the existing shortcut

1. Close the app. Right-click the existing **JAnimeVault** desktop
   shortcut and open **Properties**. Do not create a second shortcut.
   Older shortcuts may still be named **External Disk Indexer**.
2. Keep **Target**, its `main.py` argument, and **Start in** unchanged.
3. Choose **Change Icon**, browse to this checkout's
   `assets\branding\janimevault.ico`, select it, and apply.
4. Rename the shortcut **JAnimeVault**. Do not rename the checkout or Python packages.
5. Launch it and verify the title, sidebar name, and window/taskbar icon.

## Pin the desktop shortcut to the taskbar

Use this procedure, which was manually verified with the source-based `pythonw.exe`
launch:

1. Close JAnimeVault.
2. Unpin any existing Python or outdated JAnimeVault taskbar entry.
3. Locate the existing **JAnimeVault desktop shortcut**, already configured to use
   `assets\branding\janimevault.ico` from this checkout.
4. Right-click that shortcut and select **Pin to taskbar**. On Windows 11, use
   **Show more options** first if needed.
5. Launch JAnimeVault from the pinned shortcut.
6. Verify that the custom icon remains visible and that the running window groups
   with the pinned icon. Keep only one pin.

**Do not pin the running `pythonw.exe` application directly from its taskbar button.**
That method was observed to replace the custom JAnimeVault icon with the generic
Python icon. Pin the configured desktop shortcut instead.

The application sets `JAnimeVault.Desktop` as its explicit Windows AppUserModelID
before creating Qt's application/window, and loads its window icon from an absolute
path relative to `main.py`. This distinguishes running windows from generic Python
windows. The external shortcut is not edited automatically. Existing pins can retain
old icon/identity metadata; changing a desktop shortcut does not update its pinned
copy. Windows builds and policies can expose pinning commands differently; the
desktop-shortcut procedure above is the verified approach. No extra helper or
packaged EXE is required by that procedure.

If Explorer still shows an old icon after applying/re-pinning, refresh the desktop
or sign out and back in. Do not clear system caches or create duplicate pins just
to force an icon refresh. Keep the checkout at its current location, or update both
the shortcut target and icon path if it moves.

## Diagnose startup problems

Run the same `main.py` argument with `python.exe` beside the shortcut's `pythonw.exe`
from a PowerShell window, after changing to the checkout directory. This exposes
Qt/plugin/import/startup errors that occur before application logging starts.
Existing cover diagnostics remain at
`%LOCALAPPDATA%\ExternalDiskIndexer\logs\covers.log`; they are not a general startup log.

## Artwork and compatibility

`assets/branding/janimevault.jpg` is the approved original. The PNG is a lossless
conversion of its decoded pixels (not a restoration of JPEG compression losses).
The ICO contains 16, 24, 32, 48, 64, 128, and 256 pixel square images, resized with
Lanczos filtering from the full image, without cropping or changing the background.
No transparency or replacement artwork was introduced.

The public name does not rename `%LOCALAPPDATA%\ExternalDiskIndexer`, `index.db`,
cover caches, import inboxes, Native Messaging registration, or internal package names.
