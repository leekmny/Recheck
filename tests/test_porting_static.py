import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

from recheck.comparison import match_functions


def _manifest(left, right, left_runtime='python', right_runtime='python', **options):
    return {
        'targets': {
            'reference': {'entry': left, 'runtime': left_runtime},
            'candidate': {'entry': right, 'runtime': right_runtime},
        },
        'structure': options,
    }


async def test_python_maps_selected_renamed_functions_and_retains_scope(tmp_path):
    from recheck.porting_static import compare_structure

    (tmp_path / '원본.py').write_text('def sum_fee(x):\n    return x + 1\n\ndef helper():\n    pass\n')
    (tmp_path / '포팅.py').write_text('def fee(x):\n    return x + 1\n')
    manifest = _manifest('원본.py', '포팅.py', profile='porting', function_map={'sum_fee': 'fee'})
    manifest['targets']['reference']['static'] = {'exclude': ['helper']}

    result = await compare_structure(manifest, tmp_path)

    assert result['status'] == 'succeeded'
    assert result['representation'] == 'python-ast'
    assert result['similarity'] == 95
    assert result['targets']['reference']['scope'] == {
        'total': 2, 'observed': 2, 'selected': 1, 'excluded': 1, 'comparable': 1,
    }
    assert result['comparison']['denominator'] == 1
    assert result['comparison']['matches'][0]['source'] == 'user_mapping'
    assert result['targets']['reference']['files'][0]['sha256']
    json.dumps(result)


async def test_python_retains_partial_observations_and_reports_ambiguous_candidates(tmp_path):
    from recheck.porting_static import compare_structure

    (tmp_path / 'left.py').write_text('def original():\n    return 1\n')
    (tmp_path / 'broken.py').write_text('def broken(:\n')
    (tmp_path / 'right.py').write_text('def one():\n    return 2\n\ndef two():\n    return 3\n')
    manifest = _manifest('left.py', 'right.py')
    manifest['targets']['reference']['static'] = {'files': ['left.py', 'broken.py']}

    result = await compare_structure(manifest, tmp_path)

    assert result['status'] == 'partial'
    assert result['comparison']['matches'][0]['ambiguous'] is True
    assert len(result['comparison']['matches'][0]['alternatives']) == 1
    assert result['comparison']['denominator'] == 2
    assert result['targets']['reference']['scope']['total'] is None
    assert result['targets']['reference']['files'][1]['status'] == 'failed'


async def test_ambiguous_user_selector_keeps_target_evidence_and_blocks_the_score(tmp_path):
    from recheck.porting_static import compare_structure

    (tmp_path / 'left.py').write_text('def fee():\n    return 1\n\ndef fee():\n    return 2\n')
    (tmp_path / 'right.py').write_text('def cost():\n    return 3\n')

    result = await compare_structure(_manifest('left.py', 'right.py', function_map={'fee': 'cost'}), tmp_path)

    assert result['status'] == 'failed'
    assert result['similarity'] is None
    assert result['mapping_issues'][0]['reference_options'] == ['left.py:fee:1:1', 'left.py:fee:4:1']
    assert result['targets']['reference']['scope']['observed'] == 2


async def test_unavailable_extraction_never_creates_a_zero_similarity(tmp_path):
    from recheck.porting_static import compare_structure

    (tmp_path / 'left.py').write_text('def observed():\n    pass\n')
    (tmp_path / 'right.py').write_text('print(1)\n')

    result = await compare_structure(_manifest('left.py', 'right.py'), tmp_path)

    assert result['similarity'] is None
    assert result['comparison']['denominator'] == 0
    assert result['targets']['reference']['scope']['observed'] == 1


async def test_structure_refuses_path_outside_manifest_root_and_keeps_peer(tmp_path):
    from recheck.porting_static import compare_structure

    (tmp_path / 'main.py').write_text('def observed():\n    pass\n')
    result = await compare_structure(_manifest('../outside.py', 'main.py'), tmp_path)

    assert result['status'] == 'failed'
    assert result['similarity'] is None
    assert result['targets']['reference']['files'][0]['status'] == 'failed'
    assert result['targets']['candidate']['scope']['observed'] == 1


