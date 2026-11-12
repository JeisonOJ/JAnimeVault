"""Offline, real Qt layout/input checks; no controller, database, or network."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class PlannedSearchLayoutTests(unittest.TestCase):
    def test_responsive_dialog(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--scenario"],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QML_DISABLE_DISK_CACHE="1",
                     PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Responsive layout and input checks passed", result.stdout)


def scenario():
    from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlEngine, QQmlComponent, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest

    class Backend(QObject):
        plannedSearchChanged = Signal()

        def __init__(self):
            super().__init__()
            self.state = dict(entryId="entry", query="Original title", results=[], busy=False, error="")
            self.confirmed = []
            self.cancelled = 0
            self.queries = []

        @Property("QVariantMap", notify=plannedSearchChanged)
        def plannedSearch(self):
            return self.state

        @Slot(str)
        def beginPlannedSearch(self, entry_id):
            self.state["entryId"] = entry_id
            self.plannedSearchChanged.emit()

        @Slot(str)
        def searchPlannedCandidates(self, query):
            self.queries.append(query)

        @Slot(int)
        def confirmPlannedCandidate(self, anime_id):
            self.confirmed.append(anime_id)

        @Slot()
        def cancelPlannedSearch(self):
            self.cancelled += 1

        def results(self, count):
            self.state["results"] = [dict(id=i + 1, title="Long Unicode title 日本語 " * 30,
                alternate="Alternate title " * 40, year="2026", format="Metadata " * 40,
                localCoverUrl="") for i in range(count)]
            self.plannedSearchChanged.emit()

    app = QGuiApplication([])
    QQuickStyle.setStyle("Basic")
    backend = Backend()
    engine = QQmlEngine()
    warnings = []
    engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
    engine.rootContext().setContextProperty("testBackend", backend)
    component = QQmlComponent(engine)
    component.setData(b'''
        import QtQuick
        import QtQuick.Controls
        ApplicationWindow {
            width: 900; height: 600; visible: true
            Item {
                width: 400; height: 320
                PlannedSearchDialog { backend: testBackend }
            }
        }
    ''', QUrl.fromLocalFile(str(Path("ui/qml/LayoutTest.qml").resolve())))
    window = component.create()
    assert window is not None, component.errors()
    dialog = window.findChild(QObject, "plannedSearchDialog")
    candidates = dialog.findChild(QObject, "plannedCandidateList")

    def evaluate(obj, expression):
        expr = QQmlExpression(engine.contextForObject(obj), obj, expression)
        value, undefined = expr.evaluate()
        assert not expr.hasError(), expr.error().toString()
        assert not undefined, expression
        return value

    def settle():
        QTest.qWait(100)
        app.processEvents()

    def button(kind):
        return evaluate(dialog, f"standardButton(Dialog.{kind})")

    def click(item):
        QTest.mouseClick(window, Qt.LeftButton, Qt.NoModifier,
                         item.mapToScene(item.boundingRect().center()).toPoint())
        settle()

    def check_bounds():
        width, height = window.width(), window.height()
        assert 0 <= dialog.property("x") <= width - dialog.property("width")
        assert 0 <= dialog.property("y") <= height - dialog.property("height")
        for control in (button("Ok"), button("Cancel"), dialog.findChild(QObject, "candidateQuery")):
            bounds = control.mapRectToScene(control.boundingRect())
            assert bounds.left() >= 0 and bounds.right() <= width, bounds
            assert bounds.top() >= 0 and bounds.bottom() <= height, bounds
        assert evaluate(dialog, "contentItem.y + contentItem.height <= footer.y + 1")
        row = evaluate(candidates, "itemAtIndex(0)")
        if row:
            assert row.height() == 94
            def check_text(item):
                for child in item.childItems():
                    if child.property("text") is not None and child.property("visible"):
                        bounds = child.mapRectToItem(row, child.boundingRect())
                        assert bounds.left() >= 0 and bounds.right() <= row.width() + 1, bounds
                    check_text(child)
            check_text(row)

    backend.results(5)
    evaluate(dialog, 'searchEntry("entry"); true')
    settle()
    assert dialog.property("width") == 720  # not constrained by the 400px Planned parent
    normal_height = dialog.property("height")
    normal_list_height = candidates.height()
    check_bounds()
    assert candidates.property("contentHeight") > candidates.height()
    assert not button("Ok").property("enabled")

    # Search/selection remain explicit; scroll to the last candidate and confirm it.
    query = dialog.findChild(QObject, "candidateQuery")
    query.setProperty("text", "Edited query")
    query.forceActiveFocus()
    QTest.keyClick(window, Qt.Key_Return)
    assert backend.queries == ["Edited query"]
    evaluate(candidates, "positionViewAtEnd(); true")
    settle()
    last = evaluate(candidates, "itemAtIndex(4)")
    click(last)
    assert dialog.property("selectedId") == 5
    assert last.property("color").name() == "#2c2447"
    assert button("Ok").property("enabled")
    assert not backend.confirmed

    # Resize while open, including narrower than the application's 900px minimum.
    for width, height in ((1600, 1000), (900, 600), (640, 420)):
        window.resize(width, height)
        settle()
        assert dialog.property("visible")
        check_bounds()
    window.resize(900, 600)
    settle()
    click(button("Ok"))
    assert backend.confirmed == [5] and not dialog.property("visible")

    # A single candidate uses its natural height rather than a mostly empty list.
    backend.results(1)
    evaluate(dialog, 'searchEntry("entry"); true')
    settle()
    check_bounds()
    assert dialog.property("height") < normal_height - 150
    assert abs(candidates.height() - 94) <= 1
    click(button("Cancel"))
    assert backend.confirmed == [5] and backend.cancelled == 2
    assert not dialog.property("visible")

    # Opening (not just resizing) at a large size and a compact two-result state.
    window.resize(1600, 1000)
    backend.results(5)
    evaluate(dialog, 'searchEntry("entry"); true')
    settle()
    check_bounds()
    assert dialog.property("height") > normal_height
    assert candidates.height() > normal_list_height
    backend.results(2)
    settle()
    check_bounds()
    assert abs(candidates.height() - 194) <= 1
    click(button("Cancel"))
    assert backend.confirmed == [5] and backend.cancelled == 3

    assert not warnings, "\n".join(warnings)
    window.close()
    print("Responsive layout and input checks passed")


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        scenario()
    else:
        unittest.main()
