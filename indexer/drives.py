"""Windows USB/removable drive discovery."""

from __future__ import annotations

import ctypes
import subprocess


POLL_SECONDS = 5


def connected_removable_drives() -> dict[str, tuple[str, str, str]]:
    """Return stable-id -> (letter, label, root) for connected USB/removable disks."""
    result: dict[str, tuple[str, str, str]] = {}
    kernel32 = ctypes.windll.kernel32
    # Portable hard disks often report themselves as DRIVE_FIXED. Ask Windows
    # Storage Management which mounted partitions belong to a USB disk as well.
    usb_letters: set[str] = set()
    try:
        command = (
            "Get-Partition | Where-Object DriveLetter | ForEach-Object { "
            "$disk = Get-Disk -Number $_.DiskNumber -ErrorAction SilentlyContinue; "
            "if ($disk.BusType -eq 'USB') { $_.DriveLetter } }"
        )
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            text=True, stderr=subprocess.DEVNULL, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        usb_letters = {line.strip().upper() for line in output.splitlines() if len(line.strip()) == 1}
    except (OSError, subprocess.SubprocessError):
        pass
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(root) != 2 and letter not in usb_letters:  # removable or USB
            continue
        volume_name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong()
        if not kernel32.GetVolumeInformationW(root, volume_name, len(volume_name), ctypes.byref(serial), None, None, None, 0):
            continue
        drive_id = f"{serial.value:08X}"
        result[drive_id] = (f"{letter}:", volume_name.value or "Removable drive", root)
    return result
