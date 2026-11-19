"""Run the release's fixed native examples through the packaged comparison engine."""
from __future__ import annotations

import asyncio
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

from samples.porting.console.fixtures import CASES, manifest, write

from .models import Settings, resources_root
from .porting import run_porting
from .porting_transport import TrustedFixtures
from .porting_worker import execution_architecture, native_file, worker_platform
from .storage import Store


HOSTED = 'github-hosted-windows-fixtures'
FIXTURE_MISMATCHES = {'normal': [], 'defect': ['above', 'higher', 'maximum'], 'alternate': []}
FIXTURE_FILES = ('fee.c', 'fee.rs')
FIXTURE_DIR = 'samples/porting/console'
PLATFORM = 'windows/x64'
FIXTURE_IDS = tuple(case[0] for case in CASES)
FIXTURE_STDOUT = {b'', b'400', b'1000', b'7600', *(case[2].encode('ascii') for case in CASES if case[2] is not None)}
FIXTURE_STDERR = {b'', b'invalid'}
OBSERVATION_STATUSES = {'completed', 'blocked', 'cancelled', 'timed_out', 'output_truncated', 'observation_error',
                        'environment_error', 'unsupported', 'oom'}


class FixtureVerificationError(RuntimeError):
    """A validation failure whose message is safe for release diagnostics."""


def require(condition, message):
    if not condition:
        raise FixtureVerificationError(message)


def result_check(result, variant):
    rows = result['scenarios']
    stats = result['behavior']
    require(stats['denominator'] == len(rows), f'{variant}: unmeasured rows in {result["run_id"]}')
    expected = (1 if len(rows) <= 4 else 3) if variant == 'defect' else 0
    require(stats['mismatched'] == expected, f'{variant}: unexpected number of differences')
    require(stats['acceptance'] == ('failed' if expected else 'passed'), f'{variant}: incorrect acceptance')
    for row in rows:
        for attempt in row['attempts']:
            left, right = attempt['observations'].values()
            require(left['started'] and right['started'] and left['entry_sha256'] != right['entry_sha256'],
                    'Targets must both start and have distinct entry bytes.')
    return {'status': 'success', 'run_id': result['run_id'], 'behavior': stats,
            'manifest_sha256': result['manifest_sha256']}


def hosted_manifest(variant):
    return manifest('windows', 'x64', variant=variant, windows_rust_target='x86_64-pc-windows-msvc')


def _known_output(encoded, expected):
    if not isinstance(encoded, str) or len(encoded) > 24:
        return '<redacted>'
    try:
        value = base64.b64decode(encoded, validate=True)
    except ValueError:
        return '<redacted>'
    return value.decode('ascii') if value.removesuffix(b'\n').removesuffix(b'\r') in expected else '<redacted>'


def _observation_detail(observation, expectation):
    status = observation.get('status')
    exit_code = observation.get('exit_code')
    build_exit = observation.get('build', {}).get('exit_code')
    expected = expectation.get('status')
    return {'status': status if status in OBSERVATION_STATUSES else 'unavailable',
            'exit_code': exit_code if type(exit_code) is int else None,
            'build_exit_code': build_exit if type(build_exit) is int else None,
            'expected': expected if expected in ('passed', 'failed', 'inconclusive') else 'unavailable',
            'stdout': _known_output(observation.get('stdout_b64'), FIXTURE_STDOUT),
            'stderr': _known_output(observation.get('stderr_b64'), FIXTURE_STDERR)}


def _fixture_row_valid(row, should_mismatch, fixture_hashes):
    if row['outcome'] != ('mismatched' if should_mismatch else 'matched') or len(row['attempts']) != 1:
        return False
    attempt = row['attempts'][0]
    expected = attempt['comparison']['expected']
    for side, source, expectation in (('reference', 'fee.c', 'passed'),
                                      ('candidate', 'fee.rs', 'failed' if should_mismatch else 'passed')):
        observation = attempt['observations'][side]
        if (expected[side]['status'] != expectation
                or row['target_inputs'][side]['file_hashes'] != {source: fixture_hashes[source]}
                or observation['status'] != 'completed' or observation['started'] is not True
                or observation['process_executions'] != 2 or observation['build']['exit_code'] != 0):
            return False
    return True


