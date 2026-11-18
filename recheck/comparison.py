from collections import Counter
from bisect import bisect_left
import base64
import difflib
import re


def _name(fn):
    name = fn.get('name', '')
    return '' if re.match(r'^(FUN_|sub_|thunk_|LAB_|fcn\.)', name) else name


def _ratio(a, b):
    return 1 - abs(a - b) / max(a, b, 1)


def _static_facets(fn):
    nodes = fn.get('blocks', fn.get('cfg_nodes'))
    edges = fn.get('edges', fn.get('cfg_edges'))
    cfg = fn.get('cfg', {})
    if not isinstance(cfg, dict):
        cfg = {}
    return (cfg.get('nodes', nodes), cfg.get('edges', edges), fn.get('calls'),
            fn.get('mnemonics', fn.get('normalized_instructions')))


def _static_observation_issues(fn):
    """An absent facet is unknown, even when its model default is an empty list.

    Older exports without coverage labels remain usable when their actual CFG,
    instruction inventory and resolved-call list are retained. Pseudocode is not
    a feature of this score, so a failed or truncated decompile alone is no gate.
    """
    if fn.get('external') is True:
        return ['external_function']
    nodes, edges, calls, mnemonics = _static_facets(fn)
    issues = []
    coverage = fn.get('coverage', {})
    if not isinstance(coverage, dict):
        coverage = {}
        issues.append('coverage_invalid')
    if 'cfg' in fn and not isinstance(fn['cfg'], dict):
        issues.append('cfg_metadata_invalid')
    for facet, allowed in (('cfg', ('complete',)), ('assembly', ('complete',)),
                           ('calls', ('complete', 'complete_resolved_only'))):
        if facet in coverage and coverage[facet] not in allowed:
            issues.append(facet + '_not_observed_complete')
    if not isinstance(nodes, list) or not nodes or not isinstance(edges, list):
        issues.append('cfg_missing')
    if (not isinstance(mnemonics, list) or not mnemonics
            or not all(isinstance(value, str) and value for value in mnemonics)):
        issues.append('assembly_missing')
    if not isinstance(calls, list) or not all(isinstance(value, str) for value in calls):
        issues.append('calls_missing')
    if fn.get('cfg_truncated') is True:
        issues.append('cfg_truncated')
    count = fn.get('instruction_count')
    if (fn.get('instructions_truncated') is True
            or type(count) is int and isinstance(mnemonics, list) and count != len(mnemonics)):
        issues.append('assembly_incomplete')
    return issues


def _features(fn):
    nodes, edges, calls, mnemonics = _static_facets(fn)
    return (
        len(nodes), len(edges), len(calls), Counter(mnemonics),
    )


def similarity(a, b):
    if _static_observation_issues(a) or _static_observation_issues(b):
        return None
    na, ea, ca, ma = _features(a)
    nb, eb, cb, mb = _features(b)
    union = sum((ma | mb).values())
    instr = sum((ma & mb).values()) / union if union else 0
    name = float(bool(_name(a)) and _name(a) == _name(b))
    return round(
        0.25 * name
        + 0.2 * _ratio(na, nb)
        + 0.15 * _ratio(ea, eb)
        + 0.1 * _ratio(ca, cb)
        + 0.3 * instr,
        6,
    )


