"""To Archive ordering/reindex regressions and actual offscreen wheel events."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_archive_tracking as fixtures
from indexer.archive_tracking import ArchiveTracker
from ui.archive_presentation import overview_page, priority
from ui.controller import AppController
from indexer.service import IndexerService


class ArchiveUsabilityTests(unittest.TestCase):
    setUp = fixtures.ArchiveTests.setUp
    tearDown = fixtures.ArchiveTests.tearDown
    seed = fixtures.ArchiveTests.seed

    def test_global_priority_across_pages_without_mapping_changes(self):
        folders = ['A Complete', 'B Review', 'C Pending', 'Z Unmatched'] + [f'D Unmatched {i:02}' for i in range(26)]
        self.seed(self.temp_id, folders, ['A Complete/01.mkv', 'B Review/unknown.mkv', 'C Pending/02.rar'])
        self.seed(self.dest_id, ['A Complete','B Review','C Pending'], ['A Complete/01.mp4','C Pending/01.mkv'])
        for name in folders[:3]: self.tracker.confirm(name, self.dest_id, name)
        before = self.store.conn.execute('SELECT * FROM archive_folder_links ORDER BY normalized_path').fetchall()
        first = overview_page(self.tracker)
        second = overview_page(self.tracker, first['nextAfter'])
        combined = first['items']+second['items']
        self.assertEqual(len(combined),30)
        self.assertEqual([r['priority'] for r in combined], sorted(r['priority'] for r in combined))
        self.assertEqual([r['temporary']['name'] for r in combined[-4:]], ['Z Unmatched','C Pending','B Review','A Complete'])
        self.assertEqual(second['offset'],25)
        self.assertEqual(self.store.conn.execute('SELECT * FROM archive_folder_links ORDER BY normalized_path').fetchall(),before)
        self.assertEqual(overview_page(self.tracker,term='Pending')['items'][0]['temporary']['name'],'C Pending')

    def test_unknown_counts_are_not_zero_pending(self):
        self.assertEqual(priority({'state':'confirmed','comparison':None}),2)
        self.assertEqual(priority({'state':'no_match','comparison':None}),0)
        self.assertEqual(priority({'state':'confirmed','comparison_state':'review',
                                   'comparison':{'counts':{'pending':1}}}),1)

    def test_reindex_targets_designation_and_completion_refreshes(self):
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication([])
        service = IndexerService(self.store)
        with patch('ui.controller.IndexerService',return_value=service), patch('ui.controller.CoverService'), patch.object(AppController,'_request_items'):
            c = AppController()
        c._request_items = Mock()
        c._selected_drive = self.dest_id
        try:
            normal = [r[0] for r in self.store.folder_sources()]
            self.assertEqual(c.drives[0]['id'],self.temp_id)
            with patch.object(service,'reindex',return_value=False) as reindex:
                c.selectToArchive(); reindex.assert_not_called()
                c.reindexArchiveSource(); reindex.assert_called_once_with(self.temp_id)
                self.assertIn('available',c.status)
            def start(source):
                service.indexing.add(source); return True
            with patch.object(service,'reindex',side_effect=start) as reindex:
                c.reindexArchiveSource()
                self.assertEqual(c.statusMode,'operation')
                self.assertTrue(c.archiveSourceStatus['indexing'])
                c.reindexArchiveSource(); self.assertEqual(reindex.call_count,1)
            service.indexing.clear()
            c._request_items.reset_mock()
            c._handle_events([('complete',self.temp_id,'Temp','',3)])
            c._request_items.assert_called()
            c._archive_after = '25'; c._archive_state['offset'] = 25
            c.refreshArchive(); self.assertEqual(c._archive_after,'25')
            c.selectToArchive(); self.assertEqual(c._archive_after,'25')
            c.previousArchivePage(); self.assertEqual(c._archive_after,'0')
            with patch.object(service,'reindex',side_effect=OSError('offline')):
                c.reindexArchiveSource(); self.assertIn('offline',c.status)
            self.tracker.designate(None); c._refresh_drives()
            self.assertEqual([r['id'] for r in c.drives],normal)
        finally: c.shutdown()


class ArchiveScrollTests(unittest.TestCase):
    def test_model_context_and_nested_wheel_events(self):
        p = subprocess.run([sys.executable,'-B',__file__,'--qml'],cwd=Path(__file__).resolve().parents[1],
            env=dict(os.environ,QT_QPA_PLATFORM='offscreen',QML_DISABLE_DISK_CACHE='1'),
            text=True,capture_output=True,timeout=35)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn('Scroll/copy preservation and wheel boundaries passed',p.stdout)


def qml_scenario():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QUrl, QPoint, QPointF, Qt, QCoreApplication
    from PySide6.QtGui import QGuiApplication, QWheelEvent
    from PySide6.QtQml import QQmlEngine, QQmlComponent, QQmlExpression
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest
    from types import SimpleNamespace
    app = QGuiApplication([]); QQuickStyle.setStyle('Basic')
    engine = QQmlEngine(); warnings=[]
    engine.warnings.connect(lambda errors:warnings.extend(e.toString() for e in errors))
    def row(i):
        return {'state':'confirmed','temporary':{'source_id':'temp','path':f'Anime {i:02}','name':f'Anime {i:02}'},
                'destination':{'source_id':'dest','label':'Archive','path':f'Nested/Anime {i:02}',
                               'connected':False,'last_indexed':'2026-01-01'},
                'counts':{'archived':10,'pending':1,'both':1,'unknown':1},'review':True,'coverUrl':''}
    records=[row(i) for i in range(25)]
    fields={'archiveState':{'items':records,'context':'temp\nsearch','offset':25,'nextAfter':'50','summary':'Page 2','busy':False},
            'archiveSource':'temp','archiveSourceStatus':{'connected':True,'indexing':False},
            'archiveDetail':{'path':'','busy':False,'text':''},'archivePicker':{'items':[],'busy':False}}
    code='import QtQuick\nQtObject { signal archiveChanged(); property string copied: ""; property int detailLoads:0; property int reindexCalls:0;\n'
    code+='\n'.join(f'property var {k}: {json.dumps(v)}' for k,v in fields.items())
    code+='''
        function loadArchiveDetails(path) {
            detailLoads++
            archiveDetail={path:path,sourceId:'temp',text:Array.from({length:120},(_,i)=>'Episode '+i+' — original filename.mkv').join('\\n'),busy:false}; archiveChanged()
        }
        function copyFolderName(name) { copied=name }
        function refreshArchive() {} function designateArchiveSource(id) {} function reindexArchiveSource() { reindexCalls++ }
        function nextArchivePage() {} function previousArchivePage() {} function cancelArchivePicker() {}
    }'''
    component=QQmlComponent(engine); component.setData(code.encode(),QUrl()); backend=component.create()
    assert backend,component.errors()
    engine.rootContext().setContextProperty('fixture',backend)
    window_component=QQmlComponent(engine)
    window_component.setData(b'import QtQuick\nimport QtQuick.Controls\nApplicationWindow { visible:true; width:1000; height:740; ArchiveView { objectName:"harness"; anchors.fill:parent; anchors.margins:16; backend:fixture } }',QUrl.fromLocalFile(str(Path('ui/qml/ArchiveHarness.qml').resolve())))
    window=window_component.create(); assert window,window_component.errors()
    retained=[]
    def ev(obj,code):
        expression=QQmlExpression(engine.contextForObject(obj),obj,code)
        value,_=expression.evaluate(); assert not expression.hasError(),expression.error().toString()
        retained.append(value); return value
    def find(obj,name):
        if obj.objectName()==name:return obj
        for child in obj.childItems() if hasattr(obj,'childItems') else []:
            found=find(child,name)
            if found is not None:return found
    def settle(): QTest.qWait(70)
    # Find through the visual tree (delegates need not be QObject descendants).
    root=find(window.contentItem(),'harness'); assert root
    outer=find(root,'archiveRows'); settle(); ev(outer,'forceLayout()')
    reindex=find(root,'archiveReindex'); assert reindex.property('enabled')
    ev(reindex,'clicked()'); assert backend.property('reindexCalls')==1
    backend.setProperty('archiveSourceStatus',{'connected':True,'indexing':True}); settle()
    assert not reindex.property('enabled') and reindex.property('text')=='Indexing…'
    backend.setProperty('archiveSourceStatus',{'connected':False,'indexing':False}); settle()
    assert not reindex.property('enabled')
    ev(outer,'positionViewAtIndex(3,ListView.Beginning); forceLayout()'); settle()
    card=ev(outer,'itemAtIndex(3)'); assert card
    ev(find(card,'archiveEpisodeToggle'),'clicked()'); settle()
    inner=find(card,'archiveEpisodeScroll'); assert inner.property('contentHeight')>inner.property('height')
    ev(find(card,'archiveCopyName'),'clicked()'); assert backend.property('copied')=='Anime 03'
    backend.setProperty('copied','')
    point=card.mapToScene(QPointF(20,20)).toPoint()
    QTest.mouseClick(window,Qt.RightButton,Qt.NoModifier,point); settle()
    menu=card.findChild(type(backend),'archiveCopyMenu')
    assert menu and menu.property('visible'),'Right-click copy menu did not open'
    ev(menu,'itemAt(0).triggered(); close()'); assert backend.property('copied')=='Anime 03'
    # The real controller clipboard helper receives only the folder name.
    AppController.copyFolderName(SimpleNamespace(_set_status=Mock()),'Anime 03')
    assert app.clipboard().text()=='Anime 03'
    ev(outer,'positionViewAtIndex(3,ListView.Beginning); forceLayout()'); settle()
    offset=card.property('y')-outer.property('contentY')
    identity=card
    # Re-sort an updated item while preserving page/search and the reader's anchor.
    changed=records.copy(); changed[3]=dict(changed[3],counts={'archived':12,'pending':0,'both':2,'unknown':1})
    backend.setProperty('archiveState',dict(fields['archiveState'],items=changed)); ev(backend,'archiveChanged()'); settle()
    assert backend.property('detailLoads')==2, 'Expanded comparison was not refreshed'
    assert ev(outer,'itemAtIndex(3)') is identity, 'Delegate recreated on metadata-only change'
    changed.insert(10,changed.pop(3))
    state=dict(fields['archiveState'],items=changed)
    backend.setProperty('archiveState',state); ev(backend,'archiveChanged()'); settle(); ev(outer,'forceLayout()')
    updated=ev(outer,'itemAtIndex(10)'); assert updated
    assert updated.property('expanded')
    assert abs(updated.property('y')-outer.property('contentY')-offset)<3, (updated.property('y'),outer.property('contentY'),offset)
    assert backend.property('archiveState')['offset']==25 and backend.property('archiveState')['context']=='temp\nsearch'
    assert 'original filename' in find(updated,'archiveEpisodeDetails').property('text')
    # Return to an expanded mid-page row so the outer page can scroll both ways.
    ev(outer,'positionViewAtIndex(5,ListView.Beginning); forceLayout()'); settle()
    card=ev(outer,'itemAtIndex(5)'); ev(find(card,'archiveEpisodeToggle'),'clicked()'); settle()
    inner=find(card,'archiveEpisodeScroll')
    ev(outer,'positionViewAtIndex(5,ListView.Beginning); forceLayout()'); settle()
    def wheel(delta,pixels=0):
        point=inner.mapToScene(QPointF(inner.property('width')/2,inner.property('height')/2))
        global_point=QPointF(window.mapToGlobal(point.toPoint()))
        event=QWheelEvent(point,global_point,QPoint(0,pixels),QPoint(0,delta),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
        QCoreApplication.sendEvent(window,event); settle()
    inner.setProperty('contentY',100.0); before=outer.property('contentY')
    wheel(-120)
    assert abs(inner.property('contentY')-148)<2 and abs(outer.property('contentY')-before)<2, ('down',inner.property('contentY'),before,outer.property('contentY'))
    wheel(120)
    assert abs(inner.property('contentY')-100)<2 and abs(outer.property('contentY')-before)<2
    bottom=inner.property('originY')+inner.property('contentHeight')-inner.property('height')
    inner.setProperty('contentY',bottom); wheel(-120)
    assert abs(inner.property('contentY')-bottom)<2 and abs(outer.property('contentY')-before-48)<2, 'Lower boundary trapped/doubled wheel'
    inner.setProperty('contentY',inner.property('originY')); before=outer.property('contentY'); wheel(120)
    assert abs(outer.property('contentY')-before+48)<2 and abs(inner.property('contentY')-inner.property('originY'))<2, 'Upper boundary trapped/doubled wheel'
    inner.setProperty('contentY',100.0); before=outer.property('contentY'); wheel(0,-15)
    assert abs(inner.property('contentY')-115)<2 and abs(outer.property('contentY')-before)<2, 'Pixel scrolling'
    ev(inner,'forceActiveFocus()'); inner.setProperty('contentY',0.0)
    QTest.keyClick(window,Qt.Key_PageDown); settle()
    assert inner.property('contentY')>0, 'Keyboard episode scrolling'
    assert not warnings,warnings
    print('Scroll/copy preservation and wheel boundaries passed')
    window.close(); window.deleteLater(); app.processEvents()


if __name__=='__main__':
    if '--qml' in sys.argv:qml_scenario()
    else:unittest.main()
