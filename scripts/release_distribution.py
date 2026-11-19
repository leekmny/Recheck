"""Publish one tested payload to all launch locations with recoverable swaps."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recheck.models import atomic_json
from scripts.collect_distribution_notices import verify_notices


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(folder):
    folder = Path(folder).absolute()
    rows = []
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('Linked paths are not admitted: ' + str(path))
        if path.is_file():
            rows.append(dict(path=path.relative_to(folder).as_posix(),
                             size=path.stat().st_size, sha256=sha(path)))
    return rows


def verify_outputs(targets, archive, expected):
    for target in targets:
        if inventory(target) != expected:
            raise ValueError('Distribution inventory differs: ' + str(target))
    with zipfile.ZipFile(archive) as stream:
        actual = []
        for member in stream.infolist():
            if member.is_dir():
                continue
            if not member.filename.startswith('Recheck/'):
                raise ValueError('ZIP has an invalid payload root')
            with stream.open(member) as payload:
                digest = hashlib.file_digest(payload, 'sha256').hexdigest()
            actual.append(dict(path=member.filename[len('Recheck/'):],
                               size=member.file_size, sha256=digest))
        if sorted(actual, key=lambda row: row['path']) != sorted(expected, key=lambda row: row['path']):
            raise ValueError('ZIP inventory differs from the tested payload')
    return True


def write_archive(stage, archive, expected):
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as stream:
        for row in expected:
            stream.write(stage / row['path'], 'Recheck/' + row['path'],
                         compress_type=zipfile.ZIP_STORED if row['path'].endswith('.tar')
                         else zipfile.ZIP_DEFLATED)
    verify_outputs([], archive, expected)


def recover(record):
    """Roll back interrupted publication without deleting any release folder."""
    record = Path(record).resolve(strict=True)
    journal = json.loads((record / 'journal.json').read_text(encoding='utf-8'))
    if journal['state'] in ('committed', 'rolled_back'):
        for row in journal['targets']:
            lock = Path(row['target']).parent / '.recheck-publication.lock'
            if lock.is_file() and lock.read_text(encoding='utf-8') == str(record):
                lock.unlink()
        return journal
    # Preflight every path before mutating any folder, including later rows.
    for row in journal['targets']:
        target, backup, incoming = (Path(row[key]) for key in ('target', 'backup', 'incoming'))
        if (not target.is_absolute() or target.parent.resolve() != target.parent
                or backup.parent != target.parent or incoming.parent != target.parent
                or backup.name != '.recheck-previous-' + journal['id']
                or incoming.name != '.recheck-incoming-' + journal['id']
                or any(p.is_symlink() or p.is_junction() for p in (target, backup, incoming))):
            raise ValueError('Invalid recovery paths; no folders moved')
        if (journal['state'] == 'applying' and row['existed'] and not backup.exists()
                and (not target.exists() or inventory(target) != row['previous_inventory'])):
            raise ValueError('Previous release backup is missing; manual recovery required')
        if backup.exists() and inventory(backup) != row['previous_inventory']:
            raise ValueError('Previous release backup changed; manual recovery required')
    for index, row in reversed(list(enumerate(journal['targets']))):
        target, backup, incoming = (Path(row[key]) for key in ('target', 'backup', 'incoming'))
        if backup.exists():
            if target.exists():
                target.rename(record / f'failed-current-{index}')
            backup.rename(target)
        elif (journal['state'] == 'applying' and not row['existed']
              and target.exists() and not incoming.exists()):
            target.rename(record / f'failed-current-{index}')
        if incoming.exists():
            incoming.rename(record / f'uninstalled-{index}')
    journal['state'] = 'rolled_back'
    atomic_json(record / 'journal.json', journal)
    for row in journal['targets']:
        lock = Path(row['target']).parent / '.recheck-publication.lock'
        if lock.is_file() and lock.read_text(encoding='utf-8') == str(record):
            lock.unlink()
    return journal


def publish(stage, proof, targets, record):
    stage = Path(stage).resolve(strict=True)
    proof = json.loads(Path(proof).read_text(encoding='utf-8'))
    allowed = {'Recheck.exe', '_internal', 'environment', 'docs', 'README.md',
               'requirements-lock.txt', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
               'licenses', 'third-party-sources'}
    if any(path.name not in allowed for path in stage.iterdir()):
        raise ValueError('Unreviewed files in staging; use a fresh build')
    verify_notices(stage)
    expected = inventory(stage)
    embedded = json.loads((stage / '_internal/RECHECK_SOURCE_IDENTITY.json').read_text(encoding='utf-8'))
    if (proof.get('status') != 'verified' or proof.get('inventory') != expected
            or proof.get('source_aggregate') != embedded.get('aggregate_sha256')
            or not all(proof.get('checks', {}).get(key) is True
                       for key in ('native_smoke', 'frozen_dynamic', 'literal_observations'))):
        raise ValueError('Native verification missing or payload changed after verification')
    record = Path(record).resolve()
    resolved = []
    for value in targets:
        path = Path(value).absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        if (path.resolve() != path or path == stage or path in stage.parents
                or stage in path.parents or path == record or path in record.parents
                or record in path.parents or path.is_symlink() or path.is_junction()):
            raise ValueError('Unsafe publication target: ' + str(path))
        if any(path == other or path in other.parents or other in path.parents for other in resolved):
            raise ValueError('Publication targets overlap')
        if any(path.parent == other.parent for other in resolved):
            raise ValueError('Each publication target must have a distinct parent')
        if path.drive != record.drive:
            raise ValueError('Recovery record and targets must use the same volume')
        resolved.append(path)
    if not resolved:
        raise ValueError('At least one publication target is required')
    record.mkdir(parents=True, exist_ok=False)
    transaction = uuid.uuid4().hex
    rows = [dict(target=str(path),
                 backup=str(path.parent / ('.recheck-previous-' + transaction)),
                 incoming=str(path.parent / ('.recheck-incoming-' + transaction)),
                 existed=path.exists(), previous_inventory=inventory(path) if path.exists() else [])
            for path in resolved]
    journal = dict(id=transaction, state='preparing', targets=rows, inventory=expected)
    atomic_json(record / 'journal.json', journal)
    archive = record / 'Recheck-Windows-x64.zip'
    locks = []
    try:
        for parent in sorted({path.parent for path in resolved}):
            lock = parent / '.recheck-publication.lock'
            with lock.open('x', encoding='utf-8') as stream:
                stream.write(str(record))
            locks.append(lock)
        write_archive(stage, archive, expected)
        for row in rows:
            shutil.copytree(stage, row['incoming'])
            if inventory(row['incoming']) != expected:
                raise ValueError('Incoming copy failed verification; old release preserved')
        journal['state'] = 'applying'
        atomic_json(record / 'journal.json', journal)
        for row in rows:
            target, backup, incoming = (Path(row[key]) for key in ('target', 'backup', 'incoming'))
            if row['existed']:
                if inventory(target) != row['previous_inventory']:
                    raise ValueError('Previous release changed during publication')
                target.rename(backup)
            incoming.rename(target)
        verify_outputs(resolved, archive, expected)
        result = dict(targets=rows, inventory=expected, zip=str(archive), zip_sha256=sha(archive),
                      executable_sha256=sha(stage / 'Recheck.exe'),
                      source_aggregate=embedded['aggregate_sha256'])
        atomic_json(record / 'publication.json', result)
        journal['state'] = 'committed'
        atomic_json(record / 'journal.json', journal)
        return result
    except Exception:
        recover(record)
        raise
    finally:
        for lock in locks:
            if lock.is_file() and lock.read_text(encoding='utf-8') == str(record):
                lock.unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage')
    parser.add_argument('--proof')
    parser.add_argument('--target', action='append', default=[])
    parser.add_argument('--record', required=True)
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    result = recover(args.record) if args.recover else publish(args.stage, args.proof, args.target, args.record)
    print(json.dumps(result, ensure_ascii=False, indent=2))
