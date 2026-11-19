import base64
import json

import pytest

from recheck.porting_policy import compare_attempt


def _compare_values(reference, candidate, tolerance):
    def observation(value):
        stdout = json.dumps({'total': value}).encode()
        return {'status': 'completed', 'stdout_b64': base64.b64encode(stdout).decode(),
                'stderr_b64': '', 'reason': 'exit', 'exit_code': 0}

    scenario = {'policy': {'stdout': {'mode': 'json', 'tolerances': {'/total': tolerance}}}}
    return compare_attempt(observation(reference), observation(candidate), scenario)


@pytest.mark.parametrize('left,right,tolerance,match', [
    (2**53, 2**53 + 1, {'abs': 0, 'rel': 0}, False),
    (2**53, 2**53 + 1, {'abs': 1}, True),
    (2**53, 2**53 + 1, {'rel': 2**-54}, False),
    (2**53, 2**53 + 1, {'rel': 2**-53}, True),
    (10**400, 10**400 + 2, {'abs': 1}, False),
    (0, 10**400, {'abs': 10**400}, True),
])
def test_json_integer_tolerance_preserves_exact_delta(left, right, tolerance, match):
    comparison = _compare_values(left, right, tolerance)

    assert comparison['errors'] == {}
    assert comparison['match'] is match
    assert comparison['raw_match'] is False
    assert comparison['normalization_masked'] is match


def test_integer_tolerance_outside_float_range_for_finite_float_values():
    comparison = _compare_values(-1e308, 1e308, {'abs': 10**400})

    assert comparison['errors'] == {}
    assert comparison['match'] is True
    assert comparison['raw_match'] is False


def test_tolerance_preserves_strict_json_number_types():
    comparison = _compare_values(1, 1.0, {'abs': 1})

    assert comparison['errors'] == {}
    assert comparison['match'] is False


@pytest.mark.parametrize('tolerance', [True, -1, float('nan'), float('inf')])
def test_invalid_tolerances_remain_unmeasured(tolerance):
    comparison = _compare_values(1, 2, {'abs': tolerance})

    assert comparison['match'] is None
    assert set(comparison['errors']) == {'stdout'}
