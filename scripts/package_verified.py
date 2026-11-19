"""Stage a Windows build; preserve previous distribution before publishing it."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json
from scripts.collect_distribution_notices import collect_notices, fetch_sources, DEFAULT_CACHE

ROOT = Path(__file__).resolve().parents[1]
CURATED_DOCS = (
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
MANAGED_COMPONENTS = ('static', 'execution', 'ollama')


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_distribution_files(staged, root=ROOT, include_environment=True):
    """Copy reviewed handoff files and manifest-verified adjacent image archives.

    Archive admission follows the manifest embedded in the frozen application.
    All source archives are checked before any handoff file is copied. Local
    docs, credentials, logs, extra images and model volumes are never enumerated
    as copy inputs. ``include_environment=False`` is an explicit developer build.
    """
    root, staged = Path(root).resolve(), Path(staged).resolve(strict=True)
    if staged == root or not staged.is_dir():
        raise ValueError('A separate existing application staging directory is required')
    source_files = [(root / name, staged / name) for name in ('README.md', 'requirements-lock.txt')]
    source_files += [(root / 'docs' / name, staged / 'docs' / name) for name in CURATED_DOCS]
    for source, target in source_files:
        if not source.is_file() or source.is_symlink():
            raise ValueError('Missing reviewed distribution file: ' + str(source))
        if target.is_symlink() or target.parent.is_symlink():
            raise ValueError('Distribution destinations must not be symbolic links')
    docs = staged / 'docs'
    if docs.exists() and (
        not docs.is_dir()
        or any(path.name not in CURATED_DOCS or not path.is_file() for path in docs.iterdir())
    ):
        raise ValueError(
            'Unreviewed files already exist in staged docs; use a fresh staging directory'
        )

    archives = []
    manifest_bytes = None
    environment = staged / 'environment'
    if include_environment:
        candidates = [
            staged / '_internal/resources/runtime/environment.json',
            staged / 'resources/runtime/environment.json',
        ]
        embedded = next((path for path in candidates if path.is_file()), None)
        if embedded is None:
            raise ValueError('The application is missing its embedded environment manifest')
        manifest_bytes = embedded.read_bytes()
        if (root / 'resources/runtime/environment.json').read_bytes() != manifest_bytes:
            raise ValueError(
                'Embedded environment manifest differs from the reviewed source manifest'
            )
        manifest = json.loads(manifest_bytes.decode('utf-8-sig'))
        if manifest.get('version') != 1 or not isinstance(manifest.get('images'), dict):
            raise ValueError('Unsupported embedded environment manifest')
        source_environment = root / 'environment'
        if source_environment.is_symlink():
            raise ValueError('The archive environment folder must not be a symbolic link')
        for component in MANAGED_COMPONENTS:
            spec = manifest['images'].get(component, {})
            if (
                not re.fullmatch(r'sha256:[0-9a-f]{64}', spec.get('image_id', ''))
                or spec.get('platform') != 'linux/amd64'
            ):
                raise ValueError(component + ': invalid immutable image identity or platform')
            archive = spec.get('archive', {})
            filename, digest, size = (
                archive.get('filename'),
                archive.get('sha256'),
                archive.get('size_bytes'),
            )
            if (
                not isinstance(filename, str)
                or not filename.endswith('.tar')
                or filename in ('.', '..')
                or any(character in filename for character in '/\\:\0\r\n')
            ):
                raise ValueError(component + ': unsafe or missing archive filename')
            if (
                not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or type(size) is not int
                or size <= 0
            ):
                raise ValueError(component + ': missing archive checksum or size')
            source = source_environment / filename
            if not source.is_file() or source.is_symlink():
                raise ValueError(component + ': missing required environment archive ' + filename)
            if source.stat().st_size != size:
                raise ValueError(component + ': archive size does not match its embedded manifest')
            if sha256_file(source) != digest:
                raise ValueError(
                    component + ': archive checksum does not match its embedded manifest'
                )
            archives.append((source, environment / filename, digest, size))
        allowed = {target.name for _, target, _, _ in archives} | {'environment-manifest.json'}
        if environment.is_symlink() or (
            environment.exists()
            and (
                not environment.is_dir()
                or any(
                    path.name not in allowed or not path.is_file() or path.is_symlink()
                    for path in environment.iterdir()
                )
            )
        ):
            raise ValueError(
                'Unreviewed files already exist in staged environment; use a fresh staging directory'
            )
    elif environment.exists():
        raise ValueError('An app-only developer stage must not contain an environment directory')

    for source, target in source_files:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if include_environment:
        environment.mkdir(exist_ok=True)
        for source, target, digest, size in archives:
            if (
                not target.is_file()
                or target.stat().st_size != size
                or sha256_file(target) != digest
            ):
                shutil.copy2(source, target)
            if target.stat().st_size != size or sha256_file(target) != digest:
                raise ValueError('Archive copy failed size/checksum verification: ' + target.name)
        (environment / 'environment-manifest.json').write_bytes(manifest_bytes)
    return {
        'include_environment': include_environment,
        'docs': list(CURATED_DOCS),
        'archives': [target.name for _, target, _, _ in archives],
    }


def inventory(folder):
    return [
        dict(
            path=str(p.relative_to(folder)),
            size=p.stat().st_size,
            sha256=sha256_file(p),
        )
        for p in sorted(folder.rglob('*'))
        if p.is_file()
    ]


def main(args):
    out = Path(args.output).resolve()
    if ROOT not in out.parents:
        raise ValueError('패키지 검증 폴더는 프로젝트 안에 지정하세요.')
    if args.publish:
        raise ValueError('Use scripts/release_distribution.py after native verification for all publication targets')
    identity = ROOT / 'RECHECK_SOURCE_IDENTITY.json'
    if not identity.is_file():
        raise ValueError(
            'An existing verified RECHECK_SOURCE_IDENTITY.json is required; this script does not generate one'
        )
    out.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        '-m',
        'PyInstaller',
        '--noconfirm',
        '--workpath',
        str(out / 'work'),
        '--distpath',
        str(out / 'staged-dist'),
        str(ROOT / 'Recheck.spec'),
    ]
    with (out / 'build.log').open('w', encoding='utf-8') as stream:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=300,
            env={**os.environ, 'PYTHONUTF8': '1', 'RECHECK_BUILD_IDENTITY': str(identity)},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    atomic_json(
        out / 'build.json',
        dict(
            argv=command,
            returncode=completed.returncode,
            finished=datetime.now(timezone.utc).isoformat(),
        ),
    )
    if completed.returncode:
        raise RuntimeError('Windows 패키징 실패: ' + str(out / 'build.log'))
    staged = out / 'staged-dist' / 'Recheck'
    prepare_distribution_files(staged, include_environment=bool(args.include_environment))
    fetch_sources(args.source_cache)
    collect_notices(staged, source_cache=args.source_cache)
    atomic_json(out / 'staged-inventory.json', inventory(staged))
    print(staged / 'Recheck.exe')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--publish', action='store_true')
    environment = parser.add_mutually_exclusive_group()
    environment.add_argument(
        '--app-only',
        action='store_true',
        help='Portable public build (default): omit offline environment archives',
    )
    environment.add_argument('--include-environment', action='store_true',
                             help='Explicit private/offline build: include reviewed environment archives')
    parser.add_argument('--source-cache', default=str(DEFAULT_CACHE))
    main(parser.parse_args())
