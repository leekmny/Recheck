"""Managed distribution staging rejects missing/tampered archives and local data."""

import hashlib
import importlib
import json
from pathlib import Path

import pytest

DOCS = (
    'REVIEW_AND_RECHECK.md',
    'ANALYSIS_MODES.md',
    'AI_CONNECTIONS.md',
    'AI_AUTH_STATUS.md',
    'UX_UPGRADE_20261115.md',
    'GITHUB_WORKFLOW.md',
    'CONTAINER_RUNTIME.md',
    '검증범위.txt',
    'DISTRIBUTION_AND_RECOVERY.md',
    'WINDOWS_DOCKER.md',
    'FIRST_RUN.md',
    'BINARY_DISTRIBUTION.md',
    'PUBLIC_HISTORY.md',
    'PUBLICATION_SCOPE.md',
    'PROJECT_STATUS.md',
    'GEMINI_FREE_SETUP.md',
    'PACKAGE_COMPARISON.md',
    'ROADMAP.md',
    'PORTING_RELEASE.md',
    'PORTING_VALIDATION_UPDATE.md',
)
COMPONENTS = ('static', 'execution', 'ollama')


@pytest.fixture
def package():
    module = importlib.import_module('scripts.package_verified')
    assert hasattr(
        module, 'prepare_distribution_files'
    ), 'Verified distribution staging helper is missing'
    return module


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'source'
    staged = tmp_path / 'staged'
    root.mkdir()
    staged.mkdir()
    (root / 'docs').mkdir()
    (root / 'environment').mkdir()
    (root / 'resources/runtime').mkdir(parents=True)
    (staged / '_internal/resources/runtime').mkdir(parents=True)
    (root / 'README.md').write_text('reviewed README')
    (root / 'requirements-lock.txt').write_text('locked requirements')
    for name in DOCS:
        (root / 'docs' / name).write_text('reviewed guide ' + name, encoding='utf-8')
    manifest = {'version': 1, 'images': {}}
    for index, component in enumerate(COMPONENTS, 1):
        payload = (component + ' immutable archive').encode()
        name = f'recheck-managed-{component}-1.tar'
        (root / 'environment' / name).write_bytes(payload)
        manifest['images'][component] = {
            'image_id': 'sha256:' + str(index) * 64,
            'platform': 'linux/amd64',
            'archive': {
                'filename': name,
                'sha256': hashlib.sha256(payload).hexdigest(),
                'size_bytes': len(payload),
            },
        }
    raw = json.dumps(manifest, indent=2).encode()
    (root / 'resources/runtime/environment.json').write_bytes(raw)
    (staged / '_internal/resources/runtime/environment.json').write_bytes(raw)
    return root, staged, manifest, raw


def test_default_staging_copies_exact_verified_archives_and_curated_guides(package, project):
    root, staged, manifest, raw = project
    package.prepare_distribution_files(staged, root=root)
    assert (staged / 'environment/environment-manifest.json').read_bytes() == raw
    assert {p.name for p in (staged / 'docs').iterdir()} == set(DOCS)
    assert (staged / 'README.md').read_bytes() == (root / 'README.md').read_bytes()
    for component in COMPONENTS:
        spec = manifest['images'][component]['archive']
        archived = staged / 'environment' / spec['filename']
        assert archived.stat().st_size == spec['size_bytes']
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == spec['sha256']


@pytest.mark.parametrize('component', COMPONENTS)
def test_missing_required_archive_blocks_before_any_handoff_copy(package, project, component):
    root, staged, manifest, _ = project
    (root / 'environment' / manifest['images'][component]['archive']['filename']).unlink()
    with pytest.raises((ValueError, FileNotFoundError), match='archive|environment'):
        package.prepare_distribution_files(staged, root=root)
    assert not (staged / 'environment').exists()
    assert not (staged / 'README.md').exists()


def test_same_size_archive_tamper_is_rejected(package, project):
    root, staged, manifest, _ = project
    spec = manifest['images']['execution']['archive']
    path = root / 'environment' / spec['filename']
    path.write_bytes(b'x' * spec['size_bytes'])
    with pytest.raises(ValueError, match='checksum|SHA|hash'):
        package.prepare_distribution_files(staged, root=root)
    assert not (staged / 'environment').exists()


def test_local_docs_credentials_models_and_extra_archives_are_excluded(package, project):
    root, staged, _, _ = project
    (root / 'docs/auth.json').write_text('credential')
    (root / 'docs/WORK_LOG.md').write_text('private notes')
    (root / 'environment/extra.tar').write_bytes(b'unapproved image')
    (root / 'environment/auth.json').write_text('credential')
    (root / 'environment/models').mkdir()
    (root / 'environment/models/weights.bin').write_bytes(b'model volume')
    (root / 'credentials').mkdir()
    (root / 'credentials/settings.json').write_text('secret')
    package.prepare_distribution_files(staged, root=root)
    copied = {
        str(p.relative_to(staged)).replace('\\', '/') for p in staged.rglob('*') if p.is_file()
    }
    assert not any(
        'auth.json' in path
        or 'WORK_LOG' in path
        or 'models/' in path
        or 'credentials/' in path
        or 'extra.tar' in path
        for path in copied
    )
    assert {p.name for p in (staged / 'environment').iterdir()} == {
        'environment-manifest.json',
        *(f'recheck-managed-{c}-1.tar' for c in COMPONENTS),
    }


def test_app_only_is_explicit_and_skips_missing_archives(package, project):
    root, staged, manifest, _ = project
    for component in COMPONENTS:
        (root / 'environment' / manifest['images'][component]['archive']['filename']).unlink()
    package.prepare_distribution_files(staged, root=root, include_environment=False)
    assert not (staged / 'environment').exists()
    assert (staged / 'README.md').is_file()


def test_manifest_filename_cannot_escape_archive_folder(package, project):
    root, staged, manifest, _ = project
    manifest['images']['static']['archive']['filename'] = '../credentials/auth.json'
    raw = json.dumps(manifest).encode()
    (root / 'resources/runtime/environment.json').write_bytes(raw)
    (staged / '_internal/resources/runtime/environment.json').write_bytes(raw)
    with pytest.raises(ValueError, match='filename|archive'):
        package.prepare_distribution_files(staged, root=root)


def test_postcopy_hash_validation_detects_copy_corruption(package, project, monkeypatch):
    root, staged, _, _ = project
    original = package.shutil.copy2

    def corrupt(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(destination).name == 'recheck-managed-static-1.tar':
            Path(destination).write_bytes(b'corrupted copy')
        return result

    monkeypatch.setattr(package.shutil, 'copy2', corrupt)
    with pytest.raises(ValueError, match='copy|checksum|size|hash'):
        package.prepare_distribution_files(staged, root=root)


def test_inventory_hashes_large_files_by_streaming(package, tmp_path, monkeypatch):
    archive = tmp_path / 'large.tar'
    archive.write_bytes(b'archive payload' * 100000)
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()

    def forbidden(*args, **kwargs):
        raise AssertionError('inventory must not materialize archive bytes')

    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    actual = package.inventory(tmp_path)
    assert actual == [{'path': 'large.tar', 'size': archive.stat().st_size, 'sha256': expected}]
