"""Escaped, shared presentation of observed scope and saved rechecks."""
from __future__ import annotations
import html
from .coverage import coverage_summary
from .display import status_label

FACETS = {'decompile': '의사코드', 'cfg': '제어 흐름', 'assembly': '어셈블리',
          'calls': '호출 관계', 'cil': '.NET CIL 본문'}
FACET_STATES = {'complete': '수집 완료', 'partial': '일부 수집', 'not_selected': '미선택',
                'failed': '수집 실패', 'unavailable': '미관측', 'unknown': '불명',
                'not_applicable': '대상 아님', 'unobserved': '미관측'}
TARGETS = {'original': '원본', 'reimplementation': '비교할 프로그램'}
REGRESSION_STATES = {'resolved': '이번 비교에서 일치', 'still_different': '차이 유지',
                     'unmeasured': '미측정'}


def _count(value):
    return str(value) if type(value) is int else '?'


def verified_behavior(run):
    """Display verdicts from retained raw evidence; keep cached history untouched."""
    from .comparison import compare_behavior, behavior_outcome, behavior_counts
    from .models import Settings
    dynamic = coverage_summary(run)['dynamic']
    inputs = run.get('tests') if isinstance(run.get('tests'), list) else []
    raw = run.get('dynamic') if isinstance(run.get('dynamic'), list) else []
    groups = {}
    for row in raw:
        if isinstance(row, dict) and isinstance(row.get('test_id'), str):
            groups.setdefault(row['test_id'], []).append(row)
    identifiers = list(dict.fromkeys(item['id'] for item in inputs
                                    if isinstance(item, dict) and isinstance(item.get('id'), str)))
    registered_ids = set(identifiers)
    identifiers.extend(key for key in groups if key not in identifiers)
    settings = run.get('settings') if isinstance(run.get('settings'), dict) else {}
    stored = (run.get('behavior') or {}).get('normalization') or {}
    policy = Settings(normalize_newlines=settings.get('normalize_newlines', stored.get('newlines')) is True,
                      trim_trailing_whitespace=settings.get('trim_trailing_whitespace', stored.get('trailing_whitespace')) is True)
    comparisons = []
    for identifier in identifiers:
        rows = groups.get(identifier, [])
        if len(rows) != 1 or identifier in dynamic['unregistered_ids'] or identifier in dynamic['unreproduced_ids']:
            reason = ('saved_input_not_reproduced' if identifier in dynamic['unreproduced_ids'] else
                      'missing_observation' if not rows else 'invalid_observation')
            item = dict(test_id=identifier, execution_status='unmeasured', match=None,
                        observation_categories=[reason])
        else:
            item = compare_behavior(rows, policy)['tests'][0]
            if not dynamic['comparison_policy_known'] and item['match'] is not None:
                item['match'] = None
                item['observation_categories'] = ['comparison_policy_unknown']
        item['comparable'] = item['match'] is not None
        item['outcome'] = behavior_outcome(item)
        comparisons.append(item)
    matched = dynamic['matched']
    denominator = dynamic['measured'] if dynamic['comparison_policy_known'] else 0
    counted = [item for item in comparisons if item['test_id'] in registered_ids]
    excluded = (max(0, dynamic['registered'] - denominator)
                if dynamic['registered'] is not None else None)
    return dict(tests=comparisons, matched=matched, denominator=denominator,
                observed_pairs=dynamic['measured'],
                unregistered_observations=len(dynamic['unregistered_ids']),
                registered=dynamic['registered'], unmeasured=dynamic['unmeasured'],
                attempted=dynamic['attempted'], not_run=dynamic['not_run'],
                mismatched=dynamic['mismatched'], excluded=excluded,
                normalization=dict(newlines=policy.normalize_newlines,
                                   trailing_whitespace=policy.trim_trailing_whitespace)
                              if dynamic['comparison_policy_known'] else {},
                formula='일치한 유효 테스트 쌍 / 양쪽 모두 정상 수집되고 비교 정책이 확인된 테스트 쌍',
                **behavior_counts(counted),
                match_rate=matched / denominator if type(matched) is int and denominator else None,
                scope='보관된 유효 원시 관찰과 확인 가능한 비교 정책 기준; 저장 입력 재현 실패는 제외합니다.')


def coverage_brief(run):
    data = coverage_summary(run)
    parts = []
    if data['static']['requested'] is not False:
        for target, item in data['static']['targets'].items():
            facet = item['facets']['cil' if item['analysis_kind'] == 'managed' else 'decompile']
            name = 'CIL' if item['analysis_kind'] == 'managed' else '의사코드'
            parts.append(f"{TARGETS[target]} {name} {_count(facet['observed'])}/{_count(facet['total_eligible'])}")
        if data['static']['has_gaps']:
            parts.append('다른 정적 근거에 누락·미확인 범위 있음')
    dynamic = data['dynamic']
    if dynamic['requested'] is False:
        parts.append('동적분석 미선택 · 동작 미검증')
    else:
        parts.append(f"실행 입력 {_count(dynamic['measured'])}/{_count(dynamic['registered'])} 실측"
                     f" · 미측정 {_count(dynamic['unmeasured'])}")
    return ' · '.join(parts) + ' · ?는 전체 범위 미확인'


