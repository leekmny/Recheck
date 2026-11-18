"""Collect exact dependency notices and corresponding Qt sources for a portable build.

No installed paths are written to the distributable manifest. Archive members are
read as data; no archive is extracted with extractall and no downloaded code runs.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / 'resources/distribution/notice-sources.json'
DEFAULT_CACHE = ROOT / 'verification/portable-prerequisites-20261116/packaging/source-cache'
REVIEWED_QT_LIBRARIES = frozenset((
    'qt6core.dll', 'qt6gui.dll', 'qt6widgets.dll', 'qt6svg.dll',
    'qt6network.dll', 'qt6webenginecore.dll', 'qt6webenginewidgets.dll',
    'qt6positioning.dll', 'qt6qml.dll', 'qt6quick.dll', 'qt6webchannel.dll',
    'qt6printsupport.dll', 'qt6quickwidgets.dll', 'qt6opengl.dll',
    'qt6qmlmeta.dll', 'qt6qmlmodels.dll', 'qt6qmlworkerscript.dll',
))
REVIEWED_QT_EXTENSIONS = frozenset((
    'qtcore.pyd', 'qtgui.pyd', 'qtwidgets.pyd', 'qtsvg.pyd',
    'qtnetwork.pyd', 'qtwebenginecore.pyd', 'qtwebenginewidgets.pyd',
    'qtpositioning.pyd', 'qtwebchannel.pyd', 'qtprintsupport.pyd',
    'qtquickwidgets.pyd', 'qtopengl.pyd',
))
REVIEWED_QT_PLUGINS = frozenset((
    'platforms/qwindows.dll', 'platforms/qoffscreen.dll', 'platforms/qminimal.dll',
    'styles/qmodernwindowsstyle.dll', 'iconengines/qsvgicon.dll',
    'imageformats/qsvg.dll', 'imageformats/qgif.dll', 'imageformats/qico.dll',
    'imageformats/qjpeg.dll',
    'tls/qschannelbackend.dll', 'tls/qopensslbackend.dll', 'tls/qcertonlybackend.dll',
    'webview/qtwebview_webengine.dll',
))
REVIEWED_WEBENGINE_RESOURCES = frozenset((
    'icudtl.dat', 'qtwebengine_resources.pak', 'qtwebengine_resources_100p.pak',
    'qtwebengine_resources_200p.pak', 'qtwebengine_devtools_resources.pak',
    'v8_context_snapshot.bin',
))
REVIEWED_WEBENGINE_LOCALES = frozenset(('en-us.pak', 'ko.pak'))
TLS_LIBRARY_NAME = re.compile(r'(?:lib(?:ssl|crypto)[-_].*|libeay32|ssleay32)\.dll')


def reviewed_qt_binary(filename):
    """Admit the reviewed Widgets/WebEngine closure, never all installed Addons."""
    filename = str(filename).replace('\\', '/').casefold()
    basename = filename.rsplit('/', 1)[-1]
    if basename == 'opengl32sw.dll':
        return False
    if basename.startswith('qt6') and basename.endswith('.dll'):
        return basename in REVIEWED_QT_LIBRARIES
    if filename.startswith('pyside6/plugins/'):
        return filename.removeprefix('pyside6/plugins/') in REVIEWED_QT_PLUGINS
    if filename.startswith('pyside6/resources/'):
        return filename.removeprefix('pyside6/resources/') in REVIEWED_WEBENGINE_RESOURCES
    if filename.startswith('pyside6/translations/'):
        return filename.removeprefix('pyside6/translations/qtwebengine_locales/') in REVIEWED_WEBENGINE_LOCALES
    if filename.startswith('pyside6/qml/'):
        return False  # QWebEngineView uses Widgets; no QML imports are shipped.
    if filename.startswith('pyside6/') and basename.startswith('qt') and basename.endswith('.pyd'):
        return basename in REVIEWED_QT_EXTENSIONS
    if 'qtwebengine' in filename or basename == 'qtwebengineprocess.exe':
        return True
    return True


def reviewed_binary_entry(row, python_prefix=None):
    """Keep Python's own TLS DLLs; never collect Qt TLS from another host app."""
    filename, source = row[:2]
    basename = Path(str(filename).replace('\\', '/')).name.casefold()
    if TLS_LIBRARY_NAME.fullmatch(basename):
        prefix = Path(python_prefix or sys.base_prefix).resolve()
        candidate = Path(source).resolve()
        if candidate.parent not in (prefix, prefix / 'DLLs'):
            return False
    return reviewed_qt_binary(filename)


