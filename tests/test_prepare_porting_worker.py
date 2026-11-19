from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from recheck.porting_worker import worker_platform


BOOTSTRAP = Path(__file__).resolve().parents[1] / 'scripts' / 'prepare_porting_worker.py'


def prepare(destination, *options, worker_id='guest-01'):
    return subprocess.run(
        [sys.executable, '-I', '-B', str(BOOTSTRAP), '--destination', str(destination),
         '--python', sys.executable, '--worker-id', worker_id, *options],
        cwd=destination.parent, capture_output=True, timeout=45,
    )


def test_prepares_minimal_worker_with_a_fixed_loader_and_verified_files(tmp_path):
    destination = tmp_path / '워커 배포'
    completed = prepare(destination)

    assert completed.returncode == 0, completed.stderr.decode('utf-8', 'replace')
    receipt = json.loads((destination / 'worker-deployment.json').read_text(encoding='utf-8'))
    assert receipt['worker_id'] == 'guest-01'
    assert receipt['isolation'] == 'dedicated-vm'
    assert receipt['python']['executable'] == sys.executable
    required_packages = {'httpx', 'psutil'} if os.name == 'nt' else {'httpx'}
    assert required_packages <= set(receipt['python']['dependencies'])
    assert set(receipt['files']) >= {'worker-entry.py', 'recheck/porting_worker.py'}
    assert not (destination / 'recheck' / 'gui.py').exists()
    assert not (destination / 'recheck' / 'porting_transport.py').exists()
    for name, digest in receipt['files'].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest

    unrelated = tmp_path / 'other directory'
    untrusted = unrelated / 'recheck'
    untrusted.mkdir(parents=True)
    (untrusted / '__init__.py').write_text('raise Exception("cwd packages must be ignored")')
    environment = os.environ | {
        'PYTHONPATH': str(unrelated),
        'RECHECK_WORKER_ID': 'wrong-id',
        'RECHECK_WORKER_ISOLATION': 'docker',
    }
    body = json.dumps({'op': 'probe', 'protocol': 'recheck-porting-worker-1', 'job_id': 'probe-01'}).encode()
    frame = len(body).to_bytes(4, 'big') + body
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    response = subprocess.run([str(destination / receipt['launcher'])], cwd=unrelated,
                              env=environment, input=frame, capture_output=True, timeout=15,
                              creationflags=creation_flags)
    assert response.returncode == 0, response.stderr.decode('utf-8', 'replace')
    data = json.loads(response.stdout)
    assert data['job_id'] == 'probe-01'
    assert data['status'] == 'ready'
    for name, value in {'id': 'guest-01', 'isolation': 'dedicated-vm', **worker_platform()}.items():
        assert data['environment'][name] == value
    assert not list(destination.rglob('__pycache__'))


def test_never_overwrites_an_existing_worker_directory(tmp_path):
    destination = tmp_path / 'active'
    destination.mkdir()
    sentinel = destination / 'keep.txt'
    sentinel.write_bytes(b'operator data')

    completed = prepare(destination)

    assert completed.returncode != 0
    assert b'exists' in completed.stderr
    assert list(destination.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b'operator data'


def test_rejects_invalid_worker_id_without_creating_a_deployment(tmp_path):
    destination = tmp_path / 'bad-id'

    completed = prepare(destination, worker_id='bad key\nworker-id')

    assert completed.returncode != 0
    assert b'worker ID' in completed.stderr
    assert not destination.exists()


def test_optional_offline_install_requires_wheels_without_mutating_destination(tmp_path):
    wheelhouse = tmp_path / 'empty wheelhouse'
    wheelhouse.mkdir()
    destination = tmp_path / 'new worker'

    completed = prepare(destination, '--wheelhouse', str(wheelhouse))

    assert completed.returncode != 0
    assert b'wheelhouse' in completed.stderr
    assert not destination.exists()
