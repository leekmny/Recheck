import os
import json
import hashlib
import shutil
import subprocess
import zipfile

import pytest

from recheck.porting_worker import _command


def _write(root, filename, data=b'fixture'):
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _archive(path, files):
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _java(root, entry, paths):
    return _command(
        {'runtime': 'java', 'java_main': 'fixture.Main', 'java_classpath': paths},
        entry, root, root,
    )


@pytest.mark.skipif(not shutil.which('java'), reason='Java runtime is unavailable')
def test_classpath_main_must_identify_the_declared_entry(tmp_path):
    entry = _write(tmp_path, 'classes/fixture/Unrelated.class')
    _write(tmp_path, 'classes/fixture/Main.class')

    with pytest.raises(ValueError):
        _java(tmp_path, entry, ['classes'])


@pytest.mark.skipif(not shutil.which('java'), reason='Java runtime is unavailable')
@pytest.mark.parametrize('shadow', ['directory', 'jar'])
def test_earlier_classpath_provider_cannot_shadow_entry(tmp_path, shadow):
    entry = _write(tmp_path, 'classes/fixture/Main.class')
    if shadow == 'directory':
        _write(tmp_path, 'earlier/fixture/Main.class')
        previous = 'earlier'
    else:
        _archive(tmp_path / 'earlier.jar', {'fixture/Main.class': b'another fixture'})
        previous = 'earlier.jar'

    with pytest.raises(ValueError):
        _java(tmp_path, entry, [previous, 'classes'])


@pytest.mark.skipif(not shutil.which('java'), reason='Java runtime is unavailable')
def test_transitive_jar_before_entry_cannot_hide_main_origin(tmp_path):
    entry = _write(tmp_path, 'classes/fixture/Main.class')
    _archive(tmp_path / 'earlier.jar', {
        'META-INF/MANIFEST.MF': 'Manifest-Version: 1.0\r\nClass-Path: extra.jar\r\n\r\n',
    })
    _archive(tmp_path / 'extra.jar', {'fixture/Main.class': b'another fixture'})

    with pytest.raises(ValueError):
        _java(tmp_path, entry, ['earlier.jar', 'classes'])


@pytest.mark.skipif(not shutil.which('java'), reason='Java runtime is unavailable')
def test_keeps_explicit_order_for_nonshadowing_dependencies(tmp_path):
    entry = _write(tmp_path, 'classes/fixture/Main.class')
    archive = _archive(tmp_path / 'dep.jar', {'fixture/Helper.class': b'fixture'})
    directory = tmp_path / 'empty'
    directory.mkdir()

    argv = _java(tmp_path, entry, ['empty', 'dep.jar', 'classes'])

    assert argv[1:] == [
        '-cp', os.pathsep.join(map(str, (directory, archive, tmp_path / 'classes'))),
        'fixture.Main',
    ]


@pytest.mark.skipif(not shutil.which('java'), reason='Java runtime is unavailable')
def test_rejects_platform_classpath_separator_in_a_single_path(tmp_path):
    entry = _write(tmp_path, 'classes/fixture/Main.class')
    ambiguous = f'first{os.pathsep}other'
    (tmp_path / ambiguous).mkdir()

    with pytest.raises(ValueError):
        _java(tmp_path, entry, [ambiguous, 'classes'])


@pytest.mark.skipif(not all(shutil.which(tool) for tool in ('java', 'javac')),
                    reason='Java JDK is unavailable')
def test_launches_declared_class_and_both_jar_modes_with_real_java(tmp_path):
    source = _write(tmp_path, 'Main.java', b'''
        package fixture;
        public class Main {
            public static void main(String[] args) {
                System.out.println(args[0]);
            }
        }
    ''')
    classes = tmp_path / 'classes with spaces'
    classes.mkdir()
    subprocess.run(['javac', '-d', str(classes), str(source)], check=True, timeout=30)
    entry = classes / 'fixture' / 'Main.class'
    jar = _archive(tmp_path / 'app file.jar', {
        'META-INF/MANIFEST.MF': 'Manifest-Version: 1.0\r\nMain-Class: fixture.Main\r\n\r\n',
        'fixture/Main.class': entry.read_bytes(),
    })
    commands = [
        _java(tmp_path, entry, ['classes with spaces']),
        _java(tmp_path, jar, ['app file.jar']),
        _command({'runtime': 'java'}, jar, tmp_path, tmp_path),
    ]

    for argv in commands:
        result = subprocess.run(
            [*argv, 'entry verified'], cwd=tmp_path, check=True,
            capture_output=True, text=True, timeout=20,
        )
        assert result.stdout.strip() == 'entry verified'


