"""Comparison and contract checks over retained porting observations."""

from __future__ import annotations

import base64
import math
from collections import Counter
from fractions import Fraction

from .porting_manifest import json_loads

_MISSING = object()
_OPERATIONS_WITHOUT_RESULTS = {'ready', 'navigate', 'fill', 'click', 'wait', 'screenshot'}


def _at(value, pointer):
    if pointer == '':
        return value
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('필드는 JSON Pointer로 지정해야 합니다.')
    for part in pointer[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict):
            value = value.get(part, _MISSING)
        elif isinstance(value, list) and part.isascii() and part.isdecimal():
            index = int(part)
            value = value[index] if index < len(value) else _MISSING
        else:
            return _MISSING
    return value


def _equal(left, right, tolerances, pointer=''):
    if type(left) is not type(right):
        return False
    if pointer in tolerances:
        if type(left) not in (int, float):
            raise ValueError('허용 오차는 지정한 유한 숫자 필드에만 적용됩니다.')
        tolerance = tolerances[pointer]
        abs_tol, rel_tol = tolerance.get('abs', 0), tolerance.get('rel', 0)
        if type(left) is float:
            try:
                return math.isclose(left, right, abs_tol=abs_tol, rel_tol=rel_tol)
            except OverflowError:
                # An integer tolerance may be wider than a float can represent.
                left, right = Fraction(left), Fraction(right)
        difference = abs(left - right)
        return difference <= abs_tol or difference <= Fraction(rel_tol) * max(abs(left), abs(right))
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _equal(a, b, tolerances, f'{pointer}/{index}')
            for index, (a, b) in enumerate(zip(left, right)))
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _equal(left[key], right[key], tolerances,
                   f'{pointer}/' + key.replace('~', '~0').replace('/', '~1'))
            for key in left)
    if type(left) is float and (not math.isfinite(left) or not math.isfinite(right)):
        raise ValueError('유한 JSON 숫자가 필요합니다.')
    return left == right


def _bytes(row, name):
    return base64.b64decode(row[name + '_b64'], validate=True)


def _field(row, name):
    if name in ('stdout', 'stderr'):
        return _bytes(row, name)
    if name.startswith('step:'):
        key, separator, pointer = name[5:].partition('#')
        value = row.get('observations', {}).get(key, _MISSING)
        return _at(value, pointer) if separator else value
    return row.get(name, _MISSING)


def _compare_field(left, right, rule):
    if isinstance(rule, str):
        rule = {'mode': rule}
    if not isinstance(rule, dict) or rule.keys() - {'mode', 'select', 'tolerances',
                                                      'newlines', 'trim_trailing_whitespace'}:
        raise ValueError('알 수 없는 비교 정책입니다.')
    mode = rule.get('mode', 'exact')
    select = rule.get('select', [])
    tolerances = rule.get('tolerances', {})
    if mode not in ('exact', 'bytes', 'text', 'json', 'legacy', 'http-json'):
        raise ValueError('알 수 없는 비교 모드입니다.')
    if left is _MISSING or right is _MISSING:
        raise ValueError('비교하려는 관찰이 수집되지 않았습니다.')
    raw_equal = _equal(left, right, {})
    if mode == 'http-json':
        def http_json(value):
            if not isinstance(value, dict) or 'body_b64' not in value:
                raise ValueError('HTTP 응답 본문 바이트가 필요합니다.')
            result = dict(value)
            result['body'] = json_loads(base64.b64decode(result.pop('body_b64'), validate=True).decode('utf-8'))
            return result

        left, right = http_json(left), http_json(right)
        mode = 'exact'
    if mode == 'legacy':
        from .comparison import compare_behavior
        from types import SimpleNamespace

        def capture(data):
            return dict(status='completed', exit_code=0, stderr_b64='',
                        stdout_b64=base64.b64encode(data).decode('ascii'))

        if not isinstance(left, bytes) or not isinstance(right, bytes):
            raise ValueError('legacy는 원시 콘솔 스트림에만 적용됩니다.')
        value = compare_behavior(
            [dict(test_id='stream', original=capture(left), reimplementation=capture(right))],
            SimpleNamespace(normalize_newlines=rule.get('newlines', True),
                            trim_trailing_whitespace=rule.get('trim_trailing_whitespace', False)),
        )
        return raw_equal, value['tests'][0]['match']
    if mode in ('text', 'json'):
        def parse(value):
            if isinstance(value, bytes):
                value = value.decode('utf-8')
            if mode == 'json' and isinstance(value, str):
                return json_loads(value)
            if mode == 'text' and not isinstance(value, str):
                raise ValueError('텍스트 관찰이 필요합니다.')
            return value

        left, right = parse(left), parse(right)
    if not isinstance(select, list) or not isinstance(tolerances, dict):
        raise ValueError('select 또는 tolerances의 자료형이 잘못되었습니다.')
    if select and mode not in ('exact', 'json') or tolerances and mode not in ('exact', 'json'):
        raise ValueError('필드 선택과 오차 정책에는 exact 또는 json 모드가 필요합니다.')
    for path, tolerance in tolerances.items():
        if (not isinstance(tolerance, dict) or not tolerance or tolerance.keys() - {'abs', 'rel'}
                or any(type(x) not in (int, float) or x < 0
                       or type(x) is float and not math.isfinite(x)
                       for x in tolerance.values())
                or any(type(_at(item, path)) not in (int, float) for item in (left, right))):
            raise ValueError('지정한 숫자 필드의 절대·상대 오차를 확인하세요.')
    if select:
        for path in select:
            if _at(left, path) is _MISSING or _at(right, path) is _MISSING:
                return raw_equal, False
        return raw_equal, all(_equal(_at(left, path), _at(right, path), tolerances, path)
                              for path in select)
    return raw_equal, _equal(left, right, tolerances)


