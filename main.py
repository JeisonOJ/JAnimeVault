"""Application entry point for the PySide6/QML desktop interface."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from ui.controller import AppController

APP_NAME = "JAnimeVault"
WINDOWS_APP_ID = "JAnimeVault.Desktop"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "branding" / "janimevault.ico"


def set_windows_app_id() -> None:
    """Give the source-launched window its own stable taskbar identity."""
    if sys.platform == "win32":
        import ctypes
        set_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [ctypes.c_wchar_p]
        set_id.restype = ctypes.c_long
        result = set_id(WINDOWS_APP_ID)
        if result != 0:
            raise OSError(f"Could not set Windows application identity: HRESULT {result:#x}")


def run() -> int:
    set_windows_app_id()
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    engine = QQmlApplicationEngine()
    controller = AppController()
    engine.rootContext().setContextProperty("controller", controller)
    qml_file = Path(__file__).parent / "ui" / "qml" / "Main.qml"
    engine.load(str(qml_file))
    if not engine.rootObjects():
        controller.shutdown()
        return 1
    controller.start()
    app.aboutToQuit.connect(controller.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