def match_functions(original, rebuilt):
    fid = lambda f: f.get('id', f.get('entry', f.get('address')))
    inventories = {'original': original, 'reimplementation': rebuilt}
    eligible, unobserved, external, reasons, scope = {}, {}, {}, {}, {}
    for target, functions in inventories.items():
        eligible[target], unobserved[target], external[target], reasons[target] = [], [], [], {}
        for fn in functions:
            issues = _static_observation_issues(fn)
            if fn.get('external') is True:
                external[target].append(fid(fn))
            elif issues:
                unobserved[target].append(fid(fn))
                reasons[target][fid(fn)] = issues
            else:
                eligible[target].append(fn)
        scope[target] = dict(inventory=len(functions), eligible=len(eligible[target]),
                             unobserved=len(unobserved[target]), external=len(external[target]))
    original, rebuilt = eligible['original'], eligible['reimplementation']
    candidates = []
    examined = 0
    ordered = sorted(rebuilt, key=lambda f: len(f.get('mnemonics', [])))
    sizes = [len(f.get('mnemonics', [])) for f in ordered]
    by_name = {}
    for f in rebuilt:
        if _name(f):
            by_name.setdefault(_name(f), []).append(f)
    for a in original:
        # Bound the candidate set for large real-world files; disclose candidate omission.
        if len(rebuilt) > 256:
            at = bisect_left(sizes, len(a.get('mnemonics', [])))
            pool = ordered[max(0, at - 64) : at + 64] + by_name.get(_name(a), [])[:128]
            pool = list({id(b): b for b in pool}.values())
        else:
            pool = rebuilt
        for b in pool:
            examined += 1
            score = similarity(a, b)
            if score is not None and score >= 0.5:
                candidates.append(
                    dict(
                        original=a.get('id', a.get('entry', a.get('address'))),
                        reimplementation=b.get('id', b.get('entry', b.get('address'))),
                        score=score,
                        reason='이름·CFG 노드/간선·호출 수·명령 빈도',
                        source='Recheck',
                        verification_status='heuristic',
                    )
                )
    candidates.sort(key=lambda x: (-x['score'], str(x['original']), str(x['reimplementation'])))
    used_a, used_b, matches = set(), set(), []
    for candidate in candidates:
        a, b = candidate['original'], candidate['reimplementation']
        if a in used_a or b in used_b:
            continue
        alternatives = [
            x
            for x in candidates
            if x is not candidate
            and (x['original'] == a or x['reimplementation'] == b)
            and abs(x['score'] - candidate['score']) < 0.03
        ]
        candidate['ambiguous'] = bool(alternatives)
        candidate['alternatives'] = alternatives[:5]
        matches.append(candidate)
        used_a.add(a)
        used_b.add(b)
    originals = {fid(f): f for f in original}
    rebuilds = {fid(f): f for f in rebuilt}
    code_differences = []
    for match in matches:
        left, right = originals[match['original']], rebuilds[match['reimplementation']]
        ca, cb = left.get('decompile'), right.get('decompile')
        if isinstance(ca, str) and isinstance(cb, str):
            match['decompile_text_equal'] = ca == cb
            if ca != cb:
                diff = list(
                    difflib.unified_diff(
                        ca.splitlines(),
                        cb.splitlines(),
                        fromfile='original/' + left.get('name', ''),
                        tofile='reimplementation/' + right.get('name', ''),
                        lineterm='',
                    )
                )
                code_differences.append(
                    dict(
                        original=match['original'],
                        reimplementation=match['reimplementation'],
                        original_name=left.get('name'),
                        reimplementation_name=right.get('name'),
                        evidence_ids=[
                            f['evidence_id'] for f in (left, right) if f.get('evidence_id')
                        ],
                        diff='\n'.join(diff[:80]),
                        truncated=len(diff) > 80,
                        source='Ghidra 추정 디컴파일의 텍스트 비교; 원본 소스 또는 의미 동등성 판정 아님',
                    )
                )
    possible_pairs = len(original) * len(rebuilt)
    score_status = ('unavailable' if not possible_pairs else
                    'partial' if any(unobserved.values()) or examined < possible_pairs else 'measured')
    return dict(
        matches=matches,
        unmatched_original=[fid(x) for x in original if fid(x) not in used_a],
        unmatched_reimplementation=[fid(x) for x in rebuilt if fid(x) not in used_b],
        unobserved_original=unobserved['original'],
        unobserved_reimplementation=unobserved['reimplementation'],
        excluded_external_original=external['original'],
        excluded_external_reimplementation=external['reimplementation'],
        unobserved_reasons=reasons,
        comparison_scope=scope,
        score_status=score_status,
        formula='0.25×비자동이름일치 + 0.20×노드수비 + 0.15×간선수비 + 0.10×해결된호출수비 + 0.30×명령다중집합Jaccard; 수비=1-|a-b|/max(a,b,1); 후보≥0.5; 모호성차<0.03; 종합=대응점수합/양쪽 비교가능 함수 수 중 큰 값',
        decompile_differences=code_differences,
        score=(sum(x['score'] for x in matches) / max(len(original), len(rebuilt))
               if possible_pairs else None),
        producer='Recheck (휴리스틱, 의미적 동등성 증명 아님)',
        limitations=[
            '비교 가능한 네이티브 함수의 관측 범위 점수입니다. 미관측·외부 함수는 분모에서 제외합니다.',
            'CFG 노드·간선 개수와 명령 빈도를 비교하며, 그래프 위상·피연산자·상수의 의미를 검증하지 않습니다.',
            '호출 수는 해결된 호출의 관측이며 미해결 간접 호출을 모두 포함한다는 뜻이 아닙니다.',
        ],
        candidate_search=dict(
            examined_pairs=examined,
            total_possible_pairs=possible_pairs,
            inventory_possible_pairs=scope['original']['inventory'] * scope['reimplementation']['inventory'],
            policy='재구현 함수가 256개를 넘으면 명령 수 인접 128개와 동일 이름 최대128개 후보; 나머지는 미탐색',
            omitted_pairs=possible_pairs - examined,
        ),
    )


