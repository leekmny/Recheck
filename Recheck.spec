# Standalone Recheck build; no Rebuild or target-worker dependency.
from pathlib import Path
import hashlib
import json
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from scripts.collect_distribution_notices import (
    pin_cpython_ssl_binaries, reviewed_binary_entry, reviewed_qt_binary,
)

root = Path(SPECPATH)
identity_env = os.environ.get('RECHECK_BUILD_IDENTITY', '')
identity_path = Path(identity_env) if identity_env else (root / 'RECHECK_SOURCE_IDENTITY.json')

if os.environ.get('RECHECK_STRICT_IDENTITY', '1') == '1' and identity_path.is_file():
    identity = json.loads(identity_path.read_text(encoding='utf-8'))['runtime_identity']
    for name, expected in identity.items():
        candidate = (root / name).resolve(strict=True)
        if (
            not candidate.is_relative_to(root.resolve())
            or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected
        ):
            raise RuntimeError('Verified Recheck source changed before packaging: ' + name)

datas = [
    (str(root / 'resources'), 'resources'),
    (str(root / 'requirements-lock.txt'), '.'),
]
if identity_path.is_file():
    datas.append((str(identity_path), '.'))

for package in ('jsonschema', 'jsonschema_specifications', 'referencing', 'cryptography'):
    datas += collect_data_files(package)

for distribution in (
    'pydantic',
    'PyJWT',
    'cryptography',
    'jsonschema',
    'jsonschema-specifications',
    'referencing',
    'rpds-py',
):
    datas += copy_metadata(distribution)

hidden = [
    'recheck.cli',
    'recheck.agent',
    'recheck.provider_ui',
    'recheck.web_login',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtWebChannel',
    'PySide6.QtPrintSupport',
    'PySide6.QtQuickWidgets',
    'PySide6.QtNetwork',
    'jwt',
    'cryptography.hazmat.bindings._rust',
    'jsonschema_specifications',
    'referencing.jsonschema',
]
hidden += collect_submodules('ai_providers')

a = Analysis(
    [str(root / 'launch.py')],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hidden)),
    excludes=[
        'tkinter',
    ],
    noarchive=False,
)

# Keep the proven WebEngine dependency closure, its helper/resources/locales,
# and Windows Schannel TLS. Unrelated Addons, QML imports and Mesa are excluded.
a.binaries = [row for row in a.binaries if reviewed_binary_entry(row)]
# Analysis deduplicates DLL basenames before this filter. QtNetwork may have
# selected an unrelated host OpenSSL first; restore Python's exact TLS imports.
a.binaries = pin_cpython_ssl_binaries(a.binaries)
a.datas = [row for row in a.datas if reviewed_qt_binary(row[0])]

pyz = PYZ(a.pure)

is_onefile = (
    os.environ.get('RECHECK_BUNDLE_MODE', '').lower() == 'onefile'
    or os.environ.get('RECHECK_ONEFILE', '0') == '1'
)

version_file = str(root / 'resources/version_info.txt') if (root / 'resources/version_info.txt').is_file() else None

if is_onefile:
    # 단일 onefile 실행 파일 빌드:
    # a.binaries, a.zipfiles, a.datas를 모두 EXE에 직접 묶고 COLLECT는 생략하여 _internal 없이 단일 파일로 패키징
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='Recheck',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        version=version_file,
    )
else:
    # 공식 onedir 배포 폴더 빌드 (기존 배포 및 package_verified.py 호환)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='Recheck',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        version=version_file,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Recheck')