def cpython_ssl_binaries(python_prefix=None):
    """Resolve _ssl's actual PE TLS imports exclusively from this CPython build."""
    import pefile

    prefix = Path(python_prefix or sys.base_prefix).resolve(strict=True)
    folders = (prefix / 'DLLs', prefix)
    ssl_module = next((folder / '_ssl.pyd' for folder in folders
                       if (folder / '_ssl.pyd').is_file()), None)
    if (ssl_module is None or ssl_module.is_symlink()
            or ssl_module.resolve().parent not in folders):
        raise ValueError('Missing native CPython _ssl.pyd')
    with pefile.PE(str(ssl_module), fast_load=True) as binary:
        binary.parse_data_directories(directories=[1, 13])
        names = sorted({entry.dll.decode('ascii')
                        for table in ('DIRECTORY_ENTRY_IMPORT', 'DIRECTORY_ENTRY_DELAY_IMPORT')
                        for entry in getattr(binary, table, [])
                        if TLS_LIBRARY_NAME.fullmatch(entry.dll.decode('ascii').casefold())})
    if not names:
        raise ValueError('CPython _ssl.pyd has no reviewed dynamic TLS dependencies')
    entries = [('_ssl.pyd', str(ssl_module), 'EXTENSION')]
    for name in names:
        source = next((folder / name for folder in folders if (folder / name).is_file()), None)
        if (source is None or source.is_symlink()
                or source.resolve().parent not in folders):
            raise ValueError('Missing native CPython TLS dependency: ' + name)
        entries.append((name, str(source), 'BINARY'))
    return entries


def pin_cpython_ssl_binaries(entries, python_prefix=None):
    """Replace deduplicated host collisions instead of dropping _ssl's DLLs."""
    selected = []
    for row in entries:
        basename = Path(str(row[0]).replace('\\', '/')).name.casefold()
        if basename != '_ssl.pyd' and not TLS_LIBRARY_NAME.fullmatch(basename):
            selected.append(row)
    return selected + cpython_ssl_binaries(python_prefix)


def verify_cpython_ssl_runtime(internal, expected):
    for filename, expected_hash in expected.items():
        path = Path(internal) / Path(safe_relative(filename))
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing CPython TLS runtime file: ' + filename)
        if not re.fullmatch(r'[0-9a-f]{64}', expected_hash) or digest(path) != expected_hash:
            raise ValueError('CPython TLS runtime checksum differs: ' + filename)


def verify_qt_runtime(internal, manifest):
    """Fail on omitted browser runtime files or files outside reviewed Qt scope."""
    internal = Path(internal)
    for path in internal.rglob('*'):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Linked runtime paths are not admitted')
        if path.is_file() and not reviewed_qt_binary(path.relative_to(internal).as_posix()):
            raise ValueError('Unreviewed optional renderer/Qt component scope: ' + path.name)
    runtime = manifest.get('qt_runtime', {})
    sources = {row['component'] for row in manifest.get('sources', [])}
    for filename, component in runtime.get('files', {}).items():
        relative = safe_relative(filename)
        if component not in sources:
            raise ValueError('Runtime component has no corresponding source: ' + component)
        path = internal / Path(relative)
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise ValueError('Missing required Qt runtime file: ' + filename)
    for filename, expected in runtime.get('sha256', {}).items():
        path = internal / Path(safe_relative(filename))
        if not path.is_file() or not re.fullmatch(r'[0-9a-f]{64}', expected) or digest(path) != expected:
            raise ValueError('Qt runtime build checksum differs: ' + filename)
    return runtime


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_relative(value):
    path = PurePosixPath(str(value).replace('\\', '/'))
    if path.is_absolute() or any(part in ('..', '') for part in path.parts) or ':' in str(path):
        raise ValueError('Unsafe source/member path: ' + str(value))
    return path


def _notice_name(name):
    name = name.casefold()
    return any(term in name for term in ('license', 'copying', 'notice', 'copyright', 'patent')) or name in (
        'authors', 'authors.txt', 'qt_attributions.json', 'copyrights', 'legal', 'legal.txt',
    )


