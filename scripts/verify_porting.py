"""Verify the porting product with stored readback and authored normal/defect fixtures."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from recheck.engine import Engine
from recheck.models import Settings
from recheck.porting import run_porting
from recheck.porting_manifest import digest
from recheck.porting_transport import TrustedFixtures, load_workers
from recheck.porting_worker import worker_code_sha256, worker_platform
from samples.porting.console.fixtures import ROOT, manifest, write


def checked(condition, message):
    if not condition:
        raise RuntimeError(message)


def result_check(result, variant):
    rows = result['scenarios']
    stats = result['behavior']
    checked(stats['denominator'] == len(rows), f'{variant}: unmeasured rows in {result["run_id"]}')
    expected = 3 if variant == 'defect' else 0
    if len(rows) <= 4:
        expected = 1 if variant == 'defect' else 0
    checked(stats['mismatched'] == expected, f'{variant}: unexpected number of differences')
    checked(stats['acceptance'] == ('failed' if expected else 'passed'),
            f'{variant}: incorrect acceptance')
    for row in rows:
        for attempt in row['attempts']:
            left, right = attempt['observations'].values()
            checked(left['started'] and right['started'] and left['entry_sha256'] != right['entry_sha256'],
                    'Targets must both start and have distinct entry bytes.')
    return {'status': 'success', 'run_id': result['run_id'], 'behavior': stats,
            'manifest_sha256': result['manifest_sha256']}


class Verifier:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.output).resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.inputs = self.out / 'fixtures'
        self.inputs.mkdir()
        for name in ('fee.c', 'fee.rs', 'fee.py', 'fee_node.mjs', 'fee.ts',
                     'Fee.java', 'Fee.cs', 'Fee.csproj'):
            shutil.copyfile(ROOT / name, self.inputs / name)
        self.store_dir = self.out / 'history'
        self.engine = Engine(Settings(), data_dir=self.store_dir)
        self.executable = Path(args.frozen).resolve(strict=True) if args.frozen else None
        self.checks = {}

    def cli(self, *argv, exit_code=0, frozen=True):
        executable = self.executable if frozen else None
        command = [str(executable), '--cli'] if executable else [sys.executable, '-m', 'recheck']
        if executable:
            output = self.out / ('frozen-cli-' + str(len(list(self.out.glob('frozen-cli-*')))) + '.json')
        else:
            output = self.out / ('source-cli-' + str(len(list(self.out.glob('source-cli-*')))) + '.json')
        child = subprocess.run(
            [*command, '--data-dir', str(self.store_dir), '--json-output', str(output), *map(str, argv)],
            timeout=420, capture_output=True, check=False,
            env=dict(os.environ, PYTHONUTF8='1', RECHECK_DATA_DIR=str(self.store_dir)),
        )
        checked(child.returncode == exit_code and output.is_file(),
                f'{argv[0]} exit={child.returncode}, expected={exit_code}, output={output.is_file()}')
        return json.loads(output.read_text(encoding='utf-8'))

    async def fixture(self, variant):
        info = worker_platform()
        data = manifest(info['os'], info['arch'], 'python', 'node', variant)
        data['scenarios'] = data['scenarios'][:4]
        path = write(self.inputs, 'trusted-' + variant, data)
        result = await run_porting(self.engine.store, path, settings=self.engine.settings,
                                   fixture_workers={'default': TrustedFixtures()})
        self.checks[variant] = result_check(result, variant)
        return path, result

    def report(self, result):
        rid = result['run_id']
        direct = self.cli('porting-result', rid)
        checked(direct == result == self.engine.store.load_run(rid), 'Saved JSON differs from CLI result.')
        report = Path(self.cli('report', rid))
        body = report.read_text(encoding='utf-8')
        checked('테스트 범위 내 동작 일치율' in body and rid in body and '새 구현 기대 결과' in body,
                'HTML does not reflect the stored result.')
        self.checks['json'] = {'status': 'success', 'sha256': digest(json.dumps(
            direct, sort_keys=True, ensure_ascii=False).encode())}
        self.checks['html'] = {'status': 'success', 'sha256': digest(report.read_bytes())}
        self.checks['cli'] = {'status': 'success', 'readback': 'frozen' if self.executable else 'source'}
        if self.executable:
            self.checks['frozen'] = {'status': 'success'}

    def gui(self, path, result):
        screenshot = self.out / 'gui.png'
        if self.executable:
            child = subprocess.run(
                [str(self.executable), '--verify-porting-ui', str(self.store_dir),
                 result['run_id'], str(screenshot)], timeout=60, check=False,
            )
            checked(child.returncode == 0 and screenshot.is_file(), 'Packaged porting GUI failed.')
            self.checks['gui'] = {'status': 'success', 'source': 'frozen',
                                  'sha256': digest(screenshot.read_bytes())}
            return
        from PySide6.QtWidgets import QApplication
        from recheck.porting_ui import PortingWindow
        from recheck.ui_theme import STYLE

        app = QApplication.instance() or QApplication([])
        app.setStyleSheet(STYLE)
        window = PortingWindow(self.engine)
        window.manifest.setText(str(path))
        if self.args.worker:
            window.worker.setText(str(self.args.worker))
        checked('fee.py' in window.targets.text() and 'fee_node.mjs' in window.targets.text(),
                'GUI did not display both target configurations.')
        window.show_result(result)
        window.show()
        app.processEvents()
        checked('75.0%' in window.metrics.text() and window.table.topLevelItemCount() == 4,
                'GUI does not show the defect run metrics.')
        window.table.setCurrentItem(window.table.topLevelItem(3))
        app.processEvents()
        checked('stdout_b64' in window.evidence.toPlainText(), 'GUI lost raw observation evidence.')
        checked(window.grab().save(str(screenshot)), 'GUI screenshot failed.')
        window.close()
        app.processEvents()
        self.checks['gui'] = {'status': 'success', 'sha256': digest(screenshot.read_bytes())}



    async def native(self):
        workers = load_workers(self.args.worker) if self.args.worker else {}
        worker = workers.get('default')
        if not worker and not self.args.trusted_native:
            self.checks['native'] = {'status': 'blocked', 'reason': 'isolated_worker_required'}
            checked(not self.args.require_native, 'A configured isolated worker is required.')
            return
        spec = worker.spec if worker else worker_platform()
        platform_name = f"{spec['os']}/{spec['arch']}"
        checked(not self.args.native_platform or self.args.native_platform == platform_name,
                f'Expected worker {self.args.native_platform}; configured {platform_name}.')
        checked(not self.args.require_native or worker is not None, 'Trusted host fixtures are not isolated.')
        variants = {}
        for variant in ('normal', 'defect', 'alternate'):
            path = write(self.inputs, 'native-' + variant, manifest(spec['os'], spec['arch'], variant=variant))
            if worker:
                result = self.cli('porting-run', path, '--worker', self.args.worker, '--approve-build')
            else:
                result = await run_porting(
                    self.engine.store, path, allow_build=True, settings=self.engine.settings,
                    fixture_workers={'default': TrustedFixtures()},
                )
            variants[variant] = result_check(result, variant)
            observations = [item for row in result['scenarios'] for attempt in row['attempts']
                            for item in attempt['observations'].values()]
            expected_code = worker_code_sha256() if worker else None
            checked(all((obs['environment']['os'], obs['environment']['arch']) == (spec['os'], spec['arch'])
                        and obs['environment']['isolation'] == (worker.isolation if worker else 'trusted-fixtures')
                        and (not worker or (obs['environment'].get('emulated') is False
                             and obs['environment'].get('worker_code_sha256') == expected_code))
                        for obs in observations),
                    'Actual execution environment is not the required native platform.')
            variants[variant]['environment'] = observations[0]['environment']
            variants[variant]['tools'] = [obs['build_tool'] for obs in observations[:2]]
            if variant == 'defect' and worker:
                self.report(result)
        self.checks['native'] = {'status': 'success' if worker else 'trusted_fixture_success',
                                 'platform': platform_name, 'variants': variants}

    async def run(self):
        _, normal = await self.fixture('normal')
        path, defect = await self.fixture('defect')
        self.report(defect)
        if self.args.gui:
            self.gui(path, defect)
        result = await run_porting(
            self.engine.store, self.inputs / 'trusted-normal.json', previous_run=defect['run_id'],
            settings=self.engine.settings, fixture_workers={'default': TrustedFixtures()},
        )
        checked(result['behavior']['denominator'] == 1 and result['behavior']['matched'] == 1
                and result['previous_run_id'] == defect['run_id']
                and self.engine.store.load_run(defect['run_id']) == defect,
                'Rerun did not preserve the defective run and its original scenario.')
        self.checks['rerun'] = {'status': 'success', 'run_id': result['run_id'],
                                'previous_run_id': defect['run_id'],
                                'normal_run_id': normal['run_id']}
        await self.native()
        if self.args.runtime_matrix:
            self.runtimes()

    def runtimes(self):
        worker = load_workers(self.args.worker).get('default')
        checked(worker is not None, 'Runtime matrix needs an isolated worker.')
        spec = worker.spec
        outcomes = {}
        for left, right in (('python', 'rust'), ('python', 'node'), ('python', 'typescript'),
                            ('java', 'dotnet')):
            for variant in ('normal', 'defect'):
                key = f'{left}-{right}-{variant}'
                data = manifest(spec['os'], spec['arch'], left, right, variant)
                data['scenarios'] = data['scenarios'][:4]
                path = write(self.inputs, key, data)
                result = self.cli('porting-run', path, '--worker', self.args.worker, '--approve-build')
                outcomes[key] = result_check(result, variant)
                self.checks['runtimes'] = {'status': 'partial', 'pairs': outcomes}
        self.checks['runtimes']['status'] = 'success'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--require-native', action='store_true')
    parser.add_argument('--native-platform')
    parser.add_argument('--trusted-native', action='store_true')
    parser.add_argument('--runtime-matrix', action='store_true')
    parser.add_argument('--gui', action='store_true')
    parser.add_argument('--frozen')
    args = parser.parse_args(argv)
    if args.worker:
        args.worker = args.worker.resolve(strict=True)
    verifier = Verifier(args)
    source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip())
    proof = {'schema_version': 'recheck-porting-verification-1', 'source_sha': source,
             'working_tree_dirty': dirty, 'checks': verifier.checks, 'status': 'failed'}
    if verifier.executable:
        proof['executable_sha256'] = digest(verifier.executable.read_bytes())
    try:
        asyncio.run(verifier.run())
        proof['status'] = 'success'
    except Exception as exc:
        proof['error'] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        path = verifier.out / 'verification.json'
        path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
        print(path)
    return 0 if proof['status'] == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
