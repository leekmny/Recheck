import base64
import json
import subprocess
import sys
import time

import pytest


def fixture_manifest(tmp_path):
    for name in ('ref.py', 'new.py'):
        (tmp_path / name).write_text('print(1)', encoding='utf-8')
    manifest = {
        'schema_version': 'recheck-porting-1',
        'targets': {side: {'runtime': 'python', 'entry': name, 'os': 'windows', 'arch': 'x64'}
                    for side, name in [('reference', 'ref.py'), ('candidate', 'new.py')]},
        'scenarios': [{'id': 'basic', 'input': {'args': ['한글 path']}, 'kind': 'console'}],
    }
    path = tmp_path / 'scenario.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return path, manifest


def test_manifest_packages_only_declared_bytes(tmp_path):
    from recheck.porting_manifest import load_manifest, pack_target

    path, _ = fixture_manifest(tmp_path)
    (tmp_path / 'unrelated.txt').write_text('private', encoding='utf-8')
    manifest, root = load_manifest(path)
    bundle = pack_target(root, manifest['targets']['reference'], [])
    assert list(bundle['files']) == ['ref.py']
    assert base64.b64decode(bundle['files']['ref.py']) == b'print(1)'
    assert len(bundle['sha256']) == 64


@pytest.mark.parametrize('bad', ['../out.py', 'C:/out.py', 'con.py', 'data/a\\b', 'a//b'])
def test_manifest_rejects_nonportable_paths(tmp_path, bad):
    from recheck.porting_manifest import load_manifest

    path, manifest = fixture_manifest(tmp_path)
    manifest['targets']['reference']['entry'] = bad
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        load_manifest(path)


def process(data, code=0, status='completed', reason='exit_zero'):
    return {'status': status, 'reason': reason, 'started': True,
            'exit_code': code, 'stdout_b64': base64.b64encode(data).decode(), 'stderr_b64': '',
            'observations': {}}


@pytest.mark.parametrize(('left', 'right', 'expect', 'match', 'acceptance'), [
    (process(b'good'), process(b'good'), {'stdout': 'good', 'exit_code': 0}, True, 'passed'),
    (process(b'bad', 2), process(b'bad', 2), {'stdout': 'bad', 'exit_code': 2}, True, 'passed'),
    (process(b'', -11), process(b'', -11), {'exit_code': 0}, True, 'failed'),
    (process(b'good'), process(b'', None, 'timed_out'), {'exit_code': 0}, None, 'inconclusive'),
    (process(b'wrong'), process(b'good'), {'exit_code': 0, 'stdout': 'good'}, False, 'failed'),
    (process(b'good'), process(b'', None, 'environment_error'), {'exit_code': 0}, None,
     'inconclusive'),
])
def test_behavior_and_acceptance_are_independent(left, right, expect, match, acceptance):
    from recheck.porting_policy import compare_attempt

    row = compare_attempt(left, right, {'expected': {'reference': expect, 'candidate': expect}})
    assert row['match'] is match
    assert row['acceptance'] == acceptance


def test_json_policy_retains_raw_and_rejects_duplicate_keys():
    from recheck.porting_policy import compare_attempt

    scenario = {'policy': {'stdout': {'mode': 'json', 'tolerances': {'/total': {'abs': 0.1}}}}}
    row = compare_attempt(process(b'{"total":1.04}'), process(b'{"total":1.0}'), scenario)
    assert row['match'] is True and row['raw_match'] is False
    row = compare_attempt(process(b'{"total":1,"total":1}'), process(b'{"total":1}'), scenario)
    assert row['match'] is None


