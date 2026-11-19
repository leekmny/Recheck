import platform
import threading

import pytest


async def observe_target(tmp_path, runtime, entry, version, files):
    from recheck.porting_manifest import PROTOCOL, applied_scenario, pack_target
    from recheck.porting_worker import handle, worker_platform

    target = dict(worker_platform(), runtime=runtime, entry=entry, runtime_version=version,
                  files=list(files))
    scenario = {'id': 'version'}
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding='utf-8')
    reply = await handle({
        'protocol': PROTOCOL, 'op': 'run', 'job_id': 'version', 'side': 'reference',
        'target': target, 'scenario': scenario, 'applied': applied_scenario(scenario, 'reference'),
        'bundle': pack_target(tmp_path, target, []),
    }, worker_platform(), threading.Event())
    return reply['observation']


@pytest.mark.parametrize('precise', [True, False])
async def test_python_runtime_version_compares_the_actual_version(tmp_path, precise):
    version = platform.python_version()
    requested = version if precise else version[:3]
    observation = await observe_target(tmp_path, 'python', 'program.py', requested,
                                       {'program.py': 'print("started")'})
    assert observation['status'] == ('completed' if precise else 'environment_error')
    assert observation['process_executions'] == int(precise)
    assert observation['runtime']['version'] == version
    if not precise:
        assert observation['reason'] == 'runtime_version_mismatch'


@pytest.mark.parametrize('requested, expected', [
    ('8.0.34', 'completed'),
    ('8.0.3', 'environment_error'),
    ('8.0.428', 'environment_error'),
])
async def test_dotnet_runtime_pin_uses_framework_inventory_and_launch(monkeypatch, tmp_path,
                                                               requested, expected):
    from recheck import porting_worker as worker

    calls = []
    executable = str(tmp_path / 'dotnet.exe')

    async def fake_process(argv, *, on_start=None, **kwargs):
        calls.append(argv)
        if argv[1:] == ['--version']:
            output = '8.0.428\n'
        elif argv[1:] == ['--list-runtimes']:
            output = 'Microsoft.NETCore.App 8.0.34 [/opt/dotnet/shared/Microsoft.NETCore.App]\n'
        else:
            on_start(4321)
            output = 'started\n'
        return {'returncode': 0, 'stdout': output, 'stderr': '', 'truncated': False,
                'argv': argv, 'duration': 0, 'stdout_b64': '', 'stderr_b64': '',
                'error': None, 'cleanup_errors': []}

    monkeypatch.setattr(worker, '_tool', lambda _: executable)
    monkeypatch.setattr(worker, 'run_process', fake_process)
    observation = await observe_target(tmp_path, 'dotnet', 'Program.dll', requested, {
        'Program.dll': 'placeholder for mocked process',
        'Program.runtimeconfig.json': '{"runtimeOptions":{"framework":'
                                      '{"name":"Microsoft.NETCore.App","version":"8.0.0"}}}',
    })
    assert observation['status'] == expected
    assert calls[0] == [executable, '--list-runtimes']
    assert observation['runtime']['frameworks'] == [{'name': 'Microsoft.NETCore.App', 'version': '8.0.34'}]
    if expected == 'completed':
        assert calls[1][1:4] == ['exec', '--fx-version', '8.0.34']
    else:
        assert len(calls) == 1
        assert observation['reason'] == 'runtime_version_mismatch'
