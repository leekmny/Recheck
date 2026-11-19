"""Prepare and publish a verified Recheck Windows x64 Actions release."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import zipfile

from scripts.github_release import GitHub, REPOSITORY, SafeError, checked_sha, safe_run


ROOT = Path(__file__).resolve().parents[1]
REQUEST = '.github/porting-release.json'
PLATFORM = 'windows-x64'
ZIP_NAME = 'Recheck-Windows-x64.zip'
EXE_NAME = 'Recheck.exe'
EVIDENCE_NAME = 'recheck-verification.json'
CHECKSUMS_NAME = 'SHA256SUMS.txt'
ASSETS = (ZIP_NAME, EXE_NAME, EVIDENCE_NAME, CHECKSUMS_NAME)
IDENTITY = 'RECHECK_SOURCE_IDENTITY.json'
SOURCE_CHECKS = ('normal', 'defect', 'cli', 'json', 'html', 'gui', 'rerun')
ISOLATED_CHECKS = ('normal', 'defect', 'cli', 'json', 'html', 'rerun', 'native', 'frozen')
RELEASE_CHANGES = {
    'console-porting': (
        'Windows x64 격리 작업자에서 정상·결함 콘솔 포팅 예제를 비교했습니다.',
        ('source.normal', 'source.defect', 'isolated_worker.native'),
    ),
    'result-reporting': (
        '소스와 Windows x64 배포본에서 CLI 결과 조회와 JSON·HTML 보고서를 확인했습니다.',
        ('source.cli', 'source.json', 'source.html', 'frozen.cli', 'frozen.json', 'frozen.html'),
    ),
    'scenario-rerun': (
        '소스에서 저장한 결함 시나리오를 수정본에 다시 적용하고 이전 결과를 보존했습니다.',
        ('source.rerun',),
    ),
}
REQUIRED_WORKFLOWS = {
    'tests.yml': {'tests'},
    'security.yml': {'CodeQL (python)', 'CodeQL (javascript-typescript)', 'dependencies'},
    'porting-ci.yml': {'porting-product', 'porting-linux-x64'},
}
REQUIRED_PAYLOAD = {
    EXE_NAME, f'_internal/{IDENTITY}', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'licenses/manifest.json', 'licenses/REPLACEMENT.md', 'docs/FIRST_RUN.md',
    'docs/PORTING_RELEASE.md',
}


@dataclass(frozen=True)
class Candidate:
    version: str
    parts: tuple[int, int, int]
    changes: tuple[str, ...]

    @property
    def tag(self):
        return 'v' + self.version

    @property
    def prerelease(self):
        return self.parts[0] == 0


def parse_candidate(record):
    if not isinstance(record, dict) or set(record) != {'version', 'changes'}:
        raise SafeError('Release request requires only version and changes')
    version = record['version']
    part = r'(0|[1-9][0-9]{0,4})'
    match = re.fullmatch(fr'{part}\.{part}\.{part}', version) if isinstance(version, str) else None
    if not match or any(int(value) > 65535 for value in match.groups()):
        raise SafeError('Invalid canonical release version; use MAJOR.MINOR.PATCH')
    changes = record['changes']
    if (not isinstance(changes, list) or not changes
            or any(not isinstance(change, str) or change not in RELEASE_CHANGES for change in changes)
            or len(changes) != len(set(changes))):
        raise SafeError('Release changes must be a nonempty list of distinct verified change IDs')
    return Candidate(version, tuple(map(int, match.groups())), tuple(changes))


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL).strip()


def requested_candidate(root, before, commit):
    checked_sha(commit)
    current_path = f'{commit}:{REQUEST}'
    if not git(root, 'ls-tree', '-r', commit, '--', REQUEST):
        return None
    current = parse_candidate(json.loads(git(root, 'show', current_path)))
    checked_sha(before)
    if git(root, 'ls-tree', '-r', before, '--', REQUEST):
        previous = parse_candidate(json.loads(git(root, 'show', f'{before}:{REQUEST}')))
        if current.parts < previous.parts:
            raise SafeError('Release version must increase')
    if git(root, 'tag', '--list', current.tag):
        return None
    return current


def require_verification(proof, commit, required, *, executable_sha256=None):
    if (not isinstance(proof, dict) or proof.get('schema_version') != 'recheck-porting-verification-1'
            or proof.get('status') != 'success' or proof.get('source_sha') != commit):
        raise SafeError('Porting evidence is missing, unsuccessful, or from a different commit')
    checks = proof.get('checks')
    if not isinstance(checks, dict):
        raise SafeError('Porting evidence is missing required checks')
    for check in required:
        if not isinstance(checks.get(check), dict) or checks[check].get('status') != 'success':
            raise SafeError(f'Required porting check did not succeed: {check}')
    if executable_sha256 is not None and proof.get('executable_sha256') != executable_sha256:
        raise SafeError('Frozen verification evidence does not match the executable SHA-256')


def require_protected_main(api, commit):
    checked_sha(commit)
    branch = api.request('GET', api.endpoint('/branches/main'))
    if (branch.get('name') != 'main' or branch.get('protected') is not True
            or branch.get('commit', {}).get('sha') != commit):
        raise SafeError('The checked commit must be the exact head of protected main')


def require_ci(api, commit, *, wait_seconds=0):
    query = urllib.parse.urlencode({
        'branch': 'main', 'event': 'push', 'head_sha': checked_sha(commit), 'per_page': 100,
    })
    deadline = time.monotonic() + wait_seconds
    while True:
        verified = {}
        for workflow, required_jobs in REQUIRED_WORKFLOWS.items():
            page = api.request('GET', api.endpoint(f'/actions/workflows/{workflow}/runs?{query}'))
            if type(page.get('total_count')) is not int or page['total_count'] > 100:
                raise SafeError('Required CI run inventory did not verify')
            matching = [
                run for run in page['workflow_runs']
                if run.get('head_sha') == commit and run.get('head_branch') == 'main'
                and run.get('event') == 'push'
                and run.get('path', '').partition('@')[0] == f'.github/workflows/{workflow}'
            ]
            if not matching:
                continue
            run = max(matching, key=lambda item: item['id'])
            if run.get('status') != 'completed':
                continue
            if run.get('conclusion') != 'success':
                raise SafeError(f'Required CI did not succeed: {workflow}')
            jobs = api.request('GET', api.endpoint(f'/actions/runs/{run["id"]}/jobs?per_page=100'))
            successful = {
                row.get('name') for row in jobs['jobs']
                if row.get('status') == 'completed' and row.get('conclusion') == 'success'
            }
            if jobs.get('total_count') != len(jobs['jobs']) or not required_jobs <= successful:
                raise SafeError(f'Required CI jobs did not succeed: {workflow}')
            verified[workflow] = safe_run(run)
        if set(verified) == set(REQUIRED_WORKFLOWS):
            return verified
        if time.monotonic() >= deadline:
            raise SafeError('Required CI is missing or incomplete for the exact main commit')
        time.sleep(min(15, max(0, deadline - time.monotonic())))


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checkout_runtime(commit, *, allow_generated_identity=False):
    from scripts.verify_source import snapshot

    checked_sha(commit)
    paths = ('.', f':(exclude){IDENTITY}') if allow_generated_identity else ('.',)
    tracked_changes = git(ROOT, 'diff', 'HEAD', '--name-only', '--', *paths)
    if git(ROOT, 'rev-parse', 'HEAD').decode() != commit or tracked_changes:
        raise SafeError('Build inputs differ from the checked-out commit')
    if git(ROOT, 'ls-files', '--others', '--exclude-standard'):
        raise SafeError('Untracked files are present in the release checkout')
    return snapshot(ROOT)


def bind_source_identity(commit):
    from scripts.verify_source import verification_inputs

    runtime = checkout_runtime(commit, allow_generated_identity=True)
    identity = read_json(ROOT / IDENTITY)
    if (identity.get('runtime_identity') != runtime
            or identity.get('verification', {}).get('inputs') != verification_inputs(ROOT)
            or not all(identity['verification'].get(name) == 'exit 0' for name in ('pytest', 'node'))):
        raise SafeError('The verified source identity does not describe the current build and tests')
    identity['source_git_commit'] = commit
    write_json(ROOT / IDENTITY, identity)


def require_build_identity(identity, commit, candidate, runtime_inventory):
    runtime = identity.get('runtime_identity')
    if not isinstance(runtime, dict) or not runtime:
        raise SafeError('Embedded source inventory is missing')
    if runtime != runtime_inventory:
        raise SafeError('Embedded source inventory differs from the exact checked-out commit')
    digest = hashlib.sha256(json.dumps(runtime, sort_keys=True, separators=(',', ':')).encode())
    if (identity.get('source_git_commit') != commit
            or identity.get('aggregate_sha256') != digest.hexdigest()
            or identity.get('display_version') != candidate.version
            or identity.get('python_metadata_version') != candidate.version
            or identity.get('windows_numeric_version') != [*candidate.parts, 0]):
        raise SafeError('Embedded source identity or release version does not match')
    return digest.hexdigest()


def bundle_assets(candidate, commit, stage, staged_inventory, source, frozen, output):
    from scripts.collect_distribution_notices import verify_notices
    from scripts.release_distribution import inventory, write_archive

    expected = inventory(stage)
    staged_inventory = [row | {'path': row['path'].replace('\\', '/')} for row in staged_inventory]
    if expected != staged_inventory:
        raise SafeError('Windows payload changed after native execution began')
    if not REQUIRED_PAYLOAD <= {row['path'] for row in expected} or (stage / 'environment').exists():
        raise SafeError('Public Windows x64 ZIP is incomplete or contains an offline environment')
    aggregate = require_build_identity(read_json(stage / '_internal' / IDENTITY), commit, candidate,
                                       checkout_runtime(commit, allow_generated_identity=True))
    executable_sha256 = sha(stage / EXE_NAME)
    require_verification(source, commit, SOURCE_CHECKS)
    require_verification(frozen, commit, (*SOURCE_CHECKS, 'frozen', 'native_smoke'),
                         executable_sha256=executable_sha256)
    verify_notices(stage)
    output.mkdir(parents=True, exist_ok=False)
    write_archive(stage, output / ZIP_NAME, expected)
    shutil.copyfile(stage / EXE_NAME, output / EXE_NAME)
    assets = {name: {'sha256': sha(output / name), 'size': (output / name).stat().st_size}
              for name in (ZIP_NAME, EXE_NAME)}
    write_json(output / EVIDENCE_NAME, {
        'schema_version': 'recheck-porting-release-1',
        'source_sha': commit, 'version': candidate.version, 'platform': PLATFORM,
        'source_aggregate_sha256': aggregate, 'assets': assets,
        'checks': {'source_tests': 'success', 'ruff': 'success', 'notices': 'success',
                   'archive_inventory': 'success'},
        'source': source, 'frozen': frozen,
    })


def write_checksums(directory):
    (directory / CHECKSUMS_NAME).write_text(
        ''.join(f'{sha(directory / name)}  {name}\n' for name in ASSETS[:-1]),
        encoding='ascii', newline='\n',
    )


def finalize_bundle(directory, candidate, commit, native):
    if {file.name for file in directory.iterdir()} != set(ASSETS[:-1]):
        raise SafeError('Release artifact has an unexpected file set')
    proof = read_json(directory / EVIDENCE_NAME)
    if (proof.get('schema_version') != 'recheck-porting-release-1'
            or proof.get('source_sha') != commit or proof.get('version') != candidate.version
            or proof.get('platform') != PLATFORM):
        raise SafeError('Release artifact identity does not match the requested candidate')
    for key in ('source_tests', 'ruff', 'notices', 'archive_inventory'):
        if proof.get('checks', {}).get(key) != 'success':
            raise SafeError(f'Release artifact is missing a successful {key} check')
    for name in (ZIP_NAME, EXE_NAME):
        path = directory / name
        if (not path.is_file() or path.is_symlink()
                or proof.get('assets', {}).get(name) != {'sha256': sha(path), 'size': path.stat().st_size}):
            raise SafeError(f'Release artifact size or SHA-256 does not match: {name}')
    require_verification(proof.get('source'), commit, SOURCE_CHECKS)
    require_verification(proof.get('frozen'), commit, (*SOURCE_CHECKS, 'frozen', 'native_smoke'),
                         executable_sha256=proof['assets'][EXE_NAME]['sha256'])
    require_verification(native, commit, ISOLATED_CHECKS,
                         executable_sha256=proof['assets'][EXE_NAME]['sha256'])
    if native['checks']['native'].get('platform') != 'windows/x64':
        raise SafeError('Isolated worker evidence must match Windows x64')
    if native.get('archive_sha256') != proof['assets'][ZIP_NAME]['sha256']:
        raise SafeError('Isolated worker evidence does not match the release ZIP SHA-256')
    with zipfile.ZipFile(directory / ZIP_NAME) as archive:
        payload = {item.filename for item in archive.infolist() if not item.is_dir()}
        if not {f'Recheck/{name}' for name in REQUIRED_PAYLOAD} <= payload:
            raise SafeError('Release ZIP lacks its required files')
        with archive.open(f'Recheck/{EXE_NAME}') as entry:
            if hashlib.file_digest(entry, 'sha256').hexdigest() != proof['assets'][EXE_NAME]['sha256']:
                raise SafeError('ZIP executable differs from the separately uploaded executable')
        aggregate = require_build_identity(
            json.loads(archive.read(f'Recheck/_internal/{IDENTITY}')), commit, candidate, checkout_runtime(commit),
        )
        if aggregate != proof.get('source_aggregate_sha256'):
            raise SafeError('ZIP source identity differs from build evidence')
    # Release only the gate outcomes. Internal worker configuration and logs stay in CI.
    proof['source'] = {name: proof['source']['checks'][name]['status'] for name in SOURCE_CHECKS}
    proof['frozen'] = {
        name: proof['frozen']['checks'][name]['status']
        for name in (*SOURCE_CHECKS, 'frozen', 'native_smoke')
    }
    proof['isolated_worker'] = {
        name: native['checks'][name]['status'] for name in ISOLATED_CHECKS
    }
    proof['isolated_worker_platform'] = native['checks']['native']['platform']
    proof['isolated_worker_archive_sha256'] = native['archive_sha256']
    return proof


def release_body(candidate, proof, run_url):
    changes = []
    for change in candidate.changes:
        if change not in RELEASE_CHANGES:
            raise SafeError('Release changes must use verified change IDs')
        message, paths = RELEASE_CHANGES[change]
        for path in paths:
            scope, check = path.split('.')
            if proof.get(scope, {}).get(check) != 'success':
                raise SafeError(f'Release change {change} requires successful evidence at {path}')
        evidence = ', '.join(f'`{path}`' for path in paths)
        changes.append(f'- {message} (`{EVIDENCE_NAME}`의 {evidence})')
    checks = [
        f'- [{workflow}]({record["html_url"]}) 성공'
        for workflow, record in proof['ci'].items()
    ]
    return '\n'.join([
        'Recheck는 프로그램과 배포물을 비교하고 동작·구조·구성 차이의 근거를 남깁니다.',
        '포팅 검증은 버전 간 회귀 검사와 교체 후보 검증에 함께 쓰는 시나리오 비교의 한 사례입니다.',
        '',
        '## 변경 사항',
        '',
        *changes,
        '',
        '## 검증 범위',
        '',
        'Windows x64 앱 ZIP과 실행 파일을 제공하며 ZIP 안에 필수 DLL과 리소스를 포함합니다.',
        'ZIP 전체를 푼 뒤 Recheck.exe를 실행하세요.',
        '독립 EXE 자산은 ZIP과 동일한 실행 파일을 대조하는 용도입니다.',
        '소스와 Windows 배포본에서 기본 포팅 비교, CLI 결과, JSON·HTML 보고서 및 GUI 시작을 확인했습니다.',
        'Windows 배포본으로 실제 격리 작업자의 정상·결함 사례를 실행해 사전 고정한 결과와 대조했습니다.',
        'Linux·macOS, Windows ARM64, 다른 VM 구성, AI 해석 및 오프라인 Docker 이미지는 검증·배포 범위 밖입니다.',
        '',
        f'- 소스 커밋 `{proof["source_sha"]}`',
        f'- [빌드·실행 검증 결과]({run_url})',
        *checks,
        '- 자산 크기와 SHA-256은 `recheck-verification.json` 및 `SHA256SUMS.txt`에서 확인할 수 있습니다.',
    ])


def asset_rows(directory):
    return {
        name: {'size': (directory / name).stat().st_size, 'sha256': sha(directory / name)}
        for name in ASSETS
    }


def require_assets(release, expected, uploaded):
    rows = release.get('assets', [])
    if len(rows) != len(expected) or {row['name'] for row in rows} != set(expected):
        raise SafeError('Release asset set does not match the verified upload set')
    for row in rows:
        name = row['name']
        info = expected[name]
        if (type(uploaded[name]) is not int or uploaded[name] <= 0 or row.get('id') != uploaded[name]
                or row.get('state') != 'uploaded' or row.get('size') != info['size']
                or row.get('digest') != 'sha256:' + info['sha256']):
            raise SafeError(f'Release asset metadata did not verify: {name}')


def require_new_version(api, candidate, commit):
    tag_ref = api.endpoint('/git/ref/tags/' + candidate.tag)
    if api.request('GET', tag_ref, missing_ok=True) is not None or api.release_for_tag() is not None:
        raise SafeError('Release version already has a tag or release; no replacement was made')
    for page in range(1, 21):
        tags = api.request('GET', api.endpoint(f'/tags?per_page=100&page={page}'))
        for tag in tags:
            match = re.fullmatch(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', tag['name'])
            if match:
                if checked_sha(tag['commit']['sha']) == commit:
                    raise SafeError('The verified commit already has a version tag')
                if tuple(map(int, match.groups())) >= candidate.parts:
                    raise SafeError('New release version must exceed existing public version tags')
        if len(tags) < 100:
            return
    raise SafeError('Tag inventory exceeded its bounded verification range')


def publish_verified_release(api, candidate, commit, directory, native, run_url, *, wait_seconds=0):
    proof = finalize_bundle(directory, candidate, commit, native)
    api.assert_repository()
    require_protected_main(api, commit)
    require_new_version(api, candidate, commit)
    proof['ci'] = require_ci(api, commit, wait_seconds=wait_seconds)
    proof['actions_run_url'] = run_url
    body = release_body(candidate, proof, run_url)
    write_json(directory / EVIDENCE_NAME, proof)
    write_checksums(directory)
    expected = asset_rows(directory)
    require_protected_main(api, commit)
    api.request('POST', api.endpoint('/git/refs'), {'ref': 'refs/tags/' + candidate.tag, 'sha': commit})
    api.assert_lightweight_tag(commit)
    draft = api.request('POST', api.endpoint('/releases'), {
        'tag_name': candidate.tag, 'target_commitish': commit, 'name': 'Recheck ' + candidate.version,
        'body': body, 'draft': True, 'prerelease': candidate.prerelease,
        'generate_release_notes': False,
    })
    release_id = draft.get('id')
    release = api._selected_release(release_id, commit, candidate.prerelease, pinned_tag=True)
    if release.get('assets') != []:
        raise SafeError('A new draft with no existing assets is required')
    uploaded = {}
    for name, info in expected.items():
        asset = api.upload_verified_asset(
            release_id, directory / name, sha=commit, expected_sha256=info['sha256'],
            expected_size=info['size'], prerelease=candidate.prerelease,
        )
        uploaded[name] = asset.get('id')
        api.verify_asset_bytes(uploaded[name], expected_sha256=info['sha256'], expected_size=info['size'])
    release = api._selected_release(release_id, commit, candidate.prerelease, pinned_tag=True)
    require_assets(release, expected, uploaded)
    require_ci(api, commit)
    api.assert_lightweight_tag(commit)
    require_protected_main(api, commit)
    endpoint = api.endpoint(f'/releases/{release_id}')
    api.request('PATCH', endpoint, {
        'draft': False, 'prerelease': candidate.prerelease,
        'make_latest': 'false' if candidate.prerelease else 'true',
    })
    published = api.request('GET', endpoint)
    url = f'https://github.com/{REPOSITORY}/releases/tag/{candidate.tag}'
    if (published.get('id') != release_id or published.get('tag_name') != candidate.tag
            or published.get('draft') is not False or published.get('prerelease') is not candidate.prerelease
            or published.get('html_url') != url or not published.get('published_at')):
        raise SafeError('Published release identity did not verify')
    api.assert_lightweight_tag(commit)
    require_assets(published, expected, uploaded)
    return {'published': True, 'url': url, 'tag': candidate.tag, 'source_sha': commit,
            'assets': expected, 'ci': proof['ci']}


def stage_smoke(exe, output, frozen_proof, commit):
    from scripts.verify_native_release import validate_gui_smoke

    proof = read_json(frozen_proof)
    require_verification(proof, commit, (*SOURCE_CHECKS, 'frozen'), executable_sha256=sha(exe))
    output.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, 'RECHECK_DATA_DIR': str(output / 'data'),
           'RECHECK_SMOKE_DIR': str(output), 'PYTHONUTF8': '1'}
    env.pop('QT_QPA_PLATFORM', None)
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    with (output / 'frozen-native-smoke.log').open('wb') as log:
        subprocess.run([str(exe), '--smoke-test'], cwd=exe.parent, env=env, timeout=90,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    validate_gui_smoke(output)
    proof['checks']['native_smoke'] = {'status': 'success'}
    write_json(frozen_proof, proof)


def actions_push(commit):
    env = os.environ
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'push'
            or env.get('GITHUB_REF') != 'refs/heads/main' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_SHA') != commit):
        raise SafeError('Publication requires an exact-SHA push to main in the selected GitHub repository')
    run_id = env.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'[1-9][0-9]*', run_id):
        raise SafeError('A GitHub Actions run identity is required')
    return f'https://github.com/{REPOSITORY}/actions/runs/{run_id}'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sha', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('candidate', 'bundle', 'publish'):
        commands.add_parser(name).add_argument('--before', required=True)
    commands.add_parser('bind-source')
    commands.add_parser('verify-source').add_argument('--proof', type=Path, required=True)
    smoke = commands.add_parser('smoke')
    for name in ('exe', 'output', 'proof'):
        smoke.add_argument('--' + name, type=Path, required=True)
    bundle = commands.choices['bundle']
    for name in ('stage', 'inventory', 'source', 'frozen', 'output'):
        bundle.add_argument('--' + name, type=Path, required=True)
    publish = commands.choices['publish']
    for name in ('directory', 'native'):
        publish.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        commit = checked_sha(args.sha)
        if args.command == 'bind-source':
            bind_source_identity(commit)
        elif args.command == 'verify-source':
            require_verification(read_json(args.proof), commit, SOURCE_CHECKS)
        elif args.command == 'smoke':
            stage_smoke(args.exe.resolve(strict=True), args.output.resolve(), args.proof, commit)
        else:
            candidate = requested_candidate(ROOT, args.before, commit)
            if args.command == 'candidate':
                print('true' if candidate else 'false')
                return 0
            if candidate is None:
                raise SafeError('No new release version is requested for this push')
            if args.command == 'bundle':
                bundle_assets(candidate, commit, args.stage, read_json(args.inventory),
                              read_json(args.source), read_json(args.frozen), args.output)
            else:
                run_url = actions_push(commit)
                token = os.environ.get('GITHUB_TOKEN')
                if not token:
                    raise SafeError('The GitHub Actions release token is required')
                api = GitHub(execute=True, token=token, branch='main', tag=candidate.tag)
                record = publish_verified_release(api, candidate, commit, args.directory,
                                                  read_json(args.native), run_url, wait_seconds=900)
                print(json.dumps(record, indent=2))
                with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a', encoding='utf-8') as summary:
                    summary.write(f'## [Recheck {record["tag"]}]({record["url"]})\n\n'
                                  f'Published from `{record["source_sha"]}`.\n\n')
                    for name, row in record['assets'].items():
                        summary.write(f'- `{name}` ({row["size"]} bytes) SHA-256 `{row["sha256"]}`\n')
                    for workflow, row in record['ci'].items():
                        summary.write(f'- [{workflow}]({row["html_url"]}) {row["conclusion"]}\n')
    except (SafeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Release gate failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