def test_http_policy_compares_json_and_retains_raw_body():
    from recheck.porting_policy import compare_attempt

    left = process(b'')
    right = process(b'')
    for row, value in ((left, b'{"total": 1.03}'), (right, b'{"total":1.0}')):
        row['observations']['quote'] = {'status': 200, 'body_b64': base64.b64encode(value).decode()}
    scenario = {'kind': 'http', 'steps': [{'id': 'quote', 'action': 'http'}],
                'policy': {'step:quote': {'mode': 'http-json', 'tolerances': {'/body/total': {'abs': .04}}}},
                'expected': {side: {'step:quote#/status': 200} for side in ('reference', 'candidate')}}
    row = compare_attempt(left, right, scenario)
    assert row['match'] is True and row['normalization_masked'] is True
    assert row['acceptance'] == 'passed'
    right['observations']['quote']['status'] = 500
    row = compare_attempt(left, right, scenario)
    assert row['match'] is False and row['acceptance'] == 'failed'


def test_invalid_required_comparison_keeps_local_expectations_and_blocks_acceptance():
    from recheck.porting_policy import compare_attempt

    data = process(b'not json')
    scenario = {'policy': {'stdout': 'json'},
                'expected': {side: {'exit_code': 0} for side in ('reference', 'candidate')}}
    row = compare_attempt(data, data, scenario)
    assert row['match'] is None and row['acceptance'] == 'inconclusive'
    assert all(value['status'] == 'passed' for value in row['expected'].values())


async def test_reproduction_keeps_worker_path_resolvable_from_history(tmp_path, monkeypatch):
    from recheck.porting import run_porting
    from recheck.storage import Store

    manifest, _ = fixture_manifest(tmp_path)
    worker = tmp_path / 'worker.json'
    worker.write_text('{"workers": {}}', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / 'history')

    result = await run_porting(store, manifest.name, worker.name)

    argv = store.load_run(result['run_id'])['reproduction']['argv']
    assert argv[argv.index('--worker') + 1] == str(worker.resolve())


async def test_rerun_requires_original_fixture_bytes(tmp_path):
    from recheck.porting import run_porting
    from recheck.porting_transport import TrustedFixtures
    from recheck.porting_worker import worker_platform
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    for target in manifest['targets'].values():
        target.update(worker_platform())
        (tmp_path / target['entry']).write_text(
            'from pathlib import Path\nprint(Path("initial.txt").read_text())\n', encoding='utf-8')
    manifest['scenarios'][0]['fixtures'] = ['initial.txt']
    manifest['structure'] = {'enabled': False}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    fixture = tmp_path / 'initial.txt'
    fixture.write_bytes(b'initial')
    store = Store(tmp_path / 'history')
    original = await run_porting(store, path, fixture_workers={'default': TrustedFixtures()})
    assert original['behavior']['denominator'] == 1
    fixture.write_bytes(b'changed')
    repeat = await run_porting(store, path, previous_run=original['run_id'], scenario_ids=['basic'],
                               fixture_workers={'default': TrustedFixtures()})
    assert repeat['scenarios'][0]['fixture_recheck']['status'] == 'blocked'
    assert repeat['behavior']['denominator'] == repeat['behavior']['process_executions'] == 0
    assert store.load_run(original['run_id']) == original


def test_worker_rejects_different_code_after_probe():
    from recheck.porting_manifest import PROTOCOL
    from recheck.porting_transport import Worker, WorkerError

    channel = Worker({'os': 'windows', 'arch': 'x64'}, 'guest', 'dedicated-vm')
    request = dict(op='probe', job_id='probe', protocol=PROTOCOL)
    env = {'id': 'guest', 'isolation': 'dedicated-vm', 'os': 'windows', 'arch': 'x64',
           'worker_code_sha256': 'a' * 64, 'emulated': False}
    channel.proof = channel._check(request, dict(request, environment=env, status='ready'))
    with pytest.raises(WorkerError, match='worker 코드'):
        channel._check(request, dict(request, environment=dict(env, worker_code_sha256='b' * 64),
                                     status='ready'))