def test_native_manual_pairs_reserve_candidates_without_inflating_score():
    def observed(identifier, name):
        return dict(id=identifier, name=name, blocks=[{'id': 'entry'}], edges=[],
                    calls=[], mnemonics=['ret'])

    weights = dict(name=.05, nodes=.25, edges=.2, calls=.1, mnemonics=.4)
    result = match_functions(
        [observed('a', 'preferred'), observed('b', 'backup')],
        [observed('c', 'preferred'), observed('d', 'backup')],
        weights=weights,
        manual_pairs={'a': 'd'},
    )

    assert {(item['original'], item['reimplementation']) for item in result['matches']} == {
        ('a', 'd'), ('b', 'c'),
    }
    assert result['score'] == pytest.approx(.95)
    assert result['matches'][0]['source'] == 'user_mapping'
    assert result['candidate_search']['examined_pairs'] == 2


def _javac():
    from recheck.prerequisites import _candidate_paths

    name = 'javac.exe' if os.name == 'nt' else 'javac'
    return next((str(path) for path in (Path(java).with_name(name)
                                       for java in _candidate_paths('java'))
                 if path.is_file()), None)


@pytest.mark.parametrize('relative_workspace', [False, True])
async def test_java_reads_real_class_methods_and_refuses_cross_representation_score(
        tmp_path, monkeypatch, relative_workspace):
    from recheck.porting_static import compare_structure

    javac = _javac()
    if not javac:
        pytest.skip('JDK javac is unavailable')
    (tmp_path / 'Fees.java').write_text(
        'public class Fees { private int bias = 1; '
        'public int cost(int x) { if (x < 0) return 0; return x + bias; } }'
    )
    subprocess.run([javac, 'Fees.java'], cwd=tmp_path, timeout=30, check=True)
    (tmp_path / 'fees.py').write_text('def cost(x):\n    return 0 if x < 0 else x + 1\n')

    workspace = None
    if relative_workspace:
        monkeypatch.chdir(tmp_path)
        workspace = Path('analysis')
    measured = await compare_structure(_manifest('Fees.class', 'Fees.class', 'java', 'java'),
                                       tmp_path, workspace=workspace)
    result = await compare_structure(_manifest('Fees.class', 'fees.py', 'java', 'python'), tmp_path)

    assert measured['status'] == 'succeeded'
    assert measured['similarity'] == 100
    assert result['status'] == 'unsupported'
    assert result['similarity'] is None
    methods = result['targets']['reference']['functions']
    assert [item['signature'] for item in methods] == ['()V', '(I)I']
    cost = next(item for item in methods if item['name'] == 'Fees.cost')
    assert cost['features']['branches'] == 1
    assert 'ifge' in cost['features']['tokens']
    assert result['targets']['reference']['extractor']['version']


async def test_java_archive_never_silently_reuses_duplicate_class_evidence(tmp_path):
    from recheck.porting_static import compare_structure

    javac = _javac()
    if not javac:
        pytest.skip('JDK javac is unavailable')
    (tmp_path / 'Counter.java').write_text('class Counter { static int next(int x) { return x + 1; } }')
    subprocess.run([javac, 'Counter.java'], cwd=tmp_path, timeout=30, check=True)
    with zipfile.ZipFile(tmp_path / 'duplicate.jar', 'w') as archive:
        archive.write(tmp_path / 'Counter.class', 'Counter.class')
        with pytest.warns(UserWarning, match='Duplicate name'):
            archive.writestr('Counter.class', b'not the same class')
    result = await compare_structure(_manifest('duplicate.jar', 'Counter.class', 'java', 'java'), tmp_path)

    assert result['status'] == 'partial'
    assert result['similarity'] == 100
    duplicate = result['targets']['reference']['files'][0]
    assert duplicate['class_entries'] == 2
    assert duplicate['selected_class_entries'] == 1
    assert duplicate['classes'][0]['status'] == 'failed'
    assert duplicate['classes'][1]['status'] == 'succeeded'