def _native_inventory_scope(artifact):
    """Preserve artifact-level inventory gaps beside the returned-function score."""
    raw_evidence = artifact.get('raw_evidence')
    raw_evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    raw = raw_evidence.get('coverage')
    raw = raw if isinstance(raw, dict) else {}

    def count(value):
        return value if type(value) is int and value >= 0 else None

    returned = len(artifact['functions'])
    declared, observed = count(artifact.get('total_functions')), count(raw.get('inventory_observed'))
    reported_returned = count(raw.get('inventory_returned'))
    total = declared if declared is not None else observed
    complete = raw.get('inventory_complete')
    complete = complete if type(complete) is bool else None
    conflict = bool(
        declared is not None and observed is not None and declared != observed
        or reported_returned is not None and reported_returned != returned
        or total is not None and total < returned
        or complete is True and total is not None and total != returned
    )
    if (conflict or artifact.get('functions_truncated') is True
            or artifact.get('analysis_timed_out') is True or artifact.get('complete') is False):
        complete = False
    elif total is not None:
        if total != returned:
            complete = False
        elif complete is None:
            complete = True
    return dict(inventory_returned=returned, inventory_total=total,
                inventory_complete=complete, inventory_conflict=conflict,
                artifact_status=artifact.get('status', 'unrecorded'))


def compare_static_artifacts(original, rebuilt):
    """Compare native dossiers or execution-free managed member observations."""
    kinds = {value.get('analysis_kind', 'native') for value in (original, rebuilt)}
    if len(kinds) != 1:
        raise ValueError('관리 코드와 네이티브 코드의 함수 구조는 같은 기준으로 비교할 수 없습니다.')
    if kinds == {'native'}:
        result = match_functions(original['functions'], rebuilt['functions'])
        warnings = []
        for target, label, artifact in (('original', '원본', original),
                                         ('reimplementation', '재구현', rebuilt)):
            inventory = _native_inventory_scope(artifact)
            result['comparison_scope'][target].update(inventory)
            raw = artifact.get('raw_evidence')
            raw = raw if isinstance(raw, dict) else {}
            gap = (inventory['inventory_complete'] is not True
                   or inventory['artifact_status'] != 'success'
                   or artifact.get('partial') is True or artifact.get('partial_reasons')
                   or raw.get('truncated') is True or raw.get('partial_reasons'))
            if gap:
                if result['score_status'] == 'measured':
                    result['score_status'] = 'partial'
                total = inventory['inventory_total']
                state = '부분' if inventory['inventory_complete'] is False else '미확인'
                if inventory['inventory_complete'] is True:
                    state = '목록 수집 완료; 추출 범위 제한'
                warnings.append(f"{label} 함수 목록: 반환 {inventory['inventory_returned']} / "
                    f"관측 총수 {total if total is not None else '미확인'}; 완전성 {state}; "
                    f"추출 상태 {inventory['artifact_status']}. 미반환 함수의 유사성은 알 수 없습니다.")
        result['limitations'] = warnings + result['limitations']
        return result
    if kinds != {'managed'}:
        raise ValueError('지원하지 않는 정적 비교 자료입니다.')

    def index(artifact):
        groups = {}
        for fn in artifact['functions']:
            member = fn.get('managed_method', {})
            signature = member.get('signature', {})
            resolved = signature.get('resolved_sha256')
            if (member.get('declaring_type') is not None
                    and signature.get('parse_status') == 'decoded'
                    and isinstance(resolved, str) and re.fullmatch('[a-fA-F0-9]{64}', resolved)):
                key = (member['declaring_type'], member['name'], resolved)
                groups.setdefault(key, []).append(fn)
        return groups

    left, right = index(original), index(rebuilt)
    matches, differences, used_left, used_right = [], [], set(), set()
    fields = ('il_sha256', 'max_stack', 'init_locals', 'local_var_sig_token', 'exception_regions')

    def observed_body(body):
        # A missing header is not an observed null header; validate the fields
        # used below before claiming either equality or a CIL difference.
        if not isinstance(body, dict) or any(field not in body for field in fields):
            return False
        digest, token = body['il_sha256'], body['local_var_sig_token']
        return (body.get('status') == 'present'
                and isinstance(digest, str) and re.fullmatch('[a-fA-F0-9]{64}', digest)
                and type(body['max_stack']) is int and body['max_stack'] >= 0
                and type(body['init_locals']) is bool
                and (token is None or isinstance(token, str) and re.fullmatch('0x[a-fA-F0-9]{8}', token))
                and isinstance(body['exception_regions'], list)
                and all(isinstance(region, dict) for region in body['exception_regions']))

    for key, candidates in left.items():
        peers = right.get(key, [])
        if len(candidates) != 1 or len(peers) != 1:
            continue
        a, b = candidates[0], peers[0]
        body_a, body_b = a['managed_method'].get('body'), b['managed_method'].get('body')
        observed = all(observed_body(body) for body in (body_a, body_b))
        values = [{field: body[field] for field in fields} for body in (body_a, body_b)] if observed else []
        state = 'unobserved' if not observed else 'equal_bytes_and_headers' if values[0] == values[1] else 'different'
        pair = dict(original=a['id'], reimplementation=b['id'], score=1.0,
                    reason='선언 타입·메서드 이름·해석된 시그니처가 양쪽에서 유일하게 일치',
                    source='Recheck / REA 관리 코드 근거', verification_status='identity_only',
                    ambiguous=False, alternatives=[], cil_observation=state)
        matches.append(pair)
        used_left.add(a['id'])
        used_right.add(b['id'])
        if state == 'different':
            differences.append(dict(original=a['id'], reimplementation=b['id'],
                original_name=a['name'], reimplementation_name=b['name'],
                evidence_ids=[item['evidence_id'] for item in (a, b) if item.get('evidence_id')],
                original_cil=values[0], reimplementation_cil=values[1],
                source='REA가 읽은 CIL 원시 바이트 해시·메서드 헤더·예외 영역의 비교'))
    return dict(analysis_kind='managed', matches=matches,
        unmatched_original=[fn['id'] for fn in original['functions'] if fn['id'] not in used_left],
        unmatched_reimplementation=[fn['id'] for fn in rebuilt['functions'] if fn['id'] not in used_right],
        managed_differences=differences, decompile_differences=[],
        score=len(matches) / max(len(original['functions']), len(rebuilt['functions']), 1),
        formula='양쪽에서 유일한 선언 타입·메서드 이름·해석된 시그니처의 대응 수 / 큰 쪽의 메서드 수',
        producer='Recheck 관리 메서드 대응 범위; 의미 동등성 또는 실행 일치율이 아님',
        limitations=['CIL 해시·메타데이터 토큰은 빌드에 종속됩니다. 동일 관측은 모든 입력의 동등성을 증명하지 않습니다.',
                     '본문이 없거나 불완전하면 CIL 비교는 미관측이며, 시그니처가 모호한 메서드는 대응시키지 않습니다.'])


