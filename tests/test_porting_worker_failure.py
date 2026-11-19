import json
import re

import pytest

from recheck.porting import run_porting
from recheck.porting_transport import TrustedFixtures, _decode
from recheck.porting_worker import worker_platform
from recheck.storage import Store


@pytest.mark.parametrize(('phase', 'started', 'executions'), [('probe', False, 0), ('run', None, None)])
async def test_transport_failure_is_traceable_without_repeating_the_failed_job(tmp_path, phase, started, executions):
    class DisconnectOnce(TrustedFixtures):
        def __init__(self):
            super().__init__()
            self.failed_request = None
            self.dispatched = []

        async def _exchange(self, request, cancel):
            self.dispatched.append(request['job_id'])
            if self.failed_request is None and request['op'] == phase:
                self.failed_request = request
                return _decode({'returncode': -9, 'stderr': ''})
            return await super()._exchange(request, cancel)

    targets = {}
    for side, source in [('reference', 'print(4)'), ('candidate', 'print(5 - 1)')]:
        entry = side + '.py'
        (tmp_path / entry).write_text(source, encoding='utf-8')
        targets[side] = dict(worker_platform(), runtime='python', entry=entry)
    manifest = tmp_path / 'scenario.json'
    manifest.write_text(json.dumps({'schema_version': 'recheck-porting-1', 'targets': targets,
                                   'structure': {'enabled': False},
                                   'scenarios': [{'id': 'disconnect'}, {'id': 'recovered'}]}), encoding='utf-8')
    worker = DisconnectOnce()
    store = Store(tmp_path / 'history')

    result = await run_porting(store, manifest, fixture_workers={'default': worker})

    saved = store.load_run(result['run_id'])
    first, second = saved['scenarios']
    failed = first['attempts'][0]['observations']['reference']
    assert failed['status'] == 'environment_error'
    assert failed['reason'] == 'worker_transport_failed'
    assert failed['started'] is started and failed['process_executions'] == executions
    assert failed['exit_code'] is None
    assert failed['job_id'] == worker.failed_request['job_id']
    transport = failed['worker_transport']
    assert transport['operation'] == phase and transport['returncode'] == -9
    assert re.fullmatch('[a-f0-9]{64}', transport['request_sha256'])
    assert '-9' in failed['error']
    assert worker.dispatched.count(failed['job_id']) == 1
    assert first['outcome'] == 'unmeasured' and second['outcome'] == 'matched'
