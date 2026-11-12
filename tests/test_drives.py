"""Offline checks for windowless Windows drive discovery."""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from indexer.drives import connected_removable_drives


@unittest.skipUnless(os.name == "nt", "Windows drive discovery")
class DriveCommandTests(unittest.TestCase):
    def detect(self, output=" e \n", error=None):
        kernel = Mock()
        kernel.GetDriveTypeW.side_effect = lambda root: 2 if root == "Z:\\" else 3

        def volume(root, name, length, serial, *unused):
            name.value = "USB" if root == "E:\\" else "Removable"
            serial._obj.value = 123 if root == "E:\\" else 456
            return 1

        kernel.GetVolumeInformationW.side_effect = volume
        with patch("indexer.drives.ctypes.windll", SimpleNamespace(kernel32=kernel)), patch(
            "indexer.drives.subprocess.check_output", return_value=output, side_effect=error
        ) as command:
            result = connected_removable_drives()
        return result, command

    def test_console_hidden_without_changing_command_or_output(self):
        result, command = self.detect()
        self.assertEqual(result, {
            "0000007B": ("E:", "USB", "E:\\"),
            "000001C8": ("Z:", "Removable", "Z:\\"),
        })
        args, kwargs = command.call_args
        self.assertEqual(args[0], ["powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-Partition | Where-Object DriveLetter | ForEach-Object { "
            "$disk = Get-Disk -Number $_.DiskNumber -ErrorAction SilentlyContinue; "
            "if ($disk.BusType -eq 'USB') { $_.DriveLetter } }"])
        self.assertEqual(kwargs, dict(text=True, stderr=subprocess.DEVNULL, timeout=15,
                                     creationflags=subprocess.CREATE_NO_WINDOW))

    def test_existing_subprocess_failure_handling_is_preserved(self):
        for error in (FileNotFoundError("PowerShell unavailable"),
                      subprocess.CalledProcessError(1, "powershell"),
                      subprocess.TimeoutExpired("powershell", 15)):
            with self.subTest(error=error):
                result, _ = self.detect(error=error)
                self.assertEqual(result, {"000001C8": ("Z:", "Removable", "Z:\\")})

    def test_unexpected_errors_are_not_suppressed(self):
        with self.assertRaises(ValueError):
            self.detect(error=ValueError("unexpected"))