def observation_issues(observation):
    """Reasons a retained raw observation cannot count as measured.

    Older valid observations may omit ``infrastructure_ok``. Missing output is
    never an empty output: successful records must carry both Base64 strings
    and an actual integer exit code (bool is deliberately excluded).
    """
    if not isinstance(observation, dict):
        return ['invalid_observation']
    unsupported = observation.get('status') == 'blocked' and observation.get('error_code') in (
        'unsupported_format', 'unsupported_architecture', 'windows_runtime_unverified',
    )
    issues = []
    for flag, category in (
        ('cancelled', 'cancelled'), ('timed_out', 'timed_out'),
        ('oom_killed', 'oom'), ('truncated', 'output_truncated'),
    ):
        if flag in observation and type(observation[flag]) is not bool:
            issues.append('invalid_observation')
        elif observation.get(flag) is True:
            issues.append(category)
    if 'infrastructure_ok' in observation:
        if type(observation['infrastructure_ok']) is not bool:
            issues.append('invalid_observation')
        elif observation['infrastructure_ok'] is False and not issues and not unsupported:
            issues.append('infrastructure_failure')
    status = observation.get('status')
    if status == 'blocked':
        issues.append('unsupported_or_blocked')
    elif status == 'cancelled':
        issues.append('cancelled')
    elif status not in ('success', 'completed', 'exited'):
        if not issues:
            issues.append('infrastructure_failure')
    if status in ('success', 'completed', 'exited'):
        if type(observation.get('exit_code')) is not int:
            issues.append('invalid_observation')
        for field in ('stdout_b64', 'stderr_b64'):
            if not isinstance(observation.get(field), str):
                issues.append('invalid_observation')
                continue
            try:
                base64.b64decode(observation[field], validate=True)
            except (ValueError, TypeError):
                issues.append('invalid_observation')
    return sorted(set(issues))


