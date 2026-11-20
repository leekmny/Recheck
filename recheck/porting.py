"""Common product result and orchestration for porting comparisons."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import psutil

from .porting_history import initial_rows
from .porting_manifest import (
    BUILT_ENTRY_RUNTIMES, SIDES, applied_scenario, canonical, digest, load_manifest,
)
from .porting_policy import compare_attempt, summarize
from .porting_transport import WorkerError, load_workers
from .storage import new_id, now

RESULT = 'recheck-porting-result-1'


def capability_matrix():
    return {
        'schema_version': 'recheck-porting-capabilities-1',
        'platforms': [
            {'os': system, 'arch': arch, 'native_format': file_format,
             'workers': ['ssh', 'docker'] if system == 'linux' else ['ssh'],
             'implementation': 'conditional_worker', 'verification': 'see PORTING_VALIDATION_UPDATE.md'}
            for system, file_format in [('windows', 'PE'), ('linux', 'ELF'), ('macos', 'Mach-O')]
            for arch in ('x64', 'arm64')
        ],
        'runtimes': ['native', 'python', 'node', 'typescript', 'java', 'dotnet'],
        'drivers': ['console', 'http', 'browser', 'desktop', 'library'],
        'observations': ['stdout', 'stderr', 'exit', 'http', 'dom', 'accessibility', 'files',
                         'sqlite', 'ffi', 'visual'],
        'static_representations': ['native', 'cil', 'python_ast', 'typescript_ast', 'jvm_bytecode'],
        'limitations': ['Matching worker configuration is required.',
                        'Linux containers do not provide an interactive desktop by default.',
                        'TypeScript requires an explicitly approved build and compiler.',
                        'Structure comparisons require compatible representations.'],
    }


def _missing(status, reason, **more):
    return dict(status=status, reason=reason, started=False, process_executions=0,
                exit_code=None, stdout_b64='', stderr_b64='', observations={}, **more)


async def _collect(*work):
    tasks = [asyncio.create_task(item) for item in work]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _select_rerun(store, previous_run, scenario_ids):
    parent = store.load_run(previous_run)
    if parent.get('schema_version') != RESULT:
        raise ValueError('포팅 검증 이력에서만 시나리오를 재검사할 수 있습니다.')
    scenarios = parent['manifest']['scenarios']
    selected = set(scenario_ids or [
        row['id'] for row in parent['scenarios']
        if row['outcome'] != 'matched' or row['acceptance'] in ('failed', 'inconclusive')
    ])
    if not selected or selected - {case['id'] for case in scenarios}:
        raise ValueError('보관된 문제 시나리오 ID를 지정하세요.')
    return [case for case in scenarios if case['id'] in selected]


def _uses_built_entry(target):
    if 'build' not in target or target['runtime'] not in BUILT_ENTRY_RUNTIMES:
        return False
    static = target.get('static', {})
    files = static.get('files', [static.get('path', target['entry'])])
    return isinstance(files, list) and target['entry'] in files


async def run_porting(store, manifest_path, worker_path=None, *, settings=None, cancel=None,
                      on_event=None, allow_build=False, previous_run=None, scenario_ids=None,
                      fixture_workers=None):
    manifest_path = Path(manifest_path).resolve(strict=True)
    if worker_path:
        worker_path = Path(worker_path).resolve()
    manifest, root = load_manifest(manifest_path)
    workers = fixture_workers if fixture_workers is not None else load_workers(worker_path)
    cancel = cancel if cancel is not None else threading.Event()
    previous_rows = {}
    if previous_run:
        manifest['scenarios'] = _select_rerun(store, previous_run, scenario_ids)
        previous_rows = {row['id']: row for row in store.load_run(previous_run)['scenarios']}
    run_id = new_id('run')
    rows = initial_rows(manifest)
    argv = ['python', '-m', 'recheck']
    argv += ['porting-rerun', previous_run] if previous_run else ['porting-run']
    argv += [str(manifest_path)]
    if worker_path:
        argv += ['--worker', str(worker_path)]
    if allow_build:
        argv += ['--approve-build']
    if previous_run:
        for case in manifest['scenarios']:
            argv += ['--scenario', case['id']]
    run = dict(schema_version=RESULT, mode='porting', run_id=run_id, attempt_id=new_id('attempt'),
               status='running', created=now(), manifest=manifest, manifest_sha256=digest(canonical(manifest)),
               manifest_path=str(manifest_path), previous_run_id=previous_run,
               owner_process=dict(pid=os.getpid(), create_time=psutil.Process().create_time()),
               stages={'dynamic': {'status': 'running'}, 'structure': {'status': 'pending'},
                       'ai': {'status': 'not_requested'}}, scenarios=rows,
               workers={key: dict(value.spec) for key, value in workers.items()},
               reproduction=dict(argv=argv, approved_build=allow_build,
                                 uncontrolled=manifest.get('uncontrolled', [])),
               behavior=summarize(rows), structure={'status': 'pending', 'similarity': None})
    store.save_porting_state(run)
    try:
        workspace = store.run_dir(run_id)
        with TemporaryDirectory(prefix='recheck-porting-inputs-', ignore_cleanup_errors=True) as directory:
            return await _compare_porting(
                store, run, root, Path(directory), workspace, workers, settings, cancel,
                on_event, allow_build, previous_rows,
            )
    except (Exception, asyncio.CancelledError):
        # If storage is still unavailable, leave the last committed running
        # state for the next process's startup recovery.
        with suppress(sqlite3.Error):
            store.interrupt_porting(run_id, '비교가 중단되었습니다. 마지막으로 저장된 관찰을 보존했습니다.')
        raise


async def _compare_porting(store, run, root, snapshot_root, workspace, workers, settings, cancel,
                           on_event, allow_build, previous_rows):
    from .porting_inputs import InputSnapshot

    manifest, rows = run['manifest'], run['scenarios']
    capture = asyncio.create_task(asyncio.to_thread(InputSnapshot, snapshot_root, root, manifest, cancel))
    try:
        snapshot = await asyncio.shield(capture)
    except asyncio.CancelledError:
        cancel.set()
        with suppress(Exception):
            await capture
        raise
    run['input_snapshot'] = dict(file_hashes=snapshot.hashes, errors=snapshot.errors)
    store.save_porting_state(run)
    built_entries = {side for side in SIDES if _uses_built_entry(manifest['targets'][side])}
    capture_enabled = manifest.get('structure', {}).get('enabled', True) is True
    captured_entries = {}

    async def execute(side, case, bundle, applied):
        target = manifest['targets'][side]
        worker = workers.get(target.get('worker', 'default'))
        if 'build' in target and not allow_build:
            return _missing('blocked', 'build_not_approved')
        if not worker:
            return _missing('blocked', 'isolated_worker_required')
        if isinstance(bundle, Exception):
            return _missing('environment_error', 'input_bundle_failed', error=str(bundle))
        try:
            request = dict(target=target, scenario=case, bundle=bundle, side=side,
                           applied=applied, allow_build=allow_build)
            if capture_enabled and side in built_entries and side not in captured_entries:
                request['capture_entry'] = True
            result = await worker.run(request, cancel)
            artifact = result.pop('_entry_artifact', None)
            if artifact is not None:
                captured_entries.setdefault(side, artifact)
            return result
        except (OSError, ValueError, RuntimeError) as exc:
            row = _missing('cancelled' if cancel.is_set() else 'environment_error',
                           'worker_transport_failed', error=str(exc))
            details = exc.transport if isinstance(exc, WorkerError) else {}
            if details:
                row.update(job_id=details['job_id'], worker_transport=details)
            if details.get('operation') != 'probe':
                row.update(started=None, process_executions=None)
            return row

    queue = asyncio.Queue()
    for case, row in zip(manifest['scenarios'], rows):
        queue.put_nowait((case, row))

    async def consume():
        while not queue.empty() and not cancel.is_set():
            case, row = queue.get_nowait()
            applied = {side: applied_scenario(case, side) for side in SIDES}
            bundles = {}
            for side in SIDES:
                try:
                    bundles[side] = snapshot.pack(manifest['targets'][side], case.get('fixtures', []))
                except (OSError, ValueError) as exc:
                    bundles[side] = exc
            target_inputs = {
                side: ({key: value for key, value in bundle.items() if key != 'files'}
                       if isinstance(bundle, dict) else {'error': str(bundle)})
                for side, bundle in bundles.items()
            }
            distinct = (bundles[SIDES[0]]['sha256'] != bundles[SIDES[1]]['sha256']
                        if all(isinstance(bundle, dict) for bundle in bundles.values()) else None)
            fields = dict(target_inputs=target_inputs, applied=applied, distinct_input_bundles=distinct)
            planned = case.get('repetitions', 1)
            if run['previous_run_id'] and case.get('fixtures'):
                baseline = previous_rows[case['id']].get('target_inputs', {})
                checks = {}
                for side in SIDES:
                    original = baseline.get(side, {}).get('file_hashes', {})
                    current = target_inputs[side].get('file_hashes', {})
                    checks[side] = {path: {'previous': original.get(path), 'current': current.get(path)}
                                    for path in case['fixtures']}
                equal = all(item['previous'] is not None and item['previous'] == item['current']
                            for result in checks.values() for item in result.values())
                fields['fixture_recheck'] = {'status': 'verified' if equal else 'blocked', 'hashes': checks}
                if not equal:
                    values = [_missing('blocked', 'rerun_fixture_hash_unverified') for _ in SIDES]
                    store.checkpoint_porting(
                        run, row, fields=fields, index=1, observations=dict(zip(SIDES, values)),
                        comparison=compare_attempt(*values, case), planned=planned,
                    )
                    continue
            store.checkpoint_porting(run, row, fields=fields)
            for index in range(planned):
                if cancel.is_set():
                    break

                async def run_side(side):
                    result = await execute(side, case, bundles[side], applied[side])
                    store.checkpoint_porting(run, row, index=index + 1, observations={side: result})
                    return result

                values = await _collect(*(run_side(side) for side in SIDES))
                store.checkpoint_porting(
                    run, row, index=index + 1, comparison=compare_attempt(*values, case), planned=planned,
                )
                run['behavior'] = summarize(rows)
                if on_event:
                    on_event(dict(stage='dynamic', status=row['outcome'], scenario=case['id'],
                                  attempted=run['behavior']['attempted'], total=len(rows),
                                  message=f"{case['id']} · {index + 1}/{planned}"))

    await _collect(*(consume() for _ in range(manifest.get('concurrency', 1))))
    behavior = run['behavior'] = summarize(rows)
    if cancel.is_set():
        dynamic = 'cancelled'
    elif behavior['denominator'] == len(rows):
        dynamic = 'success'
    elif behavior['denominator']:
        dynamic = 'partial'
    elif set(behavior['execution_statuses']) <= {'blocked', 'unsupported'}:
        dynamic = 'blocked'
    else:
        dynamic = 'failed'
    run['stages']['dynamic']['status'] = dynamic
    run['stages']['structure']['status'] = 'running'
    store.save_porting_state(run)
    if cancel.is_set():
        run['structure'] = dict(status='cancelled', similarity=None, reason='user_cancelled')
    else:
        try:
            from .porting_static import compare_structure

            execution_entries = {
                side: [attempt['observations'][side].get('entry_sha256')
                       if attempt['observations'][side].get('started') is True else None
                       for row in rows for attempt in row['attempts']]
                for side in built_entries
            }
            run['structure'] = await compare_structure(manifest, snapshot_root, workspace=workspace / 'structure',
                                                        settings=settings, cancel=cancel,
                                                        execution_entries=execution_entries,
                                                        captured_entries=captured_entries,
                                                        resolve_input=snapshot.resolve)
        except Exception as exc:
            run['structure'] = dict(status='failed', similarity=None, reason=f'{type(exc).__name__}: {exc}')
    structure = run['structure']['status']
    run['stages']['structure']['status'] = 'success' if structure == 'succeeded' else structure
    run['status'] = ('partial' if dynamic == 'success' and structure not in ('succeeded', 'skipped')
                     else dynamic)
    if cancel.is_set():
        run['status'] = 'cancelled'
    store.save_porting_state(run)
    return run
