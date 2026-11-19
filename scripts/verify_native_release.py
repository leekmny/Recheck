"""Verify a frozen Windows release against a known deterministic comparison."""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import Settings, atomic_json
from scripts.release_distribution import inventory, sha


def validate_gui_smoke(smoke_dir):
    smoke = json.loads((smoke_dir / 'gui-smoke.json').read_text(encoding='utf-8'))
    screenshot = smoke_dir / 'gui-smoke.png'
    if (smoke.get('visible') is not True or not screenshot.is_file()
            or screenshot.stat().st_size == 0):
        raise ValueError('Native GUI did not become visible or produce a screenshot')


def expected_execution_image(stage, data):
    from recheck.managed_runtime import ManagedRuntime

    resources = Path(stage) / '_internal/resources'
    runtime = ManagedRuntime(
        Settings(), manifest_path=resources / 'runtime/environment.json',
        resources_dir=resources, receipt_path=Path(data) / 'runtime/local-builds.json',
        bundle_dir=Path(stage) / 'environment')
    return runtime.image_specifications()['images']['execution']['image_id']


def validate_dynamic(run, expected):
    behavior = run.get('behavior', {})
    tests = behavior.get('tests', [])
    if (run.get('stages', {}).get('dynamic', {}).get('status') != 'success'
            or behavior.get('denominator') != len(expected)
            or behavior.get('matched') != sum(expected)
            or [row.get('match') for row in tests] != expected
            or any(row.get('execution_status') != 'measured' for row in tests)):
        raise ValueError('Frozen dynamic comparison did not reproduce every expected verdict')


def validate_observations(run, oracle, image_id):
    observations = run.get('dynamic', [])
    if [row.get('test_id') for row in observations] != [row['test_id'] for row in oracle]:
        raise ValueError('Frozen observations do not match registered test identities')
    for actual, expected in zip(observations, oracle):
        for side in ('original', 'reimplementation'):
            observation, truth = actual[side], expected[side]
            if (observation.get('image') != image_id or observation.get('image_id') != image_id
                    or observation.get('exit_code') != truth['exit_code']):
                raise ValueError('Frozen observation exit code or immutable image differs')
            for channel in ('stdout', 'stderr'):
                if base64.b64decode(observation.get(channel + '_b64', ''), validate=True) != truth[channel].encode('utf-8'):
                    raise ValueError('Frozen raw output differs from precommitted ground truth')


def verify(stage, output, original, changed, tests, expected, oracle, *,
           prepare_from_sources=False, without_host_tools=False):
    stage, output = Path(stage).resolve(strict=True), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = inventory(stage)
    exe = stage / 'Recheck.exe'
    env = {**os.environ, 'RECHECK_DATA_DIR': str(output / 'data'),
           'RECHECK_SMOKE_DIR': str(output / 'smoke'), 'PYTHONUTF8': '1'}
    env.pop('QT_QPA_PLATFORM', None)
    if without_host_tools:
        env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
        env.pop('PYTHONHOME', None)
        env.pop('PYTHONPATH', None)
    atomic_json(output / 'data/settings.json', Settings().model_dump())
    records = []

    def execute(name, arguments, timeout):
        started = time.monotonic()
        argv = [str(exe), *map(str, arguments)]
        with (output / (name + '-process.log')).open('wb') as stream:
            proc = subprocess.run(argv, env=env, cwd=stage, stdout=stream,
                                  stderr=subprocess.STDOUT, timeout=timeout,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        record = dict(argv=argv, returncode=proc.returncode, seconds=time.monotonic() - started)
        atomic_json(output / (name + '-process.json'), record)
        records.append(record)
        return proc.returncode

    if execute('smoke', ['--smoke-test'], 40) != 0:
        raise ValueError('Native GUI smoke process failed')
    validate_gui_smoke(output / 'smoke')
    if execute('tools', ['--cli', '--json-output', output / 'tools.json', 'tools'], 90) not in (0, 2):
        raise ValueError('Frozen tool discovery failed')
    tools = json.loads((output / 'tools.json').read_text(encoding='utf-8'))
    if tools.get('app_runtime', {}).get('python') != 'bundled':
        raise ValueError('Frozen tool discovery did not recognize its bundled Python')
    if prepare_from_sources:
        if execute('prepare-tools', ['--cli', '--data-dir', output / 'data',
                   '--json-output', output / 'prepare-tools.json', 'prepare-tools', '--build'], 1800) != 0:
            raise ValueError('Frozen first-use source preparation failed')
    code = execute('dynamic', ['--cli', '--data-dir', output / 'data', '--json-output',
                   output / 'dynamic-public.json', 'analyze', '--original', Path(original).resolve(),
                   '--reimplementation', Path(changed).resolve(), '--tests', Path(tests).resolve(),
                   '--mode', 'dynamic'], 300)
    public = json.loads((output / 'dynamic-public.json').read_text(encoding='utf-8'))
    run = json.loads((output / 'data/runs' / public['run_id'] / 'result.json').read_text(encoding='utf-8'))
    if code != 0:
        raise ValueError('Frozen dynamic process failed; see native process record')
    validate_dynamic(run, expected)
    validate_observations(run, oracle, expected_execution_image(stage, output / 'data'))
    if before != inventory(stage):
        raise ValueError('Release payload changed during native verification')
    embedded = json.loads((stage / '_internal/RECHECK_SOURCE_IDENTITY.json').read_text(encoding='utf-8'))
    proof = dict(status='verified', inventory=before,
                 source_aggregate=embedded['aggregate_sha256'], executable_sha256=sha(exe),
                 checks={'native_smoke': True, 'frozen_dynamic': True, 'literal_observations': True},
                 processes=records, run_id=run['run_id'], behavior=run['behavior'],
                 scope='Actual native Windows GUI smoke and frozen Linux dynamic comparison; no VM claim')
    proof['checks'].update(frozen_tool_discovery=True,
                          frozen_source_preparation=prepare_from_sources,
                          host_path_disabled=without_host_tools)
    atomic_json(output / 'native-proof.json', proof)
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('stage', 'output', 'original', 'changed', 'tests', 'expected', 'oracle'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--prepare-from-sources', action='store_true')
    parser.add_argument('--without-host-tools', action='store_true')
    args = parser.parse_args()
    expected = json.loads(Path(args.expected).read_text(encoding='utf-8'))
    if not isinstance(expected, list) or not expected or any(type(x) is not bool for x in expected):
        raise ValueError('Expected verdicts must be a precommitted list of booleans')
    oracle = json.loads(Path(args.oracle).read_text(encoding='utf-8'))
    result = verify(args.stage, args.output, args.original, args.changed, args.tests, expected, oracle,
                    prepare_from_sources=args.prepare_from_sources,
                    without_host_tools=args.without_host_tools)
    print(json.dumps({key: value for key, value in result.items() if key != 'inventory'}, indent=2))
