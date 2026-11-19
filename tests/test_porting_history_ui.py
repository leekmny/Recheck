import json
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from shiboken6 import delete

from recheck.gui import MainWindow
from recheck.models import Settings


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def _manifest(tmp_path, name):
    path = tmp_path / f'{name}.json'
    target = {'entry': 'original.py', 'os': 'windows', 'arch': 'x64', 'runtime': 'python'}
    path.write_text(json.dumps({
        'schema_version': 'recheck-porting-1',
        'targets': {'reference': target, 'candidate': target | {'entry': f'{name}.py'}},
        'scenarios': [{'id': 'edge'}],
    }), encoding='utf-8')
    return path


@pytest.mark.parametrize('worker_saved', [True, False])
def test_history_restores_next_run_setup_without_restoring_build_approval(app, tmp_path, monkeypatch, worker_saved):
    monkeypatch.setenv('RECHECK_DATA_DIR', str(tmp_path))
    selected_manifest = _manifest(tmp_path, 'selected')
    saved_manifest = _manifest(tmp_path, 'saved')
    saved_worker = tmp_path / 'saved worker.json'
    saved_worker.write_text(json.dumps({'workers': {'default': {
        'kind': 'ssh', 'os': 'windows', 'arch': 'x64', 'host': 'fixture-only', 'worker_id': 'fixture',
    }}}), encoding='utf-8')
    result = {
        'run_id': 'run_saved', 'mode': 'porting', 'status': 'success', 'manifest_path': str(saved_manifest),
        'scenarios': [{'id': 'edge', 'kind': 'console', 'outcome': 'mismatched', 'acceptance': 'failed', 'attempts': []}],
    }
    if worker_saved:
        result['reproduction'] = {
            'argv': ['python', '-m', 'recheck', 'porting-run', str(saved_manifest),
                     '--worker', str(saved_worker), '--approve-build'],
            'approved_build': True,
        }

    class Store:
        def list_runs(self):
            return [result]

        def load_run(self, run_id):
            assert run_id == result['run_id']
            return result

    class Engine:
        def __init__(self, settings, on_event=None):
            self.store = Store()

    window = MainWindow(Settings(), engine_factory=Engine, auto_check=False)
    try:
        window.open_porting()
        porting = window.porting_window
        porting.manifest.setText(str(selected_manifest))
        porting.worker.setText(str(tmp_path / 'other-worker.json'))
        porting.approve.setChecked(True)

        window.history.setCurrentItem(window.history.topLevelItem(0))
        window.open_history()

        assert porting.manifest.text() == str(saved_manifest)
        assert porting.worker.text() == (str(saved_worker) if worker_saved else '')
        assert porting.rerun.isEnabled() == worker_saved
        assert not porting.approve.isChecked()
        assert '저장된 결과 run_saved' in porting.status.text()
        assert '다음 실행' in porting.targets.text()
        assert 'saved.py' in porting.targets.text()
        assert porting.table.topLevelItem(0).text(2) == '불일치'

        porting.manifest.setText(str(selected_manifest))
        porting.worker.setText(str(saved_worker))
        porting.approve.setChecked(True)
        assert 'selected.py' in porting.targets.text()
        assert '저장된 결과 run_saved' in porting.status.text()
        assert porting.rerun.isEnabled()

        porting.show_result(result | {'run_id': 'run_live'})
        assert porting.manifest.text() == str(selected_manifest)
        assert porting.worker.text() == str(saved_worker)
        assert porting.approve.isChecked()
        assert '저장된 결과' not in porting.status.text()
    finally:
        window.close()
        app.processEvents()
        delete(window)
