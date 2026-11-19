import json

import pytest

from recheck.porting_manifest import load_manifest, validate_scenario


@pytest.mark.parametrize('field, message', [
    ('os', 'OS·아키텍처'),
    ('arch', 'OS·아키텍처'),
    ('runtime', '런타임'),
    ('kind', '실행 방식'),
])
@pytest.mark.parametrize('value', [[], {}])
def test_manifest_enum_fields_reject_non_scalar_values(tmp_path, field, message, value):
    target = {'entry': 'app.py', 'os': 'windows', 'arch': 'x64', 'runtime': 'python'}
    reference = dict(target)
    scenario = {'id': 'example'}
    (scenario if field == 'kind' else reference)[field] = value
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps({
        'schema_version': 'recheck-porting-1',
        'targets': {'reference': reference, 'candidate': target},
        'scenarios': [scenario],
    }), encoding='utf-8')

    with pytest.raises(ValueError, match=message):
        load_manifest(path)


@pytest.mark.parametrize('steps', [[], [{'id': 'capture', 'action': 'files', 'paths': ['result.txt']}]])
def test_library_scenario_requires_a_call_in_execution_steps(steps):
    with pytest.raises(ValueError, match='call'):
        validate_scenario({'id': 'library', 'kind': 'library', 'steps': steps})


@pytest.mark.parametrize('phase', ['prepare', 'cleanup'])
def test_library_scenario_rejects_calls_outside_execution_steps(phase):
    call = {'action': 'call', 'symbol': 'quote', 'abi': 'cdecl',
            'arg_types': [], 'return_type': 'i32', 'args': []}
    with pytest.raises(ValueError, match='준비·정리'):
        validate_scenario({'id': 'library', 'kind': 'library',
                           'steps': [dict(call, id='invoke')], phase: [dict(call, id='outside')]})
