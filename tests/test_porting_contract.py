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
