import base64
import hashlib
import json
import subprocess
import zipfile

import pytest

from scripts.github_release import SafeError


SHA = 'a' * 40
VERSION = '0.1.8'
PRODUCT_CHECKS = ('normal', 'defect', 'cli', 'json', 'html', 'gui', 'rerun')


@pytest.fixture
def release_gate():
    from scripts import porting_release

    return porting_release


def candidate(version):
    return {'version': version, 'changes': ['result-reporting']}


def test_candidate_channel_is_derived_from_repository_version_policy(release_gate):
    prototype = release_gate.parse_candidate(candidate('0.1.8'))
    stable = release_gate.parse_candidate(candidate('1.0.0'))
    assert (prototype.tag, prototype.prerelease) == ('v0.1.8', True)
    assert (stable.tag, stable.prerelease) == ('v1.0.0', False)


def test_candidate_explicitly_selects_hosted_fixture_verification(release_gate):
    hosted = 'github-hosted-windows-fixtures'
    assert release_gate.parse_candidate(candidate(VERSION)).verification == 'ssh'
    assert release_gate.parse_candidate(candidate(VERSION) | {'verification': hosted}).verification == hosted
    with pytest.raises(SafeError, match='verification'):
        release_gate.parse_candidate(candidate(VERSION) | {'verification': 'trusted-fixtures'})


def test_hosted_manifest_changes_only_the_windows_rust_linker_target():
    from recheck import porting_release_fixtures as hosted
    from samples.porting.console.fixtures import manifest

    original = manifest('windows', 'x64', variant='defect')
    selected = hosted.hosted_manifest('defect')
    assert selected['scenarios'] == original['scenarios']
    assert selected['targets']['reference'] == original['targets']['reference']
    assert original['targets']['candidate']['build']['args'][-6:] == [
        '--target', 'x86_64-pc-windows-gnu', '-C', 'link-self-contained=yes', '--cfg', 'defect',
    ]
    assert selected['targets']['candidate']['build']['args'][-4:] == [
        '--target', 'x86_64-pc-windows-msvc', '--cfg', 'defect',
    ]
    assert selected['targets']['candidate']['files'] == ['fee.rs']


def test_failed_hosted_fixture_reports_known_outputs_and_redacts_other_data():
    from recheck import porting_release_fixtures as hosted

    def observation(stdout, stderr, exit_code, status='completed'):
        return {'stdout_b64': base64.b64encode(stdout).decode(), 'stderr_b64': base64.b64encode(stderr).decode(),
                'status': status, 'exit_code': exit_code, 'argv': [r'C:\Users\Private\example.exe'],
                'build': {'status': 'completed', 'exit_code': 0, 'error': 'PRIVATE_ENV_TOKEN'}}

    row = {'id': 'above', 'outcome': 'mismatched', 'attempts': [
        {'observations': {'reference': observation(b'375\r\n', b'', 0),
                          'candidate': observation(b'PRIVATE_ENV_TOKEN', b'invalid\n', 2)},
         'comparison': {'expected': {'reference': {'status': 'passed'}, 'candidate': {'status': 'failed'}}}},
    ]}
    with pytest.raises(RuntimeError, match='normal') as failure:
        hosted.validate_fixture_result({'scenarios': [row]}, 'normal', {}, [])
    message = str(failure.value)
    assert 'PRIVATE_ENV_TOKEN' not in message and 'Private' not in message
    detail = json.loads(message.partition('details=')[2])[0]
    assert detail['id'] == 'above' and detail['outcome'] == 'mismatched'
    assert detail['reference'] == {'status': 'completed', 'exit_code': 0, 'build_exit_code': 0,
                                   'expected': 'passed', 'stdout': '375\r\n', 'stderr': ''}
    assert detail['candidate'] == {'status': 'completed', 'exit_code': 2, 'build_exit_code': 0,
                                   'expected': 'failed', 'stdout': '<redacted>', 'stderr': 'invalid\n'}


