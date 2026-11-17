"""Real archive controller and QML, isolated SQLite; no startup/polling/cover worker."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ArchiveUITests(unittest.TestCase):
    def test_archive_workflow_offscreen(self):
        result = subprocess.run([sys.executable, '-B', __file__, '--scenario'],
            cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen', QML_DISABLE_DISK_CACHE='1'),
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Archive UI/controller checks passed', result.stdout)


def scenario():
    import tempfile
    import time
    from unittest.mock import Mock, patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QUrl, QObject
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlEngine, QQmlComponent, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    import indexer.database as database
    from indexer.service import IndexerService
    from indexer.archive_tracking import ArchiveTracker
    from ui.controller import AppController
    app = QGuiApplication([])
    QQuickStyle.setStyle('Basic')
    with tempfile.TemporaryDirectory() as temp:
        database.APP_DIR = Path(temp)
        database.DB_PATH = Path(temp)/'index.db'
        store = database.IndexStore()
        temporary = store.register_folder_source(str(Path(temp)/'temporary'))[0]
        destination = store.register_folder_source(str(Path(temp)/'archive'))[0]
        extra = store.register_folder_source(str(Path(temp)/'second-archive'))[0]
        def seed(sid, folder, filenames):
            import ntpath
            paths = [folder] + [folder+'\\'+name for name in filenames]
            store.replace_drive(sid, 'Fixture', 'X:', [(sid,p,ntpath.dirname(p),ntpath.basename(p),int(i==0),None,None) for i,p in enumerate(paths)])
        seed(temporary, 'Anime Season 2', ['01.mkv','1.rar','06.5.rar','unknown.mkv'])
        seed(destination, 'Nested\\Anime Season 2', ['01.mp4','02.mkv'])
        service = IndexerService(store)
        with patch('ui.controller.IndexerService', return_value=service), patch('ui.controller.CoverService') as cover, patch.object(AppController, '_request_items'):
            c = AppController()
        engine = QQmlEngine()
        engine.rootContext().setContextProperty('controller', c)
        warnings = []
        engine.warnings.connect(lambda errors: warnings.extend(e.toString() for e in errors))
        component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path('ui/qml/Main.qml').resolve())))
        window = component.create()
        assert window, component.errors()
        retained = []
        def evaluate(obj, expression):
            exp = QQmlExpression(engine.contextForObject(obj), obj, expression)
            value, _ = exp.evaluate()
            assert not exp.hasError(), exp.error().toString()
            retained.append(value)
            return value
        def wait(predicate):
            end = time.monotonic()+8
            while time.monotonic()<end:
                app.processEvents()
                if predicate(): return
                time.sleep(.01)
            raise AssertionError((c.archiveState, c.archivePicker, c.archiveDetail, warnings))
        def visual(obj, name):
            if obj.objectName() == name: return obj
            for child in obj.childItems() if hasattr(obj, 'childItems') else []:
                found = visual(child, name)
                if found is not None: return found
            return None
        try:
            # Actual sidebar navigation action, not direct view-property mutation.
            nav = window.findChild(QObject, 'archiveNavigation')
            assert nav
            evaluate(nav, 'clicked()')
            wait(lambda: c.currentView == 'to_archive' and not c.archiveState['busy'])
            view = window.findChild(QObject, 'archiveView')
            assert view
            assert not c.archiveSource and not c.archiveState['items']
            assert window.findChild(QObject, 'archiveEmptyState').property('visible')
            # Existing menu action labels/order remain; designation is appended.
            text = Path('ui/qml/Main.qml').read_text(encoding='utf-8')
            start = text.index('id: sourceMenu\n')
            menu = text[start:text.index('RowLayout {', start)]
            assert menu.index('Open in File Explorer') < menu.index('Re-index') < menu.index('Rename') < menu.index('Remove from Library') < menu.index('Use as To Archive source')
            # First source is visible in sidebar and its real menu action can designate it.
            index = next(i for i, row in enumerate(c.drives) if row['id']==temporary)
            evaluate(window, f'driveList.positionViewAtIndex({index}, ListView.Beginning); driveList.forceLayout()')
            delegate = evaluate(window, f'driveList.itemAtIndex({index})')
            assert delegate
            evaluate(delegate, 'sourceMenu.itemAt(5).triggered()')
            wait(lambda: c.archiveSource == temporary and not c.archiveState['busy'] and len(c.archiveState.get('items',[]))==1)
            row = c.archiveState['items'][0]
            assert row['state']=='suggested' and row['counts'] is None
            row_view = visual(view, 'archiveRows')
            assert row_view
            evaluate(row_view, 'forceLayout()')
            wait(lambda: visual(view, 'archiveConfirmSuggestion') is not None)
            button = visual(view, 'archiveConfirmSuggestion')
            evaluate(button, 'clicked()')
            wait(lambda: not c.archiveState['busy'] and c.archiveState['items'][0]['state']=='confirmed')
            row = c.archiveState['items'][0]
            assert row['counts']=={'archived':2,'pending':1,'both':1,'unknown':1}, row
            assert row['review'] and row['destination']['connected'] is False
            wait(lambda: visual(view, 'archiveEpisodeToggle') is not None)
            evaluate(visual(view, 'archiveEpisodeToggle'), 'clicked()')
            wait(lambda: not c.archiveDetail['busy'])
            assert '06.5.rar' in c.archiveDetail['text'] and 'unknown.mkv' in c.archiveDetail['text']
            assert '06.5:' in c.archiveDetail['text'] and '01:' in c.archiveDetail['text']
            assert '1.rar' in c.archiveDetail['text'] and '01.mkv' in c.archiveDetail['text']
            # Open actual picker and search/confirm an indexed folder manually.
            seed(extra, 'Different title', ['01.rar'])
            c._refresh_drives()
            wait(lambda: not c.archiveState['busy'])
            evaluate(view, 'chooseDestination(backend.archiveState.items[0])')
            dialog = window.findChild(QObject, 'archiveDestinationDialog')
            assert dialog and dialog.property('visible')
            field = window.findChild(QObject, 'archiveDestinationSearch')
            field.setProperty('text', 'Different')
            evaluate(field, 'accepted()')
            wait(lambda: not c.archivePicker['busy'] and len(c.archivePicker.get('items',[]))==1)
            picked = c.archivePicker['items'][0]
            popup_content = evaluate(dialog, 'contentItem')
            wait(lambda: visual(popup_content, 'archivePickerConfirm') is not None)
            evaluate(visual(popup_content, 'archivePickerConfirm'), 'clicked()')
            wait(lambda: not c.archiveState['busy'] and c.archiveState['items'][0].get('destination',{}).get('source_id')==extra)
            # Missing mapping is never silently replaced.
            seed(extra, 'Other', [])
            c.refreshArchive()
            wait(lambda: not c.archiveState['busy'])
            assert c.archiveState['items'][0]['state']=='invalid_destination'
            c.unlinkArchiveDestination('Anime Season 2', temporary)
            wait(lambda: not c.archiveState['busy'] and c.archiveState['items'][0]['state']=='suggested')
            # Multiple candidates remain unconfirmed.
            seed(extra, 'Anime Season 2', [])
            c.refreshArchive(); wait(lambda: not c.archiveState['busy'])
            assert c.archiveState['items'][0]['state']=='multiple_candidates'
            # Search isolation and navigation preserve previous source selection.
            selected = c.selectedDrive
            c.search('No such anime'); wait(lambda: not c.archiveState['busy'])
            assert not c.archiveState['items']
            c.selectFavorites(); c.selectToWatch(); c.selectPlanned(); c.selectToArchive()
            wait(lambda: not c.archiveState['busy'])
            assert c.searchTerm == 'No such anime' and c.selectedDrive == selected
            window.setProperty('gridView', False)
            window.setProperty('width', 900)
            window.setProperty('height', 600)
            app.processEvents()
            assert not evaluate(window, 'mediaList.visible') and not evaluate(window, 'mediaGrid.visible')
            assert visual(window.findChild(QObject, 'archiveView'), 'archiveRows').property('height') > 100
            c.clearSearch(); wait(lambda: not c.archiveState['busy'])
            # Superseded and foreign-view results cannot overwrite current state.
            old = c.archiveState
            token = c._archive_tokens['overview']
            c._apply_archive(c._query_number-1, 'overview', token, {'items':['stale']})
            assert c.archiveState is old
            c._apply_archive(c._query_number, 'overview', token-1, {'items':['stale']})
            assert c.archiveState is old
            c.cancelArchivePicker()
            c._apply_archive(c._query_number, 'picker', c._archive_tokens['picker']-1, {'items':['stale']})
            assert c.archivePicker.get('items') != ['stale']
            # Source switching invalidates old dialog identities and clears designation.
            c.designateArchiveSource(destination)
            wait(lambda: c.archiveSource==destination and not c.archiveState['busy'])
            c.confirmArchiveDestination('Anime Season 2', extra, 'Anime Season 2', temporary)
            assert 'source changed' in c.status
            c.designateArchiveSource('')
            wait(lambda: not c.archiveSource and not c.archiveState['busy'])
            c.selectLibrary()
            assert c.currentView=='source'
            cover.return_value.enqueue.assert_not_called()
            cover.return_value.link_folder.assert_not_called()
            assert not warnings, warnings
            print('Archive UI/controller checks passed')
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()
            del component, engine
            c.shutdown()
            store.conn.close()


if __name__ == '__main__':
    if '--scenario' in sys.argv: scenario()
    else: unittest.main()