def behavior_outcome(test):
    """Exclusive outcome; reason categories remain overlapping diagnostics."""
    if test.get('match') is not None:
        return 'matched' if test['match'] else 'mismatched'
    reasons = set(test.get('observation_categories', []))
    if reasons & {'infrastructure_failure', 'invalid_observation'}:
        return 'infrastructure_error'
    if 'unsupported_or_blocked' in reasons:
        return 'unsupported'
    return 'inconclusive'


def behavior_counts(comparisons):
    outcomes = Counter(behavior_outcome(test) for test in comparisons)
    categories = Counter(category for test in comparisons
                         for category in set(test.get('observation_categories', [])))
    return dict(
        outcome_counts={key: outcomes[key] for key in (
            'matched', 'mismatched', 'inconclusive', 'unsupported', 'infrastructure_error')},
        category_counts=dict(categories),
        normalization_masked=sum(test.get('match') is True and test.get('raw_match') is False
                                 for test in comparisons),
    )


def compare_behavior(rows, settings, *, planned_tests=None):
    def normalize(value):
        if settings.normalize_newlines:
            value = value.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
        if settings.trim_trailing_whitespace:
            value = b'\n'.join(line.rstrip(b' \t') for line in value.split(b'\n'))
        return value

    # Missing registered tests remain visible when execution stopped early.
    rows = list(rows)
    attempted = len(rows)
    if planned_tests is not None:
        observed_ids = {row['test_id'] for row in rows}
        rows += [dict(test_id=test['id'], not_run=True) for test in planned_tests
                 if test['id'] not in observed_ids]
    comparisons = []
    for row in rows:
        a, b = row.get('original', {}), row.get('reimplementation', {})
        reasons = (['missing_observation'] if row.get('not_run') else
                   sorted(set(observation_issues(a) + observation_issues(b))))
        eligible = not reasons

        def output(x, key):
            return normalize(base64.b64decode(x[key + '_b64'], validate=True))

        raw_differences, differences = [], []
        if eligible:
            for key in ('stdout', 'stderr'):
                if a[key + '_b64'] != b[key + '_b64']:
                    # Compare decoded bytes, not their transport representation.
                    if base64.b64decode(a[key + '_b64']) != base64.b64decode(b[key + '_b64']):
                        raw_differences.append(key)
                if output(a, key) != output(b, key):
                    differences.append(key)
            if a['exit_code'] != b['exit_code']:
                raw_differences.append('exit_code')
                differences.append('exit_code')
        equal = eligible and not differences
        comparisons.append(
            dict(
                test_id=row['test_id'],
                execution_status='measured' if eligible else 'unmeasured',
                match=equal if eligible else None,
                raw_match=not raw_differences if eligible else None,
                raw_differences=raw_differences if eligible else None,
                differences=differences if eligible else None,
                observation_categories=(
                    sorted(set(reasons)) if reasons else ['matched' if equal else 'mismatched']
                ),
                original_exit_code=a.get('exit_code') if isinstance(a, dict) else None,
                reimplementation_exit_code=b.get('exit_code') if isinstance(b, dict) else None,
            )
        )
        comparisons[-1]['outcome'] = behavior_outcome(comparisons[-1])
    denominator = sum(x['execution_status'] == 'measured' for x in comparisons)
    numerator = sum(x['match'] is True for x in comparisons)
    return dict(
        tests=comparisons,
        matched=numerator,
        mismatched=denominator - numerator,
        denominator=denominator,
        registered=len(rows),
        attempted=attempted,
        not_run=len(rows) - attempted,
        excluded=len(rows) - denominator,
        outcome_counts=behavior_counts(comparisons)['outcome_counts'],
        normalization_masked=behavior_counts(comparisons)['normalization_masked'],
        match_rate=numerator / denominator if denominator else None,
        category_counts={
            category: sum(category in x['observation_categories'] for x in comparisons)
            for category in (
                'matched',
                'mismatched',
                'infrastructure_failure',
                'unsupported_or_blocked',
                'cancelled',
                'timed_out',
                'oom',
                'output_truncated',
                'invalid_observation',
            )
        },
        formula='일치한 유효 테스트 쌍 / 양쪽 모두 정상 수집된 테스트 쌍 (시간초과·취소·OOM·출력잘림·인프라실패 제외)',
        normalization=dict(
            newlines=settings.normalize_newlines,
            trailing_whitespace=settings.trim_trailing_whitespace,
        ),
        scope='등록하여 실행한 입력 범위에서만 확인한 결과',
        producer='Recheck',
    )
