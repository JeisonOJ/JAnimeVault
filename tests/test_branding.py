"""Branding-only checks: real Qt startup, isolated data, no polling or networking."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BrandingTests(unittest.TestCase):
    def test_icon_resources(self):
        from PySide6.QtGui import QImage
        assets = ROOT / "assets" / "branding"
        original = QImage(str(assets / "janimevault.jpg"))
        png = QImage(str(assets / "janimevault.png"))
        self.assertFalse(original.isNull())
        self.assertEqual((png.width(), png.height()), (2048, 2048))
        self.assertFalse(png.hasAlphaChannel())
        # Different JPEG decoders can round a channel by one; check full geometry and background.
        self.assertEqual(original.size(), png.size())
        self.assertLess(png.pixelColor(0, 0).lightness(), 10)
        data = (assets / "janimevault.ico").read_bytes()
        self.assertEqual(struct.unpack_from("<HHH", data), (0, 1, 7))
        sizes = []
        for index in range(7):
            width, height, _, _, _, _, length, offset = struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
            size = (width or 256, height or 256)
            image = QImage.fromData(data[offset:offset + length])
            self.assertFalse(image.isNull())
            self.assertEqual((image.width(), image.height()), size)
            self.assertFalse(image.hasAlphaChannel())
            sizes.append(size[0])
        self.assertEqual(sorted(sizes), [16, 24, 32, 48, 64, 128, 256])

    def test_isolated_startup(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--startup"],
            cwd=ROOT, env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1",
                              PYTHONDONTWRITEBYTECODE="1"), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("JAnimeVault startup passed", result.stdout)


def startup():
    import tempfile
    from unittest.mock import Mock, patch
    sys.path.insert(0, str(ROOT))
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    import indexer.database as db
    from indexer.service import IndexerService
    from ui.controller import AppController
    import main

    runtime = ROOT / "tests" / ".branding-test-runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as directory:
        db.APP_DIR = Path(directory)
        db.DB_PATH = db.APP_DIR / "index.db"
        store = db.IndexStore()
        retained, failures, warnings = [], [], []
        set_id = main.set_windows_app_id

        def identity():
            assert QGuiApplication.instance() is None
            set_id()  # Test real Windows API before Qt/window construction.

        def controller():
            with patch("ui.controller.IndexerService", return_value=IndexerService(store)), patch("ui.controller.CoverService"):
                value = AppController()
            value.start = Mock()  # Never poll drives, import, or start background indexing.
            retained.append(value)
            return value

        def engine():
            value = QQmlApplicationEngine()
            value.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
            retained.append(value)
            def verify():
                try:
                    app = QGuiApplication.instance()
                    assert app.applicationName() == app.applicationDisplayName() == "JAnimeVault"
                    icon = app.windowIcon()
                    assert not icon.isNull()
                    assert sorted(s.width() for s in icon.availableSizes()) == [16, 24, 32, 48, 64, 128, 256]
                    window = value.rootObjects()[0]
                    assert window.title() == "JAnimeVault"
                    assert any(obj.property("text") == "JAnimeVault" for obj in window.findChildren(QObject))
                    assert main.ICON_PATH.is_absolute()
                    assert not store.drives()
                except Exception as error:
                    failures.append(repr(error))
                finally:
                    QGuiApplication.instance().quit()
            QTimer.singleShot(200, verify)
            return value

        with patch("main.set_windows_app_id", side_effect=identity), patch("main.AppController", side_effect=controller), patch("main.QQmlApplicationEngine", side_effect=engine):
            assert main.run() == 0
        assert not failures, failures
        assert not [w for w in warnings if "Controls/Basic/MenuItem.qml:12:" not in w], warnings
        retained[1].start.assert_called_once()
        store.conn.close()
    print("JAnimeVault startup passed")


if __name__ == "__main__":
    if "--startup" in sys.argv:
        startup()
    else:
        unittest.main()