def _check_expected(observation, expected, required, policy):
    if not expected:
        return {'status': 'inconclusive' if required else 'not_requested', 'checks': {}}
    checks = {}
    for key, value in expected.items():
        if key == 'deadline_met':
            if type(value) is not bool:
                checks[key] = 'inconclusive'
            elif observation['status'] in ('completed', 'timed_out'):
                checks[key] = 'passed' if (observation['status'] == 'completed') is value else 'failed'
            else:
                checks[key] = 'inconclusive'
            continue
        if observation['status'] != 'completed':
            checks[key] = 'inconclusive'
            continue
        try:
            if key in ('stdout_b64', 'stderr_b64'):
                actual = _bytes(observation, key[:-4])
                value = base64.b64decode(value, validate=True)
            else:
                actual = _field(observation, key)
                if isinstance(actual, bytes) and isinstance(value, str):
                    actual = actual.decode('utf-8')
            if key in ('stdout', 'stderr') and isinstance(actual, str) and isinstance(value, str):
                rule = policy.get(key, 'exact')
                if isinstance(rule, dict):
                    rule = {name: option for name, option in rule.items() if name not in {'select', 'tolerances'}}
                _, equal = _compare_field(actual.encode(), value.encode(), rule)
            else:
                equal = _equal(actual, value, {})
            checks[key] = 'passed' if equal else 'failed'
        except (TypeError, ValueError, KeyError, OverflowError):
            checks[key] = 'inconclusive'
    if (observation['status'] == 'completed' and observation.get('reason') in (
        'signal', 'windows_exception', 'runtime_exception',
    ) and 'reason' not in expected and 'exit_code' not in expected):
        checks['normal_termination'] = 'failed'
    status = ('failed' if 'failed' in checks.values() else
              'inconclusive' if 'inconclusive' in checks.values() else 'passed')
    return {'status': status, 'checks': checks}


def _without_visual_fields(fields, visual_steps):
    return {name: value for name, value in fields.items()
            if not (name.startswith('step:') and name[5:].partition('#')[0] in visual_steps)}


def _without_visual_steps(observations, visual_steps):
    return {step: value for step, value in observations.items() if step not in visual_steps}


def _functional_policy(fields, observations, visual_steps):
    policy = _without_visual_fields(fields, visual_steps)
    if 'observations' not in policy:
        return policy
    if not any(row.get('observations') for row in observations.values()):
        policy.pop('observations')
        return policy
    if isinstance(policy['observations'], dict):
        rule = dict(policy['observations'])

        def functional(path):
            return not (isinstance(path, str) and path.startswith('/')
                        and path[1:].partition('/')[0] in visual_steps)

        if isinstance(rule.get('select'), list) and rule['select']:
            rule['select'] = [path for path in rule['select'] if functional(path)]
            if not rule['select']:
                policy.pop('observations')
                return policy
        if isinstance(rule.get('tolerances'), dict):
            rule['tolerances'] = {path: value for path, value in rule['tolerances'].items()
                                  if functional(path)}
        policy['observations'] = rule
    return policy


