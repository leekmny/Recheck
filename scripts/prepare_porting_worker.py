"""Prepare a dedicated-VM worker from the reviewed checkout and a local Python."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1]
WORKER_MODULES = (
    '__init__.py', 'paths.py', 'process.py', 'porting_manifest.py', 'porting_worker.py',
    'porting_drivers.py', 'porting_browser.py', 'porting_desktop.py', 'porting_library.py',
    'porting_library_child.py',
)
DEPENDENCIES = ('anyio', 'certifi', 'h11', 'httpcore', 'httpx', 'idna', 'psutil', 'typing-extensions')
WORKER_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
PYTHON_FLAGS = ('-I', '-B')


def _python_command(python, *args, timeout=30, env=None):
    completed = subprocess.run([str(python), *PYTHON_FLAGS, *map(str, args)],
                               capture_output=True, check=False, timeout=timeout, env=env)
    if completed.returncode != 0:
        detail = completed.stderr.decode('utf-8', 'replace').strip()
        if not detail:
            detail = completed.stdout.decode('utf-8', 'replace').strip()
        raise ValueError(f'Python command failed ({completed.returncode}): {detail[-1000:]}')
    return completed.stdout


def _python_details(python, check_packages):
    code = (
        'import importlib.metadata, json, struct, sys; '
        'assert sys.version_info >= (3, 11), "Python 3.11 or newer is required"; '
        'assert struct.calcsize("P") == 8, "A 64-bit Python interpreter is required"; '
    )
    if 'httpx' in check_packages:
        code += 'import httpx; '
    if 'psutil' in check_packages:
        code += 'import psutil; '
    code += ('print(json.dumps(dict(executable=sys.executable, version=sys.version.split()[0], '
             f'dependencies={{name: importlib.metadata.version(name) for name in {check_packages!r}}})))')
    return json.loads(_python_command(python, '-c', code))


def _worker_requirements():
    locks = {}
    for line in (SOURCE_ROOT / 'requirements-lock.txt').read_text(encoding='utf-8').splitlines():
        name, separator, value = line.partition('==')
        if separator:
            locks[name.lower().replace('_', '-')] = value
    if any(name not in locks or not re.fullmatch(r'[0-9][A-Za-z0-9.]*', locks[name])
           for name in DEPENDENCIES):
        raise ValueError('The checkout must contain exact pins for the worker dependency set.')
    return [f'{name}=={locks[name]}' for name in DEPENDENCIES]


def _create_environment(python, directory, wheelhouse, requirements):
    _python_command(python, '-m', 'venv', directory, timeout=120)
    interpreter = directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('PIP_')}
    env['PIP_CONFIG_FILE'] = os.devnull
    _python_command(interpreter, '-m', 'pip', '--disable-pip-version-check', 'install', '--no-index',
                    '--find-links', wheelhouse, '--only-binary=:all:', '--no-deps', '--no-cache-dir',
                    *requirements, env=env, timeout=120)
    return interpreter


def _entry_script(worker_id):
    return (
        'import os\nimport sys\nfrom pathlib import Path\n\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
        'from recheck.porting_worker import main\n\n'
        f'os.environ["RECHECK_WORKER_ID"] = {worker_id!r}\n'
        'os.environ["RECHECK_WORKER_ISOLATION"] = "dedicated-vm"\n'
        'raise SystemExit(main())\n'
    ).encode('utf-8')


def _launcher(python, entry):
    if os.name == 'nt':
        def literal(path):
            return '"' + str(path).replace('%', '%%') + '"'

        return 'recheck-porting-worker.cmd', (
            '@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
            '"%SystemRoot%\\System32\\chcp.com" 65001 >nul 2>&1\r\n'
            f'{literal(python)} -I -B {literal(entry)}\r\n'
            'exit /b %ERRORLEVEL%\r\n'
        ).encode('utf-8')
    return 'recheck-porting-worker', (
        f'#!/bin/sh\nexec {shlex.quote(str(python))} -I -B {shlex.quote(str(entry))}\n'
    ).encode('utf-8')


def _absolute_path(value):
    if any(character in str(value) for character in ('\0', '\n', '\r')):
        raise ValueError('Paths cannot contain NUL or line breaks.')
    # The venv's python may be a symlink to the base interpreter. Keep its venv path.
    return Path(os.path.abspath(value))


def prepare(destination, worker_id, python, wheelhouse=None):
    if not WORKER_ID.fullmatch(worker_id):
        raise ValueError('Invalid worker ID (1–64 letters, digits, dots, underscores or dashes).')
    destination = _absolute_path(destination)
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.exists() or destination.is_symlink():
        raise ValueError(f'Destination already exists: {destination}')
    python = _absolute_path(python)
    if not python.is_file() or (os.name == 'nt' and python.suffix.lower() != '.exe'):
        raise ValueError('Select an existing Python executable.')
    package_names = ('httpx', 'psutil') if os.name == 'nt' else ('httpx',)
    if wheelhouse is not None:
        wheelhouse = _absolute_path(wheelhouse).resolve(strict=True)
        if not wheelhouse.is_dir() or not any(path.is_file() for path in wheelhouse.glob('*.whl')):
            raise ValueError('Select a local wheelhouse containing reviewed Python wheels.')
        requirements = _worker_requirements()
        _python_details(python, ())
    else:
        details = _python_details(python, package_names)
    sources = {f'recheck/{name}': (SOURCE_ROOT / 'recheck' / name).read_bytes() for name in WORKER_MODULES}
    sources['LICENSE'] = (SOURCE_ROOT / 'LICENSE').read_bytes()
    sources['worker-entry.py'] = _entry_script(worker_id)

    destination.mkdir(mode=0o755)
    created = destination.stat()
    try:
        if wheelhouse is not None:
            python = _create_environment(python, destination / '.venv', wheelhouse, requirements)
            details = _python_details(python, DEPENDENCIES)
        name, launcher = _launcher(python, destination / 'worker-entry.py')
        sources[name] = launcher
        (destination / 'recheck').mkdir()
        for filename, data in sources.items():
            with (destination / filename).open('xb') as output:
                output.write(data)
        if os.name != 'nt':
            (destination / name).chmod(0o755)
        receipt = dict(schema_version='recheck-porting-worker-deployment-1', worker_id=worker_id,
                       isolation='dedicated-vm', launcher=name, python=details,
                       fixed_argv=[str(python), *PYTHON_FLAGS, str(destination / 'worker-entry.py')],
                       dependency_source='wheelhouse' if wheelhouse is not None else 'preconfigured',
                       files={filename: hashlib.sha256(data).hexdigest() for filename, data in sources.items()})
        with (destination / 'worker-deployment.json').open('x', encoding='utf-8') as output:
            output.write(json.dumps(receipt, indent=2) + '\n')
        return receipt
    except (Exception, KeyboardInterrupt):
        if destination.resolve() == destination and os.path.samestat(created, destination.stat()):
            shutil.rmtree(destination)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True, help='New deployment directory')
    parser.add_argument('--worker-id', required=True, help='ID used in the controller worker config')
    parser.add_argument('--python', type=Path, default=Path(sys.executable),
                        help='Local Python executable with worker dependencies')
    parser.add_argument('--wheelhouse', type=Path,
                        help='Explicitly create an internal venv with reviewed local wheels (offline)')
    args = parser.parse_args(argv)
    try:
        receipt = prepare(args.destination, args.worker_id, args.python, args.wheelhouse)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f'{exc}\n')
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