async def test_unconfigured_worker_preserves_a_blocked_result(tmp_path):
    from recheck.porting import run_porting
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    manifest['structure'] = {'enabled': False}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    store = Store(tmp_path / 'history')
    result = await run_porting(store, path)
    assert result['status'] == 'blocked'
    assert result['behavior']['process_executions'] == 0
    assert result['behavior']['match_rate'] is None
    assert result['behavior']['acceptance'] == 'inconclusive'
    assert store.load_run(result['run_id']) == result


def test_worker_stream_disconnect_ends_live_child(tmp_path):
    from recheck.porting_manifest import PROTOCOL, applied_scenario, pack_target
    from recheck.porting_transport import _frame
    from recheck.porting_worker import worker_platform
    import os

    (tmp_path / 'test_program.py').write_text('import time\nprint("ready", flush=True)\ntime.sleep(30)\n',
                                             encoding='utf-8')
    target = dict(worker_platform(), entry='test_program.py', runtime='python')
    scenario = {'id': 'cancelled', 'timeout': 20}
    request = dict(protocol=PROTOCOL, op='run', job_id='disconnect-proof',
                   side='reference', target=target, bundle=pack_target(tmp_path, target, []),
                   scenario=scenario, applied=applied_scenario(scenario, 'reference'))
    env = dict(os.environ, RECHECK_WORKER_ID='test-worker', RECHECK_WORKER_ISOLATION='dedicated-vm')
    proc = subprocess.Popen(
        [sys.executable, '-m', 'recheck.porting_worker'], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        proc.stdin.write(_frame(request))
        proc.stdin.flush()
        time.sleep(1.3)
        proc.stdin.close()
        proc.stdin = None
        stdout, stderr = proc.communicate(timeout=7)
        assert proc.returncode == 0, stderr.decode()
        row = json.loads(stdout)['observation']
        assert row['started'] is True and row['status'] == 'cancelled'
        assert base64.b64decode(row['stdout_b64']).strip() == b'ready'
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_porting_keeps_one_input_snapshot_across_scenarios_and_structure(tmp_path):
    from recheck.porting import run_porting
    from recheck.porting_manifest import digest
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    sources = [tmp_path / side['entry'] for side in manifest['targets'].values()]
    source = b'def original():\n    return 1\n'
    for filename in sources:
        filename.write_bytes(source)
    fixture = tmp_path / 'input.txt'
    fixture.write_bytes(b'original data')
    manifest['concurrency'] = 1
    manifest['scenarios'] = [dict(id=f'case-{i}', fixtures=['input.txt']) for i in range(2)]
    path.write_text(json.dumps(manifest), encoding='utf-8')

    class EditingWorker:
        spec = {}

        async def run(self, request, cancel):
            entry = request['target']['entry']
            sent = base64.b64decode(request['bundle']['files'][entry])
            sent_fixture = base64.b64decode(request['bundle']['files']['input.txt'])
            for filename in sources:
                filename.write_bytes(b'def changed():\n    return 2\n')
            fixture.write_bytes(b'changed data')
            return process(sent + sent_fixture)

    store = Store(tmp_path / 'history')
    result = await run_porting(store, path, fixture_workers={'default': EditingWorker()})

    for row in result['scenarios']:
        for side, target in manifest['targets'].items():
            assert row['target_inputs'][side]['file_hashes'][target['entry']] == digest(source)
            assert row['target_inputs'][side]['file_hashes']['input.txt'] == digest(b'original data')
            assert base64.b64decode(row['attempts'][0]['observations'][side]['stdout_b64']) == (
                source + b'original data'
            )
    for target in result['structure']['targets'].values():
        assert target['files'][0]['sha256'] == digest(source)
        assert target['functions'][0]['name'] == 'original'
    assert store.load_run(result['run_id']) == result


async def test_final_commit_failure_never_exposes_successful_porting_result(tmp_path):
    import sqlite3

    from recheck.porting import run_porting
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    manifest['structure'] = {'enabled': False}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    store = Store(tmp_path / 'history')
    with store.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_final_status BEFORE UPDATE OF status ON runs
            WHEN NEW.status = 'success'
            BEGIN SELECT RAISE(ABORT, 'forced final commit failure'); END;
        """)

    class FixtureWorker:
        spec = {}

        async def run(self, request, cancel):
            return process(b'complete observation')

    with pytest.raises(sqlite3.IntegrityError, match='forced final commit failure'):
        await run_porting(store, path, fixture_workers={'default': FixtureWorker()})
    reopened = Store(store.root)
    history = reopened.list_runs()[0]
    assert history['status'] == 'partial'
    assert reopened.load_run(history['run_id']) == history
    assert history['scenarios'][0]['attempts'][0]['observations']['reference'] == (
        process(b'complete observation')
    )


async def test_checkpoint_failure_preserves_one_side_and_joins_other_scenarios(tmp_path, monkeypatch):
    import asyncio
    import sqlite3

    import psutil

    from recheck.porting import run_porting
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    manifest.update(concurrency=2, scenarios=[dict(id='basic'), dict(id='slow')],
                    structure={'enabled': False})
    path.write_text(json.dumps(manifest), encoding='utf-8')
    store = Store(tmp_path / 'history')
    with store.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_candidate BEFORE INSERT ON porting_updates
            WHEN instr(NEW.payload, 'reject_checkpoint') > 0
            BEGIN SELECT RAISE(ABORT, 'forced checkpoint failure'); END;
        """)
        db.execute("""
            CREATE TRIGGER reject_partial BEFORE UPDATE OF status ON runs
            WHEN NEW.status = 'partial'
            BEGIN SELECT RAISE(ABORT, 'storage temporarily unavailable'); END;
        """)

    reference_ready = asyncio.Event()
    slow_ready = asyncio.Event()
    finished = []

    class FixtureWorker:
        spec = {}

        async def run(self, request, cancel):
            side = request['side']
            if request['scenario']['id'] == 'slow':
                try:
                    slow_ready.set()
                    await asyncio.Future()
                finally:
                    finished.append(side)
            if side == 'reference':
                reference_ready.set()
                return process(b'saved reference')
            await reference_ready.wait()
            await slow_ready.wait()
            return process(b'unsaved candidate', reason='reject_checkpoint')

    with pytest.raises(sqlite3.IntegrityError, match='forced checkpoint failure'):
        await run_porting(store, path, fixture_workers={'default': FixtureWorker()})
    assert sorted(finished) == ['candidate', 'reference']
    reopened = Store(store.root)
    run_id = reopened.list_run_summaries()[0]['run_id']
    assert reopened.interrupted() == []  # The owner process has not exited yet.
    with store.connect() as db:
        db.execute('DROP TRIGGER reject_partial')

    def exited(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr('recheck.storage.psutil.Process', exited)
    assert reopened.interrupted() == [run_id]
    result = reopened.load_run(run_id)
    assert reopened.list_runs() == [result]
    assert reopened.list_run_summaries()[0]['status'] == result['status'] == 'partial'
    assert result['stages']['dynamic']['status'] == 'failed'
    assert result['scenarios'][0]['pending_attempt'] == {
        'index': 1, 'observations': {'reference': process(b'saved reference')},
    }
    assert result['behavior']['denominator'] == 0
    assert reopened.interrupted() == []


async def test_new_porting_result_needs_no_legacy_json_and_diagnostics_omit_observations(tmp_path, monkeypatch):
    import zipfile

    from recheck.porting import run_porting
    from recheck.storage import Store, new_id

    path, manifest = fixture_manifest(tmp_path)
    manifest.update(structure={'enabled': False})
    manifest['scenarios'][0]['input']['stdin'] = 'private_input'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    store = Store(tmp_path / 'history')
    legacy = dict(mode='porting', run_id=new_id('run'), attempt_id=new_id('attempt'),
                  schema_version='recheck-porting-result-1', status='partial', scenarios=[])
    store.save(legacy)

    def unavailable_projection(*args):
        raise OSError('result.json cannot be written')

    monkeypatch.setattr('recheck.storage.atomic_json', unavailable_projection)

    class FixtureWorker:
        spec = {}

        async def run(self, request, cancel):
            return dict(process(b'private_output'), observations={'sample': {'body_b64': 'private_data'}})

    result = await run_porting(store, path, fixture_workers={'default': FixtureWorker()})
    reopened = Store(store.root)
    assert reopened.load_run(legacy['run_id']) == legacy
    assert reopened.load_run(result['run_id']) == result
    assert not (reopened.run_dir(result['run_id']) / 'result.json').exists()

    diagnostic = reopened.diagnostic_zip(result['run_id'], {})
    with zipfile.ZipFile(diagnostic) as archive:
        content = archive.read('diagnostics.json')
        assert all(secret not in content for secret in (b'private_input', b'private_data'))
        assert json.loads(content)['run']['behavior']['matched'] == 1


async def test_porting_uses_one_copy_for_case_aliases(tmp_path, monkeypatch):
    from recheck import porting_inputs
    from recheck.porting import run_porting
    from recheck.porting_manifest import digest
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    source = tmp_path / 'ref.py'
    if not (tmp_path / 'REF.PY').is_file():
        pytest.skip('The filesystem distinguishes case in the test directory')
    initial = b'def original():\n    return 1\n'
    source.write_bytes(initial)
    manifest['targets']['reference']['static'] = {'path': 'REF.PY'}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    capture = porting_inputs._capture

    def edit_after_capture(original, *args):
        identity = capture(original, *args)
        if original.name.casefold() == source.name:
            source.write_bytes(b'def changed():\n    return 2\n')
        return identity

    monkeypatch.setattr(porting_inputs, '_capture', edit_after_capture)

    class FixtureWorker:
        spec = {}

        async def run(self, request, cancel):
            return process(b'normal fixture')

    store = Store(tmp_path / 'history')
    result = await run_porting(store, path, fixture_workers={'default': FixtureWorker()})
    hashes = result['input_snapshot']['file_hashes']
    assert hashes['ref.py'] == hashes['REF.PY'] == digest(initial)
    assert result['structure']['targets']['reference']['files'][0]['sha256'] == digest(initial)


async def test_oversized_dynamic_bundles_are_rejected_before_snapshot_copy(tmp_path, monkeypatch):
    from recheck import porting_inputs
    from recheck.porting import run_porting
    from recheck.porting_manifest import MAX_BUNDLE
    from recheck.storage import Store

    path, manifest = fixture_manifest(tmp_path)
    fixtures = ['first.dat', 'second.dat']
    for name in fixtures:
        with (tmp_path / name).open('wb') as stream:
            stream.truncate(MAX_BUNDLE // 2)
    manifest['scenarios'][0]['fixtures'] = fixtures
    manifest['structure'] = {'enabled': False}
    path.write_text(json.dumps(manifest), encoding='utf-8')

    def unexpected_capture(*args):
        raise AssertionError('Files referenced only by an oversized bundle were copied')

    monkeypatch.setattr(porting_inputs, '_capture', unexpected_capture)

    class FixtureWorker:
        spec = {}

        async def run(self, request, cancel):
            raise AssertionError('An oversized bundle was submitted')

    store = Store(tmp_path / 'history')
    result = await run_porting(store, path, fixture_workers={'default': FixtureWorker()})
    assert result['input_snapshot']['file_hashes'] == {}
    assert result['behavior']['denominator'] == 0
    observations = result['scenarios'][0]['attempts'][0]['observations']
    assert {item['reason'] for item in observations.values()} == {'input_bundle_failed'}
    assert all('16 MiB' in item['error'] for item in observations.values())