@pytest.mark.parametrize('version', ['v0.1.8', '0.01.8', '0.1.8-draft', '0.1.8+1', '0.1', '0.1.65536'])
def test_candidate_rejects_noncanonical_or_unrepresentable_versions(release_gate, version):
    with pytest.raises(SafeError, match='version'):
        release_gate.parse_candidate(candidate(version))


@pytest.mark.parametrize('changes', [
    [], [['console-porting']], ['console-porting', 'console-porting'],
    ['macOS ARM64 native execution and packaging were verified.'],
])
def test_candidate_only_accepts_unique_verified_change_ids(release_gate, changes):
    with pytest.raises(SafeError, match='Release changes'):
        release_gate.parse_candidate({'version': VERSION, 'changes': changes})


def test_release_notes_require_evidence_for_the_selected_claim(release_gate):
    selected = release_gate.parse_candidate(candidate(VERSION))
    proof = {'source_sha': SHA, 'ci': {},
             'source': {'cli': 'success', 'json': 'success', 'html': 'success'},
             'frozen': {'cli': 'success', 'json': 'success', 'html': 'skipped'}}
    with pytest.raises(SafeError, match='frozen.html'):
        release_gate.release_body(selected, proof, 'https://example.invalid/run')


def test_pending_candidate_is_retried_at_a_later_commit_until_tagged(release_gate, tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()

    def commit(message):
        git('add', '.')
        git('-c', 'user.name=Example', '-c', 'user.email=example@example.test', 'commit', '-m', message)
        return git('rev-parse', 'HEAD')

    git('init', '-q')
    cfg = tmp_path / '.github' / 'porting-release.json'
    cfg.parent.mkdir()
    (tmp_path / 'README.md').write_text('Initial version', encoding='utf-8')
    before = commit('initial')
    cfg.write_text(json.dumps(candidate(VERSION)), encoding='utf-8')
    requested = commit('request release')
    assert release_gate.requested_candidate(tmp_path, before, requested).tag == 'v0.1.8'

    (tmp_path / 'README.md').write_text('Fix verification failure', encoding='utf-8')
    after = commit('fix verification for the same version')
    assert release_gate.requested_candidate(tmp_path, requested, after).tag == 'v0.1.8'

    git('tag', 'v0.1.8', requested)
    assert release_gate.requested_candidate(tmp_path, requested, after) is None
    assert release_gate.requested_candidate(tmp_path, before, requested) is None

    cfg.write_text(json.dumps(candidate('0.1.7')), encoding='utf-8')
    downgraded = commit('lower requested version')
    with pytest.raises(SafeError, match='must increase'):
        release_gate.requested_candidate(tmp_path, after, downgraded)


def test_checkout_runtime_requires_pinned_clean_source(release_gate, tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()

    (tmp_path / '.gitattributes').write_text('* -text\n', encoding='utf-8')
    source = tmp_path / 'launch.py'
    data = b'print("release snapshot")\r\n'
    source.write_bytes(data)
    identity = tmp_path / 'RECHECK_SOURCE_IDENTITY.json'
    identity.write_text('{}\n', encoding='utf-8')
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Example', '-c', 'user.email=example@example.test', 'commit', '-m', 'fixture')
    commit = git('rev-parse', 'HEAD')
    monkeypatch.setattr(release_gate, 'ROOT', tmp_path)

    expected = {'launch.py': hashlib.sha256(data).hexdigest()}
    assert release_gate.checkout_runtime(commit) == expected
    identity.write_text('{"generated": true}\n', encoding='utf-8')
    assert release_gate.checkout_runtime(commit, allow_generated_identity=True) == expected
    with pytest.raises(SafeError, match='Build inputs differ'):
        release_gate.checkout_runtime(commit)
    source.write_bytes(b'print("different source")\r\n')
    with pytest.raises(SafeError, match='Build inputs differ'):
        release_gate.checkout_runtime(commit, allow_generated_identity=True)


def verification(checks=PRODUCT_CHECKS):
    proof = {
        'schema_version': 'recheck-porting-verification-1',
        'status': 'success',
        'source_sha': SHA,
        'checks': {name: {'status': 'success'} for name in checks},
    }
    if 'native' in checks:
        proof['checks']['native']['platform'] = 'windows/x64'
    return proof


@pytest.mark.parametrize('damage', ['missing', 'skip', 'false', 'commit', 'schema', 'exe'])
def test_product_gate_rejects_missing_or_mismatched_verification(release_gate, damage):
    proof = verification((*PRODUCT_CHECKS, 'native', 'frozen'))
    proof['executable_sha256'] = 'd' * 64
    if damage == 'missing':
        del proof['checks']['html']
    elif damage == 'skip':
        proof['checks']['native']['status'] = 'skipped'
    elif damage == 'false':
        proof['checks']['frozen']['status'] = True
    elif damage == 'commit':
        proof['source_sha'] = 'b' * 40
    elif damage == 'schema':
        proof['schema_version'] = 'unknown'
    elif damage == 'exe':
        proof['executable_sha256'] = 'e' * 64
    with pytest.raises(SafeError):
        release_gate.require_verification(
            proof, SHA, (*PRODUCT_CHECKS, 'native', 'frozen'), executable_sha256='d' * 64,
        )


def test_product_gate_admits_matching_measured_evidence(release_gate):
    proof = verification((*PRODUCT_CHECKS, 'native', 'frozen'))
    proof['executable_sha256'] = 'd' * 64
    release_gate.require_verification(
        proof, SHA, (*PRODUCT_CHECKS, 'native', 'frozen'), executable_sha256='d' * 64,
    )


class BranchAPI:
    def __init__(self, *, protected=True, head=SHA):
        self.protected, self.head = protected, head

    def endpoint(self, suffix):
        return suffix

    def request(self, method, path):
        assert method == 'GET' and path == '/branches/main'
        return {'name': 'main', 'protected': self.protected, 'commit': {'sha': self.head}}


@pytest.mark.parametrize(('protected', 'head'), [(False, SHA), (True, 'b' * 40), ('true', SHA)])
def test_wrong_or_unprotected_main_is_rejected(release_gate, protected, head):
    with pytest.raises(SafeError, match='protected main'):
        release_gate.require_protected_main(BranchAPI(protected=protected, head=head), SHA)


def test_exact_protected_main_is_admitted(release_gate):
    release_gate.require_protected_main(BranchAPI(), SHA)


class ActionsAPI:
    def __init__(self, damage):
        self.damage = damage

    def endpoint(self, suffix):
        return suffix

    def request(self, method, path):
        assert method == 'GET'
        if path.startswith('/actions/workflows/'):
            name = path.split('/')[3]
            return {'total_count': 1, 'workflow_runs': [{
                'id': {'tests.yml': 123, 'security.yml': 456, 'porting-ci.yml': 789}[name],
                'head_sha': 'b' * 40 if self.damage == 'commit' else SHA,
                'head_branch': 'main',
                'event': 'push',
                'path': '.github/workflows/' + name,
                'status': 'completed',
                'conclusion': 'failure' if self.damage == 'failed' else 'success',
                'html_url': 'https://github.com/leekmny/Recheck/actions/runs/123',
            }]}
        jobs = {
            '123': ['tests'],
            '456': ['CodeQL (python)', 'CodeQL (javascript-typescript)', 'dependencies'],
            '789': ['porting-product', 'porting-linux-x64'],
        }[path.split('/')[3]]
        if self.damage == 'linux-missing' and 'porting-linux-x64' in jobs:
            jobs.remove('porting-linux-x64')
        return {'total_count': len(jobs), 'jobs': [
            {'name': name, 'status': 'completed',
             'conclusion': 'skipped' if self.damage == 'skipped' or (
                 self.damage == 'linux-skipped' and name == 'porting-linux-x64'
             ) else 'success'}
            for name in jobs
        ]}


@pytest.mark.parametrize('damage', ['commit', 'failed', 'skipped'])
def test_actual_required_workflows_and_jobs_must_be_green_for_commit(release_gate, damage):
    with pytest.raises(SafeError, match='CI'):
        release_gate.require_ci(ActionsAPI(damage), SHA)


def test_required_ci_reports_the_reviewed_run_results(release_gate):
    result = release_gate.require_ci(ActionsAPI(None), SHA)
    assert {key: value['id'] for key, value in result.items()} == {
        'tests.yml': 123, 'security.yml': 456, 'porting-ci.yml': 789,
    }


@pytest.fixture
def draft_bundle(tmp_path, monkeypatch, release_gate):
    exe = tmp_path / 'Recheck.exe'
    exe.write_bytes(b'fixture executable')
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    runtime = {'launch.py': 'b' * 64}
    monkeypatch.setattr(release_gate, 'checkout_runtime', lambda commit: runtime, raising=False)
    aggregate = hashlib.sha256(json.dumps(runtime, separators=(',', ':')).encode()).hexdigest()
    identity = {
        'source_git_commit': SHA,
        'display_version': VERSION,
        'python_metadata_version': VERSION,
        'windows_numeric_version': [0, 1, 8, 0],
        'runtime_identity': runtime,
        'aggregate_sha256': aggregate,
    }
    archive_path = tmp_path / 'Recheck-Windows-x64.zip'
    with zipfile.ZipFile(archive_path, 'x') as archive:
        archive.write(exe, 'Recheck/Recheck.exe')
        archive.writestr('Recheck/_internal/RECHECK_SOURCE_IDENTITY.json', json.dumps(identity))
        for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'licenses/manifest.json',
                     'licenses/REPLACEMENT.md', 'docs/FIRST_RUN.md', 'docs/PORTING_RELEASE.md'):
            archive.writestr('Recheck/' + name, 'document fixture')
    frozen = verification((*PRODUCT_CHECKS, 'frozen', 'native_smoke'))
    frozen['executable_sha256'] = digest
    proof = {
        'schema_version': 'recheck-porting-release-1',
        'source_sha': SHA, 'version': VERSION, 'platform': 'windows-x64',
        'source_aggregate_sha256': aggregate,
        'checks': dict.fromkeys(('source_tests', 'ruff', 'notices', 'archive_inventory'), 'success'),
        'assets': {
            path.name: {'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in (exe, archive_path)
        },
        'source': verification(), 'frozen': frozen,
    }
    (tmp_path / 'recheck-verification.json').write_text(json.dumps(proof), encoding='utf-8')
    return tmp_path


def isolated_proof(bundle):
    proof = verification((*PRODUCT_CHECKS, 'native', 'frozen'))
    proof['executable_sha256'] = hashlib.sha256((bundle / 'Recheck.exe').read_bytes()).hexdigest()
    proof['archive_sha256'] = hashlib.sha256((bundle / 'Recheck-Windows-x64.zip').read_bytes()).hexdigest()
    return proof


def hosted_proof(bundle):
    proof = isolated_proof(bundle)
    hosted = 'github-hosted-windows-fixtures'
    proof.update(working_tree_dirty=False,
                 github={'event': 'push', 'run_id': 123, 'run_attempt': 2, 'build_artifact_id': 5})
    proof['checks']['native'].update(
        mode=hosted, command='frozen-fixtures', native_arch='x64', emulated=False,
        source_sha=SHA, executable_sha256=proof['executable_sha256'],
        fixture_sha256={'fee.c': '1' * 64, 'fee.rs': '2' * 64},
        variants={
            name: {'status': 'success', 'denominator': 11, 'mismatched': len(ids), 'mismatch_ids': ids,
                   'acceptance': 'failed' if ids else 'passed', 'target_starts': 22,
                   'manifest_sha256': '3' * 64}
            for name, ids in [('normal', []), ('defect', ['above', 'higher', 'maximum']), ('alternate', [])]
        },
    )
    return proof


@pytest.mark.parametrize('damage', [None, 'source-path', 'fixture-hash', 'emulated', 'shallow', 'wrong-ids',
                                     'dirty', 'wrong-mode'])
def test_hosted_release_requires_frozen_fixture_evidence(release_gate, draft_bundle, monkeypatch, damage):
    selected = release_gate.parse_candidate(candidate(VERSION) | {'verification': 'github-hosted-windows-fixtures'})
    proof = hosted_proof(draft_bundle)
    monkeypatch.setattr(release_gate, 'hosted_fixture_hashes', lambda: {'fee.c': '1' * 64, 'fee.rs': '2' * 64})
    native = proof['checks']['native']
    if damage == 'source-path':
        native['command'] = 'source-fixtures'
    elif damage == 'fixture-hash':
        native['fixture_sha256']['fee.c'] = '0' * 64
    elif damage == 'emulated':
        native['emulated'] = True
    elif damage == 'shallow':
        native['variants']['defect']['target_starts'] = 20
    elif damage == 'wrong-ids':
        native['variants']['defect']['mismatch_ids'] = ['zero', 'above', 'maximum']
    elif damage == 'dirty':
        proof['working_tree_dirty'] = True
    elif damage == 'wrong-mode':
        native.pop('mode')
    if damage:
        with pytest.raises(SafeError, match='Hosted'):
            release_gate.finalize_bundle(draft_bundle, selected, SHA, proof)
    else:
        final = release_gate.finalize_bundle(draft_bundle, selected, SHA, proof)
        assert final['isolated_worker_mode'] == selected.verification
        assert final['hosted_fixture_verification']['variants']['defect']['mismatched'] == 3


def test_hosted_evidence_cannot_satisfy_an_ssh_request(release_gate, draft_bundle):
    selected = release_gate.parse_candidate(candidate(VERSION))
    with pytest.raises(SafeError, match='SSH'):
        release_gate.finalize_bundle(draft_bundle, selected, SHA, hosted_proof(draft_bundle))


@pytest.mark.parametrize('damage', [None, 'rehearsal', 'wrong-attempt', 'wrong-job', 'wrong-artifact'])
def test_hosted_provenance_requires_the_exact_push_job_and_artifacts(release_gate, draft_bundle, damage):
    proof = hosted_proof(draft_bundle)
    context = {'event': 'push', 'run_id': 123, 'run_attempt': 2}
    run = {'id': 123, 'event': 'push', 'head_sha': SHA, 'head_branch': 'main',
           'run_attempt': 2, 'path': '.github/workflows/porting-release.yml'}
    job = {'name': 'Verify repository fixtures on hosted Windows x64', 'run_id': 123,
           'head_sha': SHA, 'status': 'completed', 'conclusion': 'success',
           'runner_group_name': 'GitHub Actions', 'labels': ['windows-2025']}
    artifacts = {
        artifact_id: {'id': artifact_id, 'name': name, 'expired': False, 'digest': 'sha256:' + 'e' * 64,
                      'workflow_run': {'id': 123, 'head_branch': 'main', 'head_sha': SHA,
                                       'repository_id': 20, 'head_repository_id': 20}}
        for artifact_id, name in [(5, 'recheck-windows-x64-2'), (7, 'recheck-isolated-verification-2')]
    }
    if damage == 'rehearsal':
        proof['github']['event'] = 'pull_request'
    elif damage == 'wrong-attempt':
        run['run_attempt'] = 1
    elif damage == 'wrong-job':
        job['labels'] = ['self-hosted', 'windows-2025']
    elif damage == 'wrong-artifact':
        artifacts[7]['workflow_run']['head_repository_id'] = 99

    class Api:
        def endpoint(self, path):
            return path

        def request(self, method, path):
            assert method == 'GET'
            if path == '/actions/runs/123/attempts/2':
                return run
            if path == '/actions/runs/123/attempts/2/jobs?per_page=100':
                return {'total_count': 1, 'jobs': [job]}
            return artifacts[int(path.removeprefix('/actions/artifacts/'))]

    if damage:
        with pytest.raises(SafeError, match='Hosted'):
            release_gate.require_hosted_actions(Api(), proof, SHA, context, 20, (5, 7))
    else:
        record = release_gate.require_hosted_actions(Api(), proof, SHA, context, 20, (5, 7))
        assert record['build_artifact_id'] == 5 and record['native_artifact_id'] == 7


@pytest.mark.parametrize('damage', [
    'missing-worker', 'wrong-worker-platform', 'wrong-worker-exe', 'changed-exe',
    'different-archive-exe', 'missing-isolated-archive', 'different-isolated-archive',
    'different-archive-source', None,
])
def test_release_bundle_requires_native_evidence_and_matching_executable(release_gate, draft_bundle, damage):
    selected = release_gate.parse_candidate(candidate(VERSION))
    native = isolated_proof(draft_bundle)
    if damage == 'missing-worker':
        native['checks']['native']['status'] = 'skipped'
    if damage == 'wrong-worker-exe':
        native['executable_sha256'] = 'd' * 64
    if damage == 'wrong-worker-platform':
        native['checks']['native']['platform'] = 'linux/x64'
    if damage == 'changed-exe':
        (draft_bundle / 'Recheck.exe').write_bytes(b'changed')
    if damage == 'missing-isolated-archive':
        del native['archive_sha256']
    if damage in ('different-archive-exe', 'different-isolated-archive', 'different-archive-source'):
        archive_path = draft_bundle / 'Recheck-Windows-x64.zip'
        with zipfile.ZipFile(archive_path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        proof_path = draft_bundle / 'recheck-verification.json'
        proof = json.loads(proof_path.read_text(encoding='utf-8'))
        if damage == 'different-archive-exe':
            entries['Recheck/Recheck.exe'] = b'different archive executable'
        elif damage == 'different-isolated-archive':
            entries['Recheck/docs/FIRST_RUN.md'] = b'different archive documentation'
        else:
            identity_name = 'Recheck/_internal/RECHECK_SOURCE_IDENTITY.json'
            identity = json.loads(entries[identity_name])
            identity['runtime_identity']['launch.py'] = 'c' * 64
            runtime = json.dumps(identity['runtime_identity'], separators=(',', ':')).encode()
            identity['aggregate_sha256'] = hashlib.sha256(runtime).hexdigest()
            proof['source_aggregate_sha256'] = identity['aggregate_sha256']
            entries[identity_name] = json.dumps(identity).encode()
        with zipfile.ZipFile(archive_path, 'w') as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        proof['assets'][archive_path.name] = {
            'size': archive_path.stat().st_size,
            'sha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        }
        if damage != 'different-isolated-archive':
            native['archive_sha256'] = proof['assets'][archive_path.name]['sha256']
        proof_path.write_text(json.dumps(proof), encoding='utf-8')
    if damage:
        with pytest.raises(SafeError):
            release_gate.finalize_bundle(draft_bundle, selected, SHA, native)
    else:
        result = release_gate.finalize_bundle(draft_bundle, selected, SHA, native)
        assert result['isolated_worker']['native'] == 'success'
        assert result['source_sha'] == SHA


class DraftAPI(ActionsAPI):
    def __init__(self, damage):
        super().__init__(damage)
        self.release_damage = damage
        self.tag = None
        self.draft = None
        self.downloads = []

    def assert_repository(self):
        return {'full_name': 'leekmny/Recheck', 'private': False, 'visibility': 'public'}

    def release_for_tag(self):
        return self.draft

    def assert_lightweight_tag(self, sha):
        assert self.tag == {'ref': 'refs/tags/v0.1.8', 'sha': sha}

    def _selected_release(self, release_id, sha, prerelease, *, pinned_tag):
        assert (release_id, sha, prerelease, pinned_tag) == (10, SHA, True, True)
        assert self.draft['draft'] is True
        return self.draft

    def request(self, method, path, payload=None, *, missing_ok=False):
        if method == 'GET':
            if path.startswith('/actions/'):
                return super().request(method, path)
            if path == '/branches/main':
                return {'name': 'main', 'protected': True, 'commit': {'sha': SHA}}
            if path == '/git/ref/tags/v0.1.8':
                return {'ref': 'existing'} if self.release_damage == 'collision' else self.tag
            if path == '/tags?per_page=100&page=1':
                return [{'name': 'v0.1.7', 'commit': {
                    'sha': SHA if self.release_damage == 'commit-overlap' else 'c' * 40,
                }}]
            if path == '/releases/10':
                return self.draft
        if method == 'POST' and path == '/git/refs':
            self.tag = payload
            return self.tag
        if method == 'POST' and path == '/releases':
            self.draft = {
                **payload, 'id': 10, 'assets': [],
                'html_url': 'https://github.com/leekmny/Recheck/releases/tag/v0.1.8',
            }
            return self.draft
        if method == 'PATCH' and path == '/releases/10':
            assert self.downloads == [1, 2, 3, 4]
            self.draft |= {**payload, 'published_at': '2026-11-19T00:00:00Z'}
            return self.draft
        raise AssertionError((method, path))

    def upload_verified_asset(self, release_id, path, *, sha, expected_sha256, expected_size, prerelease):
        self._selected_release(release_id, sha, prerelease, pinned_tag=True)
        assert expected_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert expected_size == path.stat().st_size
        assets = self.draft['assets']
        row = {
            'id': len(assets) + 1, 'name': path.name, 'size': expected_size,
            'digest': 'sha256:' + expected_sha256, 'state': 'uploaded',
        }
        assets.append(row)
        return row

    def verify_asset_bytes(self, asset_id, *, expected_sha256, expected_size):
        row = self.draft['assets'][asset_id - 1]
        assert (row['digest'], row['size']) == ('sha256:' + expected_sha256, expected_size)
        if self.release_damage == 'download':
            raise SafeError('Downloaded asset hash differs')
        self.downloads.append(asset_id)


@pytest.mark.parametrize('damage', [
    'collision', 'commit-overlap', 'linux-missing', 'linux-skipped', 'download', None,
])
def test_publication_follows_draft_upload_and_download_verification(release_gate, draft_bundle, damage):
    api = DraftAPI(damage)
    selected = release_gate.parse_candidate({
        'version': VERSION, 'changes': ['console-porting', 'result-reporting', 'scenario-rerun'],
    })
    native = isolated_proof(draft_bundle)
    args = (api, selected, SHA, draft_bundle, native, 'https://github.com/leekmny/Recheck/actions/runs/1')
    if damage:
        match = 'Required CI jobs' if damage.startswith('linux-') else None
        with pytest.raises(SafeError, match=match):
            release_gate.publish_verified_release(*args)
        if damage in ('collision', 'commit-overlap', 'linux-missing', 'linux-skipped'):
            assert api.tag is None and api.draft is None
        else:
            assert api.draft['draft'] is True
    else:
        record = release_gate.publish_verified_release(*args)
        assert record['published'] is True and record['source_sha'] == SHA
        assert set(record['assets']) == {
            'Recheck.exe', 'Recheck-Windows-x64.zip', 'SHA256SUMS.txt', 'recheck-verification.json',
        }
        body = api.draft['body']
        for text in ('콘솔 포팅', 'JSON·HTML 보고서', '결함 시나리오', '`isolated_worker.native`',
                     '`frozen.cli`', '`source.rerun`'):
            assert text in body
        assert 'console-porting' not in body


def test_invalid_release_note_cannot_create_a_tag(release_gate, draft_bundle):
    api = DraftAPI(None)
    selected = release_gate.Candidate(VERSION, (0, 1, 8), ('unverified platform claim',))
    native = isolated_proof(draft_bundle)
    with pytest.raises(SafeError, match='Release changes'):
        release_gate.publish_verified_release(api, selected, SHA, draft_bundle, native,
                                              'https://example.invalid/run')
    assert api.tag is None and api.draft is None
