"""Reproduce literal-oracle accuracy through Recheck's isolated execution adapter."""
from __future__ import annotations
import argparse
import asyncio
import base64
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from recheck.comparison import compare_behavior
from recheck.adapters import DockerAdapter
from recheck.models import Settings, atomic_json, inspect_binary
from recheck.managed_execution import ManagedExecutionAdapter
SOURCES = ROOT / 'samples' / 'accuracy'
VARIANTS = {'base': ['-O0'], 'optimized': ['-O2'], 'alternate': ['-O0', '-DALTERNATE=1'],
            'mutant': ['-O0', '-DMUTANT=1'], 'stripped': ['-O0', '-s']}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def retain_observation(workspace, returned):
    """Keep the adapter's in-memory verdict even when its final snapshot write failed."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    destination = workspace / 'returned-observation.json'
    if destination.exists():
        raise ValueError('Existing returned observation must be retained; choose a new workspace')
    snapshot = dict(path=None, matches_returned=False)
    name = returned.get('container_name')
    if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        path = workspace / (name + '.json')
        snapshot['path'] = str(path)
        try:
            raw = path.read_bytes()
            snapshot.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            saved = json.loads(raw)
            snapshot['matches_returned'] = saved == returned
            snapshot['status'] = saved.get('status') if isinstance(saved, dict) else None
        except (OSError, ValueError) as exc:
            snapshot['error'] = f'{type(exc).__name__}: {exc}'
    else:
        snapshot['error'] = 'No valid container snapshot identity was returned'
    receipt = dict(schema_version='1.0', utc=datetime.now(timezone.utc).isoformat(),
                   returned=returned, adapter_snapshot=snapshot)
    atomic_json(destination, receipt)
    return receipt


def grade_pair(expected_left, expected_right, left, right):
    expected_match = expected_left == expected_right
    result = compare_behavior([dict(test_id='case', original=left, reimplementation=right)], Settings())['tests'][0]
    actual = result['match']
    def conforms(expected, actual):
        return (actual.get('exit_code') == expected['exit_code'] and all(
            base64.b64decode(actual.get(key + '_b64', '')) == expected[key].encode('utf-8')
            for key in ('stdout', 'stderr')))
    measured = result['execution_status'] == 'measured'
    return dict(expected_match=expected_match, actual_match=actual, measured=measured,
                oracle_correct=measured and conforms(expected_left, left) and conforms(expected_right, right),
                false_positive=measured and expected_match and actual is False,
                false_negative=measured and not expected_match and actual is True,
                categories=result['observation_categories'])


def expected(case, variant):
    return dict(stdout=case.get('mutant_stdout', case.get('stdout', '')) if variant == 'mutant'
                else case.get('stdout', ''), stderr=case.get('stderr', ''), exit_code=case.get('exit_code', 0))


def command(argv, receipt):
    started = time.monotonic()
    done = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
                          timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    receipt.append(dict(argv=argv, exit_code=done.returncode, stdout=done.stdout,
                        stderr=done.stderr, seconds=time.monotonic() - started))
    if done.returncode:
        raise RuntimeError('Compilation failed; see build-receipt.json')
    return done.stdout


def build(out, compiler_image):
    receipts, binaries = [], {}
    image = json.loads(command(['docker', 'image', 'inspect', compiler_image], receipts))[0]
    image_id = image['Id']
    command(['docker', 'run', '--rm', '--network=none', '--entrypoint', 'gcc', image_id, '--version'], receipts)
    pe_compilers = {
        'c': shutil.which('x86_64-w64-mingw32-gcc'),
        'cpp': shutil.which('x86_64-w64-mingw32-g++'),
    }
    pe_compilation = {
        language: dict(status='available' if compiler else 'unavailable', compiler=compiler)
        for language, compiler in pe_compilers.items()
    }
    for pe_compiler in dict.fromkeys(pe_compilers.values()):
        if pe_compiler:
            command([pe_compiler, '--version'], receipts)
    elf, pe = out / 'elf', out / 'pe'
    elf.mkdir(exist_ok=True); pe.mkdir(exist_ok=True)
    for source in sorted([*SOURCES.glob('*.c'), *SOURCES.glob('*.cpp')]):
        cpp = source.suffix == '.cpp'
        compiler = 'g++' if cpp else 'gcc'
        pe_compiler = pe_compilers['cpp' if cpp else 'c']
        standard = '-std=c++17' if cpp else '-std=c11'
        variants = ({'base': ['-O0'], 'stripped': ['-O0', '-s']} if cpp else
                    VARIANTS if source.stem != 'recovery' else {'base': ['-O0']})
        for variant, flags in variants.items():
            target = elf / f'{source.stem}-{variant}'
            argv = ['docker', 'run', '--rm', '--network=none', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges', '--memory=512m', '--cpus=2',
                    '--mount', f'type=bind,src={SOURCES},dst=/src,readonly',
                    '--mount', f'type=bind,src={elf},dst=/out', '--entrypoint', compiler, image_id,
                    standard, '-Wall', '-Wextra', '-Werror', *flags,
                    f'/src/{source.name}', '-o', '/out/' + target.name]
            try:
                command(argv, receipts)
                binaries[(source.stem, variant)] = target
                if pe_compiler:
                    pe_target = pe / (target.name + '.exe')
                    command([pe_compiler, standard,
                             '-Wall', '-Wextra', '-Werror', *flags,
                             str(source.relative_to(ROOT)), '-o',
                             str(pe_target.relative_to(ROOT))], receipts)
                    metadata = inspect_binary(pe_target)
                    if (metadata['format'] != 'PE' or metadata['architecture'] != 'x86-64'
                            or metadata.get('kind') != 'console'):
                        raise ValueError('Compiler did not produce a native PE x64 console artifact')
            finally:
                atomic_json(out / 'build-receipt.json', dict(compiler_image_id=image_id,
                    commands=receipts, pe_compilation=pe_compilation,
                    pe_execution='not attempted: isolated Windows backend required'))
    atomic_json(out / 'binary-identities.json', [dict(path=str(p.relative_to(out)),
        sha256=digest(p), bytes=p.stat().st_size) for p in [*elf.iterdir(), *pe.iterdir()]])
    return binaries


async def prepare_execution(out, execution_image=None):
    """Keep production pins by default; explicit legacy images are a separate baseline."""
    if execution_image is None:
        adapter = ManagedExecutionAdapter(Settings())
        preparation = dict(await adapter.prepare_image('linux'),
                           execution_backend='managed_production_adapter')
    else:
        adapter = DockerAdapter()
        preparation = dict(status='blocked', managed=False,
            execution_backend='standalone_docker_baseline', requested_image=execution_image,
            scope='Standalone isolated output comparison; not production Engine or managed-image validation')
        try:
            engine = await adapter.probe()
            preparation['environment'] = engine
            if (not engine.get('ready') or engine.get('local_endpoint') is not True
                    or engine.get('os') != 'linux' or engine.get('arch') not in ('amd64', 'x86_64')):
                raise ValueError('A local Linux amd64 Docker engine is required')
            inspected = await adapter.call('image', 'inspect', execution_image, timeout=15)
            preparation['image_inspection'] = inspected
            if inspected.get('returncode') != 0:
                raise ValueError('The explicitly requested image is not installed')
            details = json.loads(inspected['stdout'])[0]
            image_id = details.get('Id')
            if (not isinstance(image_id, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image_id)
                    or details.get('Os') != 'linux' or details.get('Architecture') != 'amd64'):
                raise ValueError('The standalone image must have an immutable Linux amd64 identity')
            wrapper = await adapter.call('run', '--rm', '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=16',
                '--memory=64m', '--cpus=1', '--user=65534:65534', '--entrypoint=/bin/sh',
                image_id, '-c', 'test -x /usr/local/bin/recheck-run-target', timeout=20)
            preparation['wrapper_check'] = wrapper
            if wrapper.get('returncode') != 0:
                raise ValueError('The standalone image lacks the executable target wrapper')
            preparation.update(status='success', image=image_id, image_id=image_id,
                               image_details=details)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            preparation['error'] = str(exc)
    atomic_json(out / 'execution-preparation.json', preparation)
    return adapter, preparation


async def measure(out, binaries, oracle, concurrency, execution_image=None):
    adapter, preparation = await prepare_execution(out, execution_image)
    if preparation['status'] != 'success':
        raise RuntimeError('Requested execution backend unavailable; see execution-preparation.json')
    (out / 'aux').mkdir(exist_ok=True)
    (out / 'aux/data.txt').write_bytes(b'7\n')
    (out / 'aux/bad.txt').write_bytes(b'not a number\n')
    semaphore, observations, receipts = asyncio.Semaphore(concurrency), {}, {}
    async def execute(sample, number, case, variant):
        test_id = f'{sample}-{number:02d}'
        test = dict(id=test_id, stdin=case.get('stdin', ''), args=case.get('args', []),
            timeout_seconds=5, aux_files=[str(out / 'aux' / case['aux'])] if case.get('aux') else [])
        async with semaphore:
            binary_variant = 'base' if variant == 'identical' else variant
            workspace = out / 'observations' / test_id / variant
            observations[(sample, number, variant)] = await adapter.execute(
                binaries[(sample, binary_variant)], test, 'accuracy-20261116', variant,
                workspace, image=preparation['image_id'])
            receipts[(sample, number, variant)] = retain_observation(
                workspace, observations[(sample, number, variant)])
            atomic_json(out / 'progress.json', dict(completed=len(observations),
                latest=dict(sample=sample, case=number, variant=variant), utc=datetime.now(timezone.utc).isoformat()))
    await asyncio.gather(*(execute(sample, number, case, variant)
        for sample, cases in oracle['samples'].items() for number, case in enumerate(cases)
        for variant in ('base', 'identical', 'optimized', 'alternate', 'mutant', 'stripped')))
    rows = []
    for sample, cases in oracle['samples'].items():
        for number, case in enumerate(cases):
            left = observations[(sample, number, 'base')]
            for variant in ('identical', 'optimized', 'alternate', 'mutant', 'stripped'):
                right = observations[(sample, number, variant)]
                grade = grade_pair(expected(case, 'base'), expected(case, variant), left, right)
                rows.append(dict(sample=sample, case=number, variant=variant, **grade,
                    expected_left=json.dumps(expected(case, 'base'), ensure_ascii=False),
                    expected_right=json.dumps(expected(case, variant), ensure_ascii=False),
                    actual_left=json.dumps({k: left.get(k) for k in ('status', 'infrastructure_ok', 'stdout', 'stderr', 'exit_code', 'error', 'storage_errors')}, ensure_ascii=False),
                    actual_right=json.dumps({k: right.get(k) for k in ('status', 'infrastructure_ok', 'stdout', 'stderr', 'exit_code', 'error', 'storage_errors')}, ensure_ascii=False),
                    image_id=preparation['image_id'],
                    execution_backend=preparation['execution_backend'],
                    seconds=left.get('duration', 0) + right.get('duration', 0), ai_grade='not_requested', ai_requests=0,
                    observation_left=str(out / 'observations' / f'{sample}-{number:02d}' / 'base'),
                    observation_right=str(out / 'observations' / f'{sample}-{number:02d}' / variant)))
    with (out / 'accuracy-results.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    return await finish_measurement(out, adapter, binaries, preparation, observations, rows, receipts)


async def finish_measurement(out, adapter, binaries, preparation, observations, rows, receipts=None):
    recoveries = []
    for mode in ('timeout', 'flood', 'child'):
        result = await adapter.execute(binaries[('recovery', 'base')],
            dict(id=mode, args=[mode], timeout_seconds=1, limits=dict(output_bytes=4096)),
            'accuracy-recovery-20261116', mode, out / 'recovery' / mode, image=preparation['image_id'])
        retain_observation(out / 'recovery' / mode, result)
        name = result.get('container_name')
        remaining = await adapter.call('inspect', name, timeout=15) if name else {}
        recoveries.append(dict(mode=mode, timed_out=result.get('timed_out'),
            truncated=result.get('truncated'), cancelled=result.get('cancelled'),
            infrastructure_ok=result.get('infrastructure_ok'), cleanup_errors=result.get('cleanup_errors'),
            storage_errors=result.get('storage_errors'),
            container_removed=bool(name) and remaining.get('returncode') != 0,
            observation=str(out / 'recovery' / mode),
            expected='unmeasured; owned container and children removed'))
    atomic_json(out / 'recovery-results.json', recoveries)
    summary = dict(registered_pairs=len(rows), measured_pairs=sum(r['measured'] for r in rows),
        unmeasured_pairs=sum(not r['measured'] for r in rows),
        expected_differences=sum(not r['expected_match'] for r in rows),
        actual_differences=sum(r['actual_match'] is False for r in rows),
        false_positives=sum(r['false_positive'] for r in rows),
        false_negatives=sum(r['false_negative'] for r in rows),
        oracle_failures=sum(not r['oracle_correct'] for r in rows),
        actual_target_executions=len(observations), baseline_observations_reused=True,
        recovery_target_executions=len(recoveries),
        total_target_execution_attempts=len(observations) + len(recoveries),
        execution_backend=preparation.get('execution_backend', 'managed_production_adapter'),
        product_managed_execution=preparation.get('managed', True),
        observations_with_storage_errors=sum(bool(row.get('storage_errors')) for row in observations.values()),
        adapter_snapshot_mismatches=sum(not receipt['adapter_snapshot']['matches_returned']
                                        for receipt in (receipts or {}).values()),
        image_id=preparation['image_id'], ai_requests=0, ai_explanation_accuracy='unmeasured',
        pe_execution='unmeasured', static_quality='not assessed by this runner', recoveries=recoveries)
    atomic_json(out / 'accuracy-summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler-image', default='recheck-generated-compiler:1')
    parser.add_argument('--execution-image', help=(
        'Explicit installed Linux image for standalone Docker output baseline only; '
        'omitting this retains the production ManagedExecutionAdapter and its immutable pins'))
    parser.add_argument('--concurrency', type=int, choices=range(1, 5), default=3)
    parser.add_argument('--reuse-build', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    if (out / 'observations').exists() or (out / 'accuracy-summary.json').exists():
        raise ValueError('Existing measurement evidence must be retained; choose a new output folder')
    out.mkdir(parents=True, exist_ok=args.reuse_build)
    oracle_path, frozen = SOURCES / 'oracle.json', out / 'frozen-oracle.json'
    if frozen.exists() and frozen.read_bytes() != oracle_path.read_bytes():
        raise ValueError('Oracle changed since it was frozen')
    frozen.write_bytes(oracle_path.read_bytes())
    atomic_json(out / 'oracle-identity.json', dict(sha256=digest(frozen),
        frozen_before_execution=datetime.now(timezone.utc).isoformat(),
        sources=[dict(name=p.name, sha256=digest(p)) for p in [*SOURCES.glob('*.c'), *SOURCES.glob('*.cpp')]]))
    binaries = ({(s.stem, v): out / 'elf' / f'{s.stem}-{v}' for s in SOURCES.glob('*.c')
        for v in (VARIANTS if s.stem != 'recovery' else ['base'])} if args.reuse_build else build(out, args.compiler_image))
    summary = asyncio.run(measure(out, binaries, json.loads(frozen.read_text(encoding='utf-8')),
                                  args.concurrency, args.execution_image))
    return 0 if not (summary['oracle_failures'] or summary['unmeasured_pairs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