@pytest.mark.parametrize('invalid_capture', [False, True])
async def test_porting_uses_only_verified_worker_built_java_entry(tmp_path, monkeypatch, invalid_capture):
    from recheck.porting import run_porting
    from recheck.porting_transport import TrustedFixtures
    from recheck.porting_worker import worker_platform
    from recheck.storage import Store

    javac = _javac()
    if not javac:
        pytest.skip('JDK javac is unavailable')
    monkeypatch.setenv('PATH', str(Path(javac).parent) + os.pathsep + os.environ['PATH'])
    manifest = _manifest('left/Quote.class', 'right/Quote.class', 'java', 'java')
    for side, target in manifest['targets'].items():
        folder = Path(target['entry']).parent
        (tmp_path / folder).mkdir()
        source = (folder / 'Quote.java').as_posix()
        method = 'fee' if side == 'reference' else 'cost'
        (tmp_path / source).write_text(
            f'public class Quote {{ static int {method}() {{ return 12; }} '
            f'public static void main(String[] args) {{ System.out.println({method}()); }} }}',
            encoding='utf-8',
        )
        target.update(**worker_platform(), files=[source], java_main='Quote',
                      java_classpath=[folder.as_posix()], build={'tool': 'javac', 'args': [source]})
    manifest.update(schema_version='recheck-porting-1',
                    scenarios=[{'id': 'quote', 'timeout': 35, 'memory_mb': 2048}])
    path = tmp_path / 'porting.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')

    class InvalidCapture(TrustedFixtures):
        async def _exchange(self, request, cancel):
            reply = await super()._exchange(request, cancel)
            if request['op'] == 'run' and 'entry_capture' in reply:
                reply['entry_capture']['data_b64'] = 'dGFtcGVyZWQ='
            return reply

    store = Store(tmp_path / 'history')
    worker = InvalidCapture() if invalid_capture else TrustedFixtures()
    run = await run_porting(store, path, allow_build=True, fixture_workers={'default': worker})
    assert run['behavior']['matched'] == run['behavior']['denominator'] == 1
    assert run['structure']['status'] == ('blocked' if invalid_capture else 'succeeded')
    assert not list(tmp_path.glob('*/Quote.class'))
    assert store.load_run(run['run_id']) == run
    observations = run['scenarios'][0]['attempts'][0]['observations']
    for side, method in [('reference', 'fee'), ('candidate', 'cost')]:
        observation = observations[side]
        assert 'data_b64' not in observation['entry_capture']
        assert '_entry_artifact' not in observation
        if invalid_capture:
            assert observation['entry_capture']['status'] == 'invalid'
        else:
            assert observation['entry_capture']['status'] == 'verified'
            evidence = run['structure']['targets'][side]
            assert evidence['files'][0]['entry_binding']['source'] == 'worker_capture'
            assert evidence['files'][0]['sha256'] == observation['entry_sha256']
            assert f'Quote.{method}' in [function['name'] for function in evidence['functions']]


@pytest.mark.parametrize(('runtime', 'suffix', 'argument'), [
    ('node', 'js', 'x'), ('typescript', 'ts', 'x: number'),
])
async def test_node_and_typescript_use_the_actual_typescript_parser(tmp_path, runtime, suffix, argument):
    from recheck.porting_static import compare_structure

    if not shutil.which('node') or not os.environ.get('RECHECK_TYPESCRIPT_MODULE'):
        pytest.skip('Node.js and the operator-configured TypeScript package are required')
    source = f'const price = ({argument}) => x > 0 ? compute(x) : 0;\n'
    (tmp_path / f'fees.{suffix}').write_text(source)
    manifest = _manifest(f'fees.{suffix}', f'fees.{suffix}', runtime, runtime)

    result = await compare_structure(manifest, tmp_path)

    assert result['status'] == 'succeeded'
    assert result['similarity'] == 100
    function = result['targets']['reference']['functions'][0]
    assert function['name'] == 'price'
    assert function['features']['branches'] == 1
    assert function['features']['calls'] == 1
    assert result['targets']['reference']['extractor']['version']
