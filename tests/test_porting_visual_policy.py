import copy

import pytest

from recheck.porting_policy import compare_attempt
from recheck.porting_report import render_porting_report


PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
       '/x8AAwMCAO+aN1EAAAAASUVORK5CYII=')


def observation(png, *, functional=True):
    values = {'screen': {'visual_only': True, 'media_type': 'image/png', 'content_b64': png}}
    if functional:
        values['price'] = '250'
    return {'status': 'completed', 'observations': values}


@pytest.mark.parametrize('policy', [
    {'step:screen': 'exact'},
    {'step:screen#/content_b64': 'exact'},
    {'observations': 'exact'},
    {'observations': {'mode': 'exact', 'select': ['/price', '/screen/content_b64']}},
])
def test_visual_fields_cannot_change_functional_agreement(policy):
    reference, candidate = observation(PNG), observation(PNG[:-4] + 'AA==')
    originals = copy.deepcopy((reference, candidate))
    scenario = {'kind': 'browser', 'policy': policy,
                'steps': [{'id': 'price', 'action': 'read'}, {'id': 'screen', 'action': 'screenshot'}]}

    row = compare_attempt(reference, candidate, scenario)

    assert row['match'] is True
    assert row['raw_match'] is True
    assert (reference, candidate) == originals


@pytest.mark.parametrize('policy', [
    {'step:screen': 'exact'},
    {'observations': 'exact'},
    {'observations': {'mode': 'exact', 'select': ['/screen/content_b64']}},
])
def test_visual_observations_alone_leave_functional_agreement_unmeasured(policy):
    source = observation(PNG, functional=False)
    scenario = {'kind': 'browser', 'policy': policy,
                'steps': [{'id': 'screen', 'action': 'screenshot'}]}

    result = compare_attempt(source, source, scenario)

    assert result['match'] is None
    assert result['acceptance'] == 'inconclusive'


@pytest.mark.parametrize('expectation', [
    {'step:price': '250', 'step:screen#/content_b64': 'another image'},
    {'observations': {'price': '250', 'screen': 'another image'}},
])
def test_visual_fields_cannot_change_functional_expectations(expectation, tmp_path):
    reference, candidate = observation(PNG), observation(PNG[:-4] + 'AA==')
    scenario = {'kind': 'browser', 'expected': {side: expectation for side in ('reference', 'candidate')},
                'steps': [{'id': 'price', 'action': 'read'}, {'id': 'screen', 'action': 'screenshot'}]}

    result = compare_attempt(reference, candidate, scenario)

    assert result['acceptance'] == 'passed'
    attempt = {'index': 1, 'comparison': result,
               'observations': {'reference': reference, 'candidate': candidate}}
    run = {'run_id': 'test', 'status': 'success', 'scenarios': [
        {'id': 'quote', 'kind': 'browser', 'outcome': 'matched', 'attempts': [attempt]},
    ]}
    report = render_porting_report(run, tmp_path / 'report.html').read_text(encoding='utf-8')
    assert 'PNG 바이트 다름' in report
    assert f'src="data:image/png;base64,{PNG}"' in report