@pytest.mark.skipif(not all(shutil.which(tool) for tool in ('java', 'javac')),
                    reason='Java JDK is unavailable')
@pytest.mark.parametrize('static_mode', ['stale', 'missing', 'matching', 'separate', 'separate_case'])
async def test_structure_of_built_java_entry_tracks_executed_bytes(tmp_path, static_mode):
    from recheck.porting import run_porting
    from recheck.porting_transport import TrustedFixtures
    from recheck.porting_worker import worker_platform
    from recheck.storage import Store

    class LegacyWorker(TrustedFixtures):
        async def _exchange(self, request, cancel):
            reply = await super()._exchange(request, cancel)
            reply.pop('entry_capture', None)
            return reply

    program = ('class Main {{ public static void main(String[] args) '
               '{{ System.out.println("{}"); }} }}')
    targets = {}
    for side in ('reference', 'candidate'):
        directory = tmp_path / side
        source = _write(tmp_path, f'{side}/Main.java', program.format(
            'built' if static_mode == 'matching' else 'old').encode())
        if static_mode != 'missing':
            subprocess.run(['javac', str(source)], check=True, timeout=25)
        entry = f'{side}/Main.class'
        target = dict(worker_platform(), runtime='java', entry=entry, cwd=side, java_main='Main',
                      files=[f'{side}/Main.java'], build={'tool': 'javac', 'args': ['Main.java']})
        if static_mode == 'separate':
            separate = directory / 'separately-declared.class'
            (tmp_path / entry).rename(separate)
            target['static'] = {'path': f'{side}/{separate.name}'}
        elif static_mode == 'separate_case':
            separate = directory / 'main.class'
            (tmp_path / entry).rename(separate)
            target['static'] = {'path': f'{side}/{separate.name}'}
        source.write_text(program.format('built'), encoding='utf-8')
        targets[side] = target

    manifest = _write(tmp_path, 'manifest.json', json.dumps({
        'schema_version': 'recheck-porting-1',
        'targets': targets,
        'scenarios': [{
            'id': 'build', 'memory_mb': 4096,
            'expected': {side: {'stdout': 'built\n', 'exit_code': 0} for side in targets},
            'policy': {'stdout': 'legacy'},
        }],
    }).encode())
    result = await run_porting(Store(tmp_path / 'history'), manifest, allow_build=True,
                               fixture_workers={'default': LegacyWorker()})

    assert result['stages']['dynamic']['status'] == 'success'
    assert result['behavior']['matched'] == 1
    assert result['behavior']['acceptance'] == 'passed'
    observation = result['scenarios'][0]['attempts'][0]['observations']['reference']
    evidence = result['structure']['targets']['reference']['files'][0]
    if static_mode in ('matching', 'separate', 'separate_case'):
        assert result['structure']['status'] == 'succeeded'
        assert (evidence['sha256'] == observation['entry_sha256']) is (static_mode == 'matching')
        if static_mode != 'matching':
            assert 'entry_binding' not in evidence
            assert 'entry_capture' not in observation
    else:
        assert result['structure']['status'] == 'blocked'
        assert result['structure']['similarity'] is None
        assert evidence['entry_binding']['observed_sha256'] == [observation['entry_sha256']]


@pytest.mark.parametrize('evidence_missing', [True, False])
async def test_unknown_or_divergent_worker_entry_hashes_cannot_yield_similarity(tmp_path, evidence_missing):
    from recheck.porting_static import compare_structure

    entry = _write(tmp_path, 'Main.class')
    local_hash = hashlib.sha256(entry.read_bytes()).hexdigest()
    hashes = [None, None] if evidence_missing else [local_hash, 'f' * 64]
    target = {'runtime': 'java', 'entry': 'Main.class', 'build': {'tool': 'javac'}}
    result = await compare_structure(
        {'targets': {'reference': target, 'candidate': target}}, tmp_path,
        execution_entries={'reference': hashes, 'candidate': hashes},
    )

    assert result['status'] == 'blocked'
    assert result['similarity'] is None
    item = result['targets']['reference']['files'][0]
    assert item['entry_binding']['observed_sha256'] == ([] if evidence_missing else [local_hash, 'f' * 64])
    assert item['entry_binding']['local_sha256'] is None