def _source_rows(manifest, cache):
    if manifest.get('version') != 1 or not manifest.get('sources'):
        raise ValueError('Missing corresponding-source manifest')
    rows = []
    seen = set()
    for row in manifest['sources']:
        filename = safe_relative(row.get('filename', ''))
        if len(filename.parts) != 1 or filename.name in seen:
            raise ValueError('Unsafe or duplicate source filename')
        seen.add(filename.name)
        if not re.fullmatch(r'[a-z0-9-]+', row.get('component', '')):
            raise ValueError('Unsafe source component path')
        if not re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', '')):
            raise ValueError('Missing source checksum')
        if type(row.get('size_bytes')) is not int or row['size_bytes'] <= 0:
            raise ValueError('Missing source size')
        source = cache / filename.name
        if source.is_symlink() or not source.is_file():
            raise ValueError('Missing source archive: ' + filename.name)
        if source.stat().st_size != row['size_bytes'] or digest(source) != row['sha256']:
            raise ValueError('Source archive size/checksum mismatch: ' + filename.name)
        notices = []
        with tarfile.open(source, 'r:*') as stream:
            for member in stream:
                relative = safe_relative(member.name)
                third_party_readme = relative.name.casefold().startswith('readme') and any(
                    part.casefold() in ('3rdparty', 'third_party', 'vendor', 'external') for part in relative.parts
                )
                if not _notice_name(relative.name) and 'LICENSES' not in relative.parts and not third_party_readme:
                    continue
                if not member.isfile() or member.size > 8 * 1024 * 1024:
                    if member.isdir():
                        continue
                    raise ValueError('Unsafe license archive member: ' + member.name)
                notices.append((relative, stream.extractfile(member).read()))
        if not notices:
            raise ValueError('No notices found in source archive: ' + filename.name)
        rows.append((row, source, notices))
    return rows


def fetch_sources(cache=DEFAULT_CACHE, manifest_path=DEFAULT_MANIFEST):
    """Download pinned archives only, with bounded size and checksum validation."""
    cache = Path(cache).resolve()
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    cache.mkdir(parents=True, exist_ok=True)
    for row in manifest['sources']:
        filename = safe_relative(row['filename'])
        if len(filename.parts) != 1 or not row['url'].startswith('https://'):
            raise ValueError('Unsafe source filename/URL')
        target = cache / filename.name
        if target.is_file() and target.stat().st_size == row['size_bytes'] and digest(target) == row['sha256']:
            continue
        if target.exists():
            raise ValueError('Existing source cache entry has a wrong checksum: ' + filename.name)
        partial = target.with_name(target.name + '.partial')
        count = 0
        partial_created = False
        try:
            with urllib.request.urlopen(row['url'], timeout=45) as response, partial.open('xb') as out:
                partial_created = True
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > row['size_bytes']:
                        raise ValueError('Downloaded source exceeds pinned size')
                    out.write(chunk)
            if count != row['size_bytes'] or digest(partial) != row['sha256']:
                raise ValueError('Downloaded source size/checksum mismatch')
            partial.rename(target)
        finally:
            if partial_created and partial.exists():
                partial.unlink()
    _source_rows(manifest, cache)
    return cache


