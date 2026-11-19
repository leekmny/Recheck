"""Run the release's fixed native examples through the packaged comparison engine."""
from __future__ import annotations

import asyncio
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


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


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
        path = write(fixture_inputs, name, manifest('windows', 'x64', variant=name))
        result = await run_porting(store, path, settings=Settings(), allow_build=True,
                                   fixture_workers={'default': TrustedFixtures()})
        checked = result_check(result, name)
        observations = [obs for row in result['scenarios'] for attempt in row['attempts']
                        for obs in attempt['observations'].values()]
        require(checked['behavior']['denominator'] == len(CASES)
                and len(observations) == len(CASES) * 2 and all(
                    obs['status'] == 'completed' and obs['started'] is True
                    and obs['process_executions'] == 2 and obs['build']['exit_code'] == 0
                    for obs in observations), f'{name}: all PE builds and process starts are required')
        mismatches = [row['id'] for row in result['scenarios'] if row['outcome'] == 'mismatched']
        require(mismatches == expected_mismatches, f'{name}: unexpected mismatch scenario IDs')
        for row in result['scenarios']:
            expected = row['attempts'][0]['comparison']['expected']
            candidate_status = 'failed' if row['id'] in expected_mismatches else 'passed'
            require(expected['reference']['status'] == 'passed' and expected['candidate']['status'] == candidate_status
                    and row['target_inputs']['reference']['file_hashes'] == {'fee.c': fixture_hashes['fee.c']}
                    and row['target_inputs']['candidate']['file_hashes'] == {'fee.rs': fixture_hashes['fee.rs']},
                    f"{name}/{row['id']}: incorrect expectation or fixture identity")
        variants[name] = {'status': 'success', 'manifest_sha256': checked['manifest_sha256'],
                          'denominator': checked['behavior']['denominator'],
                          'mismatched': checked['behavior']['mismatched'], 'mismatch_ids': mismatches,
                          'acceptance': checked['behavior']['acceptance'], 'target_starts': len(observations)}
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
    except (OSError, ValueError, RuntimeError) as error:
        proof['error'] = str(error)
    (output / 'fixture-proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if proof['status'] == 'success' else 2
