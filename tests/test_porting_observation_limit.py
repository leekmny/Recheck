import base64

import pytest

from recheck.porting_manifest import MAX_OBSERVATION, PROTOCOL, applied_scenario, canonical, pack_target
from recheck.porting_worker import handle, worker_platform


DATA = b'A' * (480 * 1024)


async def _run(tmp_path, *, steps=(), args=()):
    files = {'app.py': b'print("observed")', 'data.bin': DATA,
             'extra.bin': b'B' * (64 * 1024), 'big.bin': b'B' * (513 * 1024),
             'small.txt': b'retained'}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    platform = worker_platform()
    target = dict(platform, runtime='python', entry='app.py', files=list(files))
    scenario = {'id': 'bounded', 'steps': list(steps), 'input': {'args': list(args)}}
    reply = await handle({
        'protocol': PROTOCOL, 'op': 'run', 'job_id': 'bounded', 'side': 'reference',
        'target': target, 'scenario': scenario, 'applied': applied_scenario(scenario, 'reference'),
        'bundle': pack_target(tmp_path, target, []),
    }, platform)
    return reply['observation']


def _file_steps(count):
    return [{'id': f'step-{index}', 'action': 'files', 'paths': ['data.bin']} for index in range(count)]


@pytest.mark.parametrize('count', [6, 8])
async def test_aggregate_limit_preserves_complete_steps_in_execution_order(tmp_path, count):
    observation = await _run(tmp_path, steps=_file_steps(count))

    assert len(canonical(observation)) <= MAX_OBSERVATION
    assert list(observation['observations']) == [f'step-{index}' for index in range(6)]
    for step in observation['observations'].values():
        assert base64.b64decode(step['data.bin']['content_b64']) == DATA
    assert base64.b64decode(observation['stdout_b64']).strip() == b'observed'
    assert observation['process_executions'] == 1
    assert observation['entry_sha256']
    if count == 6:
        assert observation['status'] == 'completed'
        assert 'observation_limit' not in observation
    else:
        assert observation['status'] == 'output_truncated'
        assert observation['reason'] == 'observation_limit'
        limit = observation['observation_limit']
        assert limit['omitted_step_ids'] == ['step-6', 'step-7']
        assert limit['omitted_step_count'] == 2
        assert limit['prior_status'] == 'completed'
        assert limit['prior_reason'] == 'exit_zero'


@pytest.mark.parametrize('first_path', ['small.txt', 'data.bin'])
async def test_aggregate_limit_accounts_for_failed_step_evidence(tmp_path, first_path):
    steps = _file_steps(7)
    failing_path = 'big.bin' if first_path == 'small.txt' else 'extra.bin'
    steps.append({'id': 'failed', 'action': 'files', 'paths': [first_path, failing_path]})

    observation = await _run(tmp_path, steps=steps)

    assert len(canonical(observation)) <= MAX_OBSERVATION
    assert observation['status'] == 'output_truncated'
    assert observation['step_id'] == 'failed'
    assert observation['observation_limit']['prior_status'] == 'observation_error'
    assert observation['observation_limit']['omitted_step_ids'] == ['step-6']
    if first_path == 'small.txt':
        assert base64.b64decode(observation['driver_evidence']['small.txt']['content_b64']) == b'retained'
        assert not observation['observation_limit']['omitted_fields']
    else:
        assert 'driver_evidence' not in observation
        assert observation['observation_limit']['omitted_fields'] == ['driver_evidence']


async def test_observation_limit_bounds_oversized_invocation_metadata(tmp_path):
    observation = await _run(tmp_path, args=['\x01' * 8192] * 100)

    assert len(canonical(observation)) <= MAX_OBSERVATION
    assert observation['status'] == 'output_truncated'
    assert observation['observation_limit']['omitted_fields'] == ['argv']
    assert observation['observations'] == {}
    assert 'argv' not in observation