def collect_notices(staged, *, source_cache=DEFAULT_CACHE, root=ROOT,
                    manifest_path=DEFAULT_MANIFEST, python_prefix=None, distributions=None):
    staged, root, cache = Path(staged).resolve(strict=True), Path(root).resolve(), Path(source_cache).resolve()
    if staged == root or not (staged / 'Recheck.exe').is_file():
        raise ValueError('A separate staged Recheck executable is required')
    if (staged / 'licenses').exists() or (staged / 'third-party-sources').exists():
        raise ValueError('Use a fresh staging folder; existing license/source folders are not merged')
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    runtime = verify_qt_runtime(staged / '_internal', manifest)
    cpython_ssl = {}
    if manifest.get('build_versions', {}).get('CPython'):
        cpython_ssl = {name: digest(source) for name, source, _ in
                       cpython_ssl_binaries(python_prefix)}
        verify_cpython_ssl_runtime(staged / '_internal', cpython_ssl)
    sources = _source_rows(manifest, cache)
    prefix = Path(python_prefix) if python_prefix is not None else Path(sys.base_prefix)
    python_license = prefix / 'LICENSE.txt'
    if not python_license.is_file():
        raise ValueError('Missing original CPython license')
    if distributions is None:
        names = [line.split('==')[0] for line in (root / 'requirements-lock.txt').read_text().splitlines()
                 if line.strip() and not line.startswith('#')]
        distributions = [importlib.metadata.distribution(name) for name in names]
    packages = []
    copies = [(python_license, Path('licenses/CPython/LICENSE.txt'))]
    for dist in distributions:
        name, version = dist.metadata['Name'], dist.version
        component = re.sub(r'[-_.]+', '-', name).casefold() + '-' + version
        if not re.fullmatch(r'[a-z0-9.+-]+', component):
            raise ValueError('Unsafe package notice path')
        files = []
        for value in dist.files or []:
            if not _notice_name(PurePosixPath(str(value)).name):
                continue
            relative = safe_relative(value)
            if _notice_name(relative.name):
                source = Path(dist.locate_file(value))
                if source.is_symlink() or not source.is_file():
                    raise ValueError('Missing original package license: ' + name)
                destination = Path('licenses/packages') / component / Path(relative)
                copies.append((source, destination))
                files.append(destination.as_posix())
        if not files:
            raise ValueError('No original license/notice found for package: ' + name)
        packages.append(dict(name=name, version=version, files=files))
    required_versions = manifest.get('build_versions', {})
    actual_versions = {package['name'].casefold(): package['version'] for package in packages}
    actual_versions['cpython'] = '.'.join(map(str, sys.version_info[:3]))
    for name, version in required_versions.items():
        if actual_versions.get(name.casefold()) != version:
            raise ValueError('Source/build component version differs: ' + name)
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        source = root / name
        if not source.is_file():
            raise ValueError('Missing reviewed distribution notice: ' + name)
        copies.append((source, Path(name)))
    copies.append((root / 'docs/BINARY_DISTRIBUTION.md', Path('licenses/REPLACEMENT.md')))
    notice_root = root / 'resources/distribution/notices'
    for row in manifest.get('notice_files', []):
        relative = safe_relative(row['filename'])
        source = notice_root / Path(relative)
        if not source.is_file() or digest(source) != row['sha256']:
            raise ValueError('Original extra notice missing/checksum mismatch: ' + str(relative))
        copies.append((source, Path('licenses/upstream') / Path(relative)))
    for source, destination in copies:
        if not source.is_file():
            raise ValueError('Missing notice: ' + str(destination))
    for source, destination in copies:
        target = staged / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for row, source, notices in sources:
        target = staged / 'third-party-sources' / row['filename']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        for relative, payload in notices:
            target = staged / 'licenses' / row['component'] / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
    files = [dict(path=path.relative_to(staged).as_posix(), size_bytes=path.stat().st_size,
                  sha256=digest(path)) for folder in ('licenses', 'third-party-sources')
             for path in sorted((staged / folder).rglob('*')) if path.is_file()]
    files += [dict(path=name, size_bytes=(staged / name).stat().st_size,
                   sha256=digest(staged / name)) for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md')]
    record = dict(version=1, cpython_version=actual_versions['cpython'], packages=packages,
                  sources=manifest['sources'], files=files,
                  qt_runtime=runtime,
                  cpython_ssl=cpython_ssl,
                  library_replacement='REPLACEMENT.md',
                  review_scope='portable-application-only',
                  bundled_environment_images=(staged / 'environment').exists())
    (staged / 'licenses/manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    verify_notices(staged)
    return record


def verify_notices(staged):
    staged = Path(staged).resolve(strict=True)
    marker = staged / 'licenses/manifest.json'
    if not marker.is_file():
        raise ValueError('Missing distribution license/source inventory')
    record = json.loads(marker.read_text(encoding='utf-8'))
    if record.get('version') != 1 or not record.get('files') or not record.get('sources'):
        raise ValueError('Missing license/source inventory')
    verify_qt_runtime(staged / '_internal', record)
    verify_cpython_ssl_runtime(staged / '_internal', record.get('cpython_ssl', {}))
    expected = set()
    for row in record['files']:
        relative = safe_relative(row['path'])
        if relative.parts[0] not in ('licenses', 'third-party-sources') and relative.as_posix() not in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            raise ValueError('Unsafe license inventory path')
        path = staged / Path(relative)
        expected.add(relative.as_posix())
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing license/source inventory file: ' + str(relative))
        if path.stat().st_size != row['size_bytes'] or digest(path) != row['sha256']:
            raise ValueError('License/source inventory checksum mismatch: ' + str(relative))
    actual = {path.relative_to(staged).as_posix() for folder in ('licenses', 'third-party-sources')
              for path in (staged / folder).rglob('*') if path.is_file() and path != marker}
    actual.update(name for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md') if (staged / name).is_file())
    if actual != expected:
        raise ValueError('License/source inventory differs')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage')
    parser.add_argument('--source-cache', default=str(DEFAULT_CACHE))
    parser.add_argument('--fetch-sources', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if args.fetch_sources:
        fetch_sources(args.source_cache)
    if args.stage:
        if args.verify_only:
            verify_notices(args.stage)
        else:
            collect_notices(args.stage, source_cache=args.source_cache)
        print('Distribution notices and corresponding-source checks passed.')
