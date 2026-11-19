import base64

import pytest

from recheck.porting_policy import compare_attempt


def _observation(*, stdout='', stderr='', observations=None):
    return dict(status='completed', reason='exit_zero', exit_code=0,
                stdout_b64=base64.b64encode(stdout.encode()).decode(),
                stderr_b64=base64.b64encode(stderr.encode()).decode(),
                observations=observations or {})


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
@pytest.mark.parametrize('options', [{'select': ['/status']}, {'tolerances': {'/amount': {'abs': 750}}}])
def test_pairwise_policy_cannot_mask_independent_stream_expectation(stream, options):
    reference = '{"status":"ok","amount":250}'
    candidate = '{"status":"ok","amount":999}'
    rule = {'mode': 'json', **options}
    scenario = {'policy': {stream: rule},
                'expected': {side: {stream: reference} for side in ('reference', 'candidate')}}

    result = compare_attempt(_observation(**{stream: reference}), _observation(**{stream: candidate}), scenario)

    assert result['match'] is True
    assert result['normalization_masked'] is True
    assert result['policy'][stream] == rule
    assert result['expected']['reference']['checks'][stream] == 'passed'
    assert result['expected']['candidate']['checks'][stream] == 'failed'
    assert result['acceptance'] == 'failed'


def test_json_expectations_still_ignore_object_key_order_and_formatting():
    reference = _observation(stdout='{"status":"ok","amount":250}')
    candidate = _observation(stdout='{"amount": 250, "status": "ok"}\n')
    scenario = {'policy': {'stdout': 'json'},
                'expected': {side: {'stdout': '{"amount":250,"status":"ok"}'}
                             for side in ('reference', 'candidate')}}

    assert compare_attempt(reference, candidate, scenario)['acceptance'] == 'passed'


@pytest.mark.parametrize('rule,acceptance', [
    ({'mode': 'legacy', 'newlines': True, 'trim_trailing_whitespace': True}, 'passed'),
    ('exact', 'failed'),
])
def test_stream_expectations_preserve_legacy_normalization_and_exact_mode(rule, acceptance):
    reference = _observation(stdout='amount=250\r\n')
    candidate = _observation(stdout='amount=250  \n')
    scenario = {'policy': {'stdout': rule},
                'expected': {side: {'stdout': 'amount=250\n'} for side in ('reference', 'candidate')}}

    assert compare_attempt(reference, candidate, scenario)['acceptance'] == acceptance


@pytest.mark.parametrize('expected_amount,acceptance', [(None, 'passed'), (250, 'failed')])
def test_explicit_expected_step_pointer_controls_which_values_are_required(expected_amount, acceptance):
    reference = _observation(observations={'quote': {'status': 200, 'amount': 250}})
    candidate = _observation(observations={'quote': {'status': 200, 'amount': 999}})
    expected = {'step:quote#/status': 200}
    if expected_amount is not None:
        expected['step:quote#/amount'] = expected_amount
    scenario = {'kind': 'http', 'steps': [{'id': 'quote', 'action': 'http'}],
                'policy': {'step:quote': {'mode': 'exact', 'select': ['/status']}},
                'expected': {side: expected for side in ('reference', 'candidate')}}

    result = compare_attempt(reference, candidate, scenario)

    assert result['match'] is True
    assert result['expected']['candidate']['checks']['step:quote#/status'] == 'passed'
    assert result['acceptance'] == acceptance
