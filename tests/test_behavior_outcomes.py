"""Observable verdicts, exclusions and normalization survive presentation."""
import base64
import copy

from recheck.comparison import compare_behavior
from recheck.models import Settings
from recheck.review_views import verified_behavior


def observed(value=b'', **changes):
    return dict(dict(status='success', exit_code=0, stdout_b64=base64.b64encode(value).decode(),
                     stderr_b64=''), **changes)


def test_exclusive_outcomes_include_unattempted_planned_inputs():
    rows = [
        dict(test_id='ok', original=observed(exit_code=7), reimplementation=observed(exit_code=7)),
        dict(test_id='diff', original=observed(b'1'), reimplementation=observed(b'2')),
        dict(test_id='timeout', original=observed(timed_out=True), reimplementation=observed()),
        dict(test_id='unsupported', original=dict(status='blocked'), reimplementation=observed()),
        dict(test_id='infra', original=dict(status='failed', infrastructure_ok=False), reimplementation=observed()),
    ]
    planned = [dict(id=name) for name in ['ok', 'diff', 'timeout', 'unsupported', 'infra', 'pending']]
    before = copy.deepcopy(rows)
    result = compare_behavior(rows, Settings(), planned_tests=planned)
    assert result['registered'] == 6 and result['attempted'] == 5 and result['not_run'] == 1
    assert result['denominator'] == 2 and result['matched'] == result['mismatched'] == 1
    assert result['excluded'] == 4 and result['match_rate'] == .5
    assert result['outcome_counts'] == dict(matched=1, mismatched=1, inconclusive=2, unsupported=1, infrastructure_error=1)
    assert sum(result['outcome_counts'].values()) == 6
    assert rows == before


def test_normalized_match_keeps_raw_difference_and_changed_stream():
    rows = [dict(test_id='line', original=observed(b'x \r\n'), reimplementation=observed(b'x\n'))]
    result = compare_behavior(rows, Settings(normalize_newlines=True, trim_trailing_whitespace=True))
    assert result['tests'][0]['match'] is True
    assert result['tests'][0]['raw_match'] is False
    assert result['tests'][0]['raw_differences'] == ['stdout']
    assert result['tests'][0]['differences'] == []
    assert result['normalization_masked'] == 1


def test_report_projection_retains_policy_and_exclusion_counts():
    rows = [dict(test_id='line', original=observed(b'x\r\n'), reimplementation=observed(b'x\n')),
            dict(test_id='wait', original=observed(timed_out=True), reimplementation=observed())]
    run = dict(analysis_mode='dynamic', ai_mode='off', tests=[dict(id=name) for name in ['line', 'wait', 'pending']],
               dynamic=rows, settings=dict(normalize_newlines=True, trim_trailing_whitespace=False))
    result = verified_behavior(run)
    assert result['normalization'] == dict(newlines=True, trailing_whitespace=False)
    assert result['attempted'] == 2 and result['not_run'] == 1
    assert result['outcome_counts']['inconclusive'] == 2
    assert result['normalization_masked'] == 1
    assert result['tests'][1]['outcome'] == 'inconclusive'
    assert result['tests'][2]['observation_categories'] == ['missing_observation']


def test_all_timeouts_are_inconclusive_and_have_no_match_rate():
    result = compare_behavior([dict(test_id='t', original=observed(timed_out=True),
                                    reimplementation=observed(timed_out=True))], Settings())
    assert result['match_rate'] is None
    assert result['outcome_counts']['inconclusive'] == 1
    assert result['tests'][0]['raw_match'] is None


def test_report_explains_status_exclusions_raw_difference_and_static_scope(tmp_path):
    from recheck.report import render_report
    record = dict(run_id='outcomes', status='partial', analysis_mode='both', ai_mode='off',
                  tests=[dict(id='line'), dict(id='pending')],
                  dynamic=[dict(test_id='line', original=observed(b'x\r\n'), reimplementation=observed(b'x\n'))],
                  settings=dict(normalize_newlines=True, trim_trailing_whitespace=False),
                  structure=dict(score=None, score_status='unavailable', comparison_scope={
                      'original': dict(inventory=2, eligible=0, unobserved=2, external=0),
                      'reimplementation': dict(inventory=1, eligible=0, unobserved=1, external=0)}))
    text = render_report(record, tmp_path / 'outcomes.html').read_text(encoding='utf-8')
    assert '판정 불가' in text and '인프라 오류' in text
    assert '정규화로 일치한 원시 차이: 1' in text
    assert 'missing_observation' in text
    assert '구조 점수: 산출 불가' in text
    assert '비교 가능 함수' in text
    assert '&quot;newlines&quot;: true' in text


def test_unknown_historical_policy_has_observations_but_no_comparable_denominator():
    run = dict(tests=[dict(id='legacy')], dynamic=[dict(test_id='legacy',
               original=observed(), reimplementation=observed())])
    result = verified_behavior(run)
    assert result['observed_pairs'] == 1
    assert result['denominator'] == 0 and result['excluded'] == 1
    assert result['match_rate'] is None
    assert result['tests'][0]['outcome'] == 'inconclusive'
    assert result['tests'][0]['comparable'] is False


def test_unknown_policy_does_not_hide_known_infrastructure_failure():
    run = dict(tests=[dict(id='error')], dynamic=[dict(test_id='error',
               original=dict(status='failed', infrastructure_ok=False), reimplementation=observed())])
    result = verified_behavior(run)
    assert result['outcome_counts']['infrastructure_error'] == 1
    assert 'infrastructure_failure' in result['tests'][0]['observation_categories']


def test_unregistered_observations_stay_visible_outside_registered_outcome_counts():
    run = dict(tests=[dict(id='registered')], dynamic=[dict(test_id=name,
               original=observed(), reimplementation=observed()) for name in ['registered', 'foreign']],
               settings=dict(normalize_newlines=False, trim_trailing_whitespace=False))
    result = verified_behavior(run)
    assert result['registered'] == 1
    assert sum(result['outcome_counts'].values()) == 1
    assert result['unregistered_observations'] == 1
    assert len(result['tests']) == 2


def test_explicit_unsupported_contract_is_distinct_from_failed_docker_connection():
    unsupported = dict(status='blocked', infrastructure_ok=False,
                       error_code='windows_runtime_unverified')
    connection = dict(status='blocked', infrastructure_ok=False)
    result = compare_behavior([dict(test_id='unsupported', original=unsupported, reimplementation=unsupported),
                               dict(test_id='docker', original=connection, reimplementation=connection)], Settings())
    assert result['outcome_counts']['unsupported'] == 1
    assert result['outcome_counts']['infrastructure_error'] == 1
