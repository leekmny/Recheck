from recheck.porting_report import render_porting_report, summaries


def test_partial_structure_score_includes_denominator_and_observed_scope():
    run = {
        'behavior': {'registered': 4, 'denominator': 3, 'match_rate': 2 / 3, 'acceptance': 'failed'},
        'structure': {
            'status': 'partial',
            'similarity': 52.25,
            'comparison': {'denominator': 5},
            'targets': {
                'reference': {'scope': {'observed': 10, 'total': 12, 'comparable': 5}},
                'candidate': {'scope': {'observed': 6, 'total': 6, 'comparable': 4}},
            },
        },
    }

    assert summaries(run)[:3] == [
        ('테스트 범위 내 동작 일치율', '66.7% (비교 가능 3/4)'),
        ('필수 기대 결과 수용 상태', '실패'),
        ('구조 특징 기반 유사도', '52.2%'),
    ]
    metrics = dict(summaries(run))
    assert metrics['구조 분석 상태'] == '부분 완료'
    assert metrics['구조 비교 분모'] == '5'
    assert metrics['구조 관측 범위'] == '원본 10/12 (비교 가능 5) · 포팅본 6/6 (비교 가능 4)'


def test_blocked_structure_distinguishes_missing_denominator_from_zero():
    structure = {
        'status': 'blocked',
        'comparison': None,
        'targets': {'reference': {'scope': {'observed': 0, 'total': None, 'comparable': 0}}},
    }

    metrics = dict(summaries({'structure': structure}))
    assert metrics['구조 특징 기반 유사도'] == '차단됨'
    assert metrics['구조 분석 상태'] == '차단됨'
    assert metrics['구조 비교 분모'] == '미기록'
    assert metrics['구조 관측 범위'] == '원본 0/미기록 (비교 가능 0) · 포팅본 미기록'

    structure['comparison'] = {'denominator': 0}
    assert dict(summaries({'structure': structure}))['구조 비교 분모'] == '0'


def test_report_keeps_legacy_missing_field_labels():
    assert summaries({}) == [
        ('테스트 범위 내 동작 일치율', '미측정 (비교 가능 미기록)'),
        ('필수 기대 결과 수용 상태', '미기록'),
        ('구조 특징 기반 유사도', '미기록'),
    ]
    assert summaries({'structure': {'status': 'unsupported'}})[2] == (
        '구조 특징 기반 유사도', '미지원',
    )


def test_html_shows_scope_and_a_bounded_escaped_function_list(tmp_path):
    matches = [
        {'original': f'old_{index}', 'reimplementation': f'new_{index}', 'mapping_source': 'automatic'}
        for index in range(13)
    ]
    run = {'structure': {
        'status': 'partial',
        'similarity': 30,
        'comparison': {'denominator': 20, 'matches': matches},
        'targets': {
            'reference': {'functions': [{'id': 'old_0', 'name': '<original>'}]},
            'candidate': {'functions': [{'entry': 'new_0', 'name': 'port & parser'}]},
        },
    }}

    report = render_porting_report(run, tmp_path / 'report.html').read_text(encoding='utf-8')
    visible, raw = report.split('<summary>저장된 JSON 전체 보기</summary>')
    assert '<dt>구조 분석 상태</dt><dd>부분 완료</dd>' in visible
    assert '<dt>구조 비교 분모</dt><dd>20</dd>' in visible
    assert '<dt>구조 관측 범위</dt><dd>원본 미기록 · 포팅본 미기록</dd>' in visible
    assert '<h2>구조 대응 후보</h2>' in visible
    assert '&lt;original&gt; (old_0)' in visible
    assert 'port &amp; parser (new_0)' in visible
    assert '<original>' not in visible
    assert 'old_11' in visible
    assert 'old_12' not in visible
    assert 'old_12' in raw
    assert '이외 1개' in visible