def coverage_section(run):
    data = coverage_summary(run)
    esc = lambda value: html.escape(str(value), quote=True)
    pieces = ['<section><h2>어디까지 확인했나요?</h2><p>' + esc(coverage_brief(run)) + '</p>',
              '<p>보관된 실제 근거를 집계합니다. 관측과 완전 수집은 다르며, 빈 결과를 완전 분석으로 추정하지 않습니다. '
              '전체 입력의 동등성은 증명하지 않습니다.</p>']
    if data['static']['requested'] is False:
        pieces.append('<p>정적분석: 선택하지 않음</p>')
    else:
        for target, item in data['static']['targets'].items():
            inventory = item['inventory']
            complete = {True: '완전 수집', False: '수집 미완료', None: '완료 여부 미확인'}[inventory['complete']]
            pieces.append(f'<h3>{esc(TARGETS[target])}</h3><p>함수·메서드 목록: '
                          f'{_count(inventory["returned"])}/{_count(inventory["total"])} · {complete}'
                          f' · 외부 함수 {inventory["external"]}개 · 관계 범위 '
                          f'{esc(FACET_STATES.get(item["relationship_status"], "미확인"))}</p>')
            pieces.append('<table><tr><th>근거 종류</th><th>관측 / 전체 대상</th><th>수집 상태</th></tr>')
            for name, title in FACETS.items():
                facet = item['facets'][name]
                states = ' · '.join(f'{label} {facet.get(key, 0)}' for key, label in FACET_STATES.items()
                                     if key in facet and facet.get(key)) or '관측 없음'
                pieces.append(f'<tr><td>{title}</td><td>{_count(facet["observed"])}/'
                              f'{_count(facet["total_eligible"])}</td><td>{esc(states)}</td></tr>')
            pieces.append('</table><p>외부 함수·본문이 없는 메서드는 해당 본문 분석의 누락 분모에 넣지 않습니다.</p>')
            rows = item['functions'][:500]
            if rows:
                pieces.append('<h4>함수별 관측 상태</h4><table><tr><th>함수</th>'
                              + ''.join(f'<th>{title}</th>' for title in FACETS.values()) + '</tr>')
                for function in rows:
                    pieces.append('<tr><td>' + esc(function['name']) + ' [' + esc(function['id']) + ']</td>'
                                  + ''.join('<td>' + esc(FACET_STATES.get(function['facets'][key], '미확인'))
                                            + '</td>' for key in FACETS) + '</tr>')
                pieces.append('</table>')
                if len(item['functions']) > len(rows):
                    pieces.append('<p>세부 표는 앞의 500개까지 표시합니다. 위 집계는 보관된 전체 함수 기준입니다.</p>')
    dynamic = data['dynamic']
    pieces.append('<h3>동적 관찰 범위</h3>')
    if dynamic['requested'] is False:
        pieces.append('<p>선택하지 않음 · 동작 미검증</p>')
    else:
        pieces.append('<table><tr><th>등록 입력</th><th>실측</th><th>미측정</th><th>미실행</th></tr>'
                      + '<tr>' + ''.join('<td>' + _count(dynamic[key]) + '</td>'
                                         for key in ('registered', 'measured', 'unmeasured', 'not_run')) + '</tr></table>')
        pieces.append('<p>손상된 관찰: ' + _count(dynamic['invalid_observations'])
                      + ' · 실제 일치: ' + _count(dynamic['matched'])
                      + ' · 실제 차이: ' + _count(dynamic['mismatched']) + '</p>')
        if not dynamic['comparison_policy_known']:
            pieces.append('<p>기존 기록의 출력 비교 정책이 불명확하여 일치·불일치 수를 추정하지 않습니다.</p>')
        if dynamic['duplicate_ids'] or dynamic['unregistered_ids']:
            pieces.append('<p>중복·미등록 입력의 관찰은 유효 실측 집계에서 제외했습니다.</p>')
    pieces.append('<p>AI 설명: ' + esc(status_label(data['ai']['status'])) + '</p></section>')
    return ''.join(pieces)


def regression_section(run):
    result = run.get('regression') or {}
    if not result:
        return ''
    esc = lambda value: html.escape(str(value), quote=True)
    pieces = ['<section><h2>저장한 문제 입력 재검사</h2><p>이번에 고른 두 프로그램의 동일 입력 비교입니다. '
              '과거 원본 동작과의 완전한 호환성을 증명하지 않습니다.</p>']
    pieces.append('<p>보관함: ' + esc(result.get('suite_id', ''))
                  + ' · 최초 분석: ' + esc(result.get('source_run_id', '')) + '</p>')
    if result.get('reference_changed') is True:
        pieces.append('<p><b>저장 당시의 원본 파일이 달라졌습니다.</b> 현재 선택한 두 파일의 일치 여부로 해석하세요.</p>')
    pieces.append('<table><tr><th>입력</th><th>재검사 결과</th></tr>')
    for case in result.get('case_results', []):
        state = case.get('status', case.get('outcome'))
        pieces.append('<tr><td>' + esc(case.get('test_id', '')) + '</td><td>'
                      + esc(REGRESSION_STATES.get(state, '미확인')) + '</td></tr>')
    pieces.append('</table></section>')
    return ''.join(pieces)