def validate_fixture_result(result, variant, fixture_hashes, expected_mismatches):
    rows = result['scenarios']
    failures = []
    for row in rows:
        scenario_id = row['id']
        if scenario_id not in FIXTURE_IDS or _fixture_row_valid(row, scenario_id in expected_mismatches, fixture_hashes):
            continue
        attempt = (row['attempts'] or [{}])[0]
        outcome = row['outcome']
        detail = {'id': scenario_id, 'outcome': outcome if outcome in
                  ('matched', 'mismatched', 'unmeasured', 'unexecuted') else 'unavailable'}
        for side in ('reference', 'candidate'):
            detail[side] = _observation_detail(attempt.get('observations', {}).get(side, {}),
                                               attempt.get('comparison', {}).get('expected', {}).get(side, {}))
        failures.append(detail)
    require(not failures, f'{variant}: fixed fixture checks failed; details={json.dumps(failures)}')
    require(tuple(row['id'] for row in rows) == FIXTURE_IDS,
            f'{variant}: result lacks the complete fixed fixture scenario set in order')
    checked = result_check(result, variant)
    return {'status': 'success', 'manifest_sha256': checked['manifest_sha256'],
            'denominator': checked['behavior']['denominator'], 'mismatched': checked['behavior']['mismatched'],
            'mismatch_ids': expected_mismatches, 'acceptance': checked['behavior']['acceptance'],
            'target_starts': len(rows) * 2}


async def compare_fixtures(output):
    resources = resources_root()
    fixture_inputs = output / 'fixtures'
    fixture_inputs.mkdir()
    identity = json.loads((resources.parent / 'RECHECK_SOURCE_IDENTITY.json').read_text(encoding='utf-8'))
    fixture_hashes = {}
    for name in FIXTURE_FILES:
        content = (resources / 'porting' / 'native-fixtures' / name).read_bytes()
        fixture_hashes[name] = hashlib.sha256(content).hexdigest()
        require(fixture_hashes[name] == identity['runtime_identity'].get(f'{FIXTURE_DIR}/{name}'),
                f'Frozen fixture SHA-256 mismatch: {name}')
        (fixture_inputs / name).write_bytes(content)

    store = Store(output / 'history')
    variants = {}
    for name, expected_mismatches in FIXTURE_MISMATCHES.items():
        path = write(fixture_inputs, name, hosted_manifest(name))
        result = await run_porting(store, path, settings=Settings(), allow_build=True,
                                   fixture_workers={'default': TrustedFixtures()})
        variants[name] = validate_fixture_result(result, name, fixture_hashes, expected_mismatches)
    return {'source_sha': identity.get('source_git_commit'), 'fixture_sha256': fixture_hashes,
            'variants': variants}


def verify_frozen_fixtures(output):
    if not getattr(sys, 'frozen', False) or worker_platform() != {'os': 'windows', 'arch': 'x64'}:
        return 2
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    proof = {'status': 'failed'}
    try:
        architecture = execution_architecture()
        require(architecture == {'native_arch': 'x64', 'emulated': False}
                and native_file(Path(sys.executable)) == ('windows', 'x64', False),
                'A native Windows x64 process and executable are required')
        # The fixture subprocess does not use Qt or start any bundled helper. Use the native toolchain's DLLs.
        require(ctypes.WinDLL('kernel32', use_last_error=True).SetDllDirectoryW(None),
                'Could not restore the native Windows DLL search path')
        resources = resources_root().parent
        os.environ['PATH'] = os.pathsep.join(part for part in os.environ.get('PATH', '').split(os.pathsep)
                                             if part and not Path(part).resolve().is_relative_to(resources))
        with Path(sys.executable).open('rb') as stream:
            executable_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        proof = {'status': 'success', 'platform': PLATFORM, 'mode': HOSTED, 'command': 'frozen-fixtures',
                 'executable_sha256': executable_hash, **architecture, **asyncio.run(compare_fixtures(output))}
    except FixtureVerificationError as error:
        proof['error'] = str(error)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        proof['error'] = f'Frozen fixture verification could not complete ({type(error).__name__})'
    (output / 'fixture-proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if proof['status'] == 'success' else 2