def _functional_expectation(fields, visual_steps):
    expectation = _without_visual_fields(fields, visual_steps)
    if isinstance(values := expectation.get('observations'), dict) and values:
        functional = _without_visual_steps(values, visual_steps)
        if functional:
            expectation['observations'] = functional
        else:
            expectation.pop('observations')
    return expectation


def compare_attempt(reference, candidate, scenario):
    observations = {'reference': reference, 'candidate': candidate}
    required = scenario.get('required', True)
    steps = [step for phase in ('prepare', 'steps', 'cleanup') for step in scenario.get(phase, [])]
    visual_steps = {step['id'] for step in steps if step['action'] == 'screenshot'}
    visual_steps.update(step for row in observations.values() for step, value in row.get('observations', {}).items()
                        if isinstance(value, dict) and value.get('visual_only') is True)
    expected_fields = scenario.get('expected', {})
    policy = scenario.get('policy', {})
    if visual_steps:
        # Keep raw screenshots in the run for rendering. Only compare functional values here.
        observations = {side: dict(row, observations=_without_visual_steps(row['observations'], visual_steps))
                        if 'observations' in row else row for side, row in observations.items()}
        expected_fields = {side: _functional_expectation(values, visual_steps)
                           for side, values in expected_fields.items()}
        policy = _functional_policy(policy, observations, visual_steps)
    expected = {side: _check_expected(row, expected_fields.get(side), required, policy)
                for side, row in observations.items()}
    eligible = all(row['status'] == 'completed' for row in observations.values())
    console = scenario.get('kind', 'console') == 'console'
    fields = {name: 'exact' for name in ('stdout', 'stderr', 'exit_code', 'reason')} if console else {}
    fields.update({f'step:{step["id"]}': 'exact' for step in steps
                   if step['action'] not in _OPERATIONS_WITHOUT_RESULTS and step['id'] not in visual_steps})
    fields.update(policy)
    differences, raw_differences, errors = [], [], {}
    if eligible:
        if not fields:
            errors['comparison'] = '기능 판정에 사용할 관찰을 지정하세요.'
        for name, rule in fields.items():
            try:
                raw_equal, equal = _compare_field(_field(observations['reference'], name),
                                                  _field(observations['candidate'], name), rule)
                if not raw_equal:
                    raw_differences.append(name)
                if not equal:
                    differences.append(name)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                errors[name] = str(exc)
    measured = eligible and not errors
    match = not differences if measured else None
    statuses = {item['status'] for item in expected.values()}
    if 'failed' in statuses:
        acceptance = 'failed'
    elif 'inconclusive' in statuses or required and not measured:
        acceptance = 'inconclusive'
    elif not required:
        acceptance = 'not_required'
    else:
        acceptance = 'passed'
    return dict(match=match, raw_match=not raw_differences if measured else None,
                raw_differences=raw_differences, differences=differences, errors=errors,
                normalization_masked=match is True and bool(raw_differences), policy=fields,
                expected=expected, acceptance=acceptance)


def summarize(scenarios):
    attempted = [row for row in scenarios if row['attempts']]
    all_attempts = [attempt for row in attempted for attempt in row['attempts']]
    statuses = Counter(obs['status'] for run in all_attempts for obs in run['observations'].values())
    outcomes = Counter(row['outcome'] for row in scenarios)
    accepts = Counter(row['acceptance'] for row in scenarios if row['required'])
    denominator = outcomes['matched'] + outcomes['mismatched']
    return dict(registered=len(scenarios), attempted=len(attempted), attempts=len(all_attempts),
                steps=sum(len(obs.get('observations', {})) for run in all_attempts
                          for obs in run['observations'].values()),
                process_executions=sum(obs.get('process_executions') or 0 for run in all_attempts
                                       for obs in run['observations'].values()),
                process_observation_unknown=sum(obs.get('process_executions', 0) is None
                                                for run in all_attempts
                                                for obs in run['observations'].values()),
                matched=outcomes['matched'], mismatched=outcomes['mismatched'],
                denominator=denominator, match_rate=outcomes['matched'] / denominator if denominator else None,
                outcomes=dict(outcomes), execution_statuses=dict(statuses), requirement_counts=dict(accepts),
                acceptance=('failed' if accepts['failed'] else 'inconclusive'
                            if accepts['inconclusive'] or not accepts else 'passed'),
                formula='일치한 시나리오 / 모든 등록 반복에서 양쪽 관찰이 비교 가능한 시나리오')
