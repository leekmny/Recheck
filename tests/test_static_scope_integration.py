"""Static evidence scope remains visible in AI input and the result tree."""
import copy
import json

import pytest

from recheck.engine import Engine
from recheck.models import Settings
from test_gui import app, window


def structure(score=None, status='unavailable'):
    original = (dict(inventory=3, eligible=0, unobserved=2, external=1) if score is None
                else dict(inventory=3, eligible=1, unobserved=1, external=1))
    rebuilt = (dict(inventory=2, eligible=0, unobserved=1, external=1) if score is None
               else dict(inventory=2, eligible=1, unobserved=0, external=1))
    return dict(score=score, score_status=status, matches=[],
                comparison_scope=dict(original=original, reimplementation=rebuilt),
                limitations=['관측한 함수 범위의 휴리스틱이며 실행 일치율이 아닙니다.'],
                unobserved_original=['left-unobserved'],
                unobserved_reimplementation=['right-unobserved'],
                excluded_external_original=['left-external'],
                excluded_external_reimplementation=['right-external'])


def record(value):
    return dict(run_id='static-scope', attempt_id='scope-attempt', settings={},
                files={}, tests=[], static={}, structure=value, dynamic=[], behavior={},
                evidence=[dict(evidence_id='ev-structure', kind='structure', target='both',
                               data=value)], llm={}, stages={})


@pytest.mark.parametrize('score,status', [(None, 'unavailable'), (1.0, 'partial')])
def test_ai_receives_score_scope_and_limitations_together(tmp_path, score, status):
    value = structure(score, status)
    before = copy.deepcopy(value)
    context = Engine(Settings(num_ctx=8192, num_predict=2048), tmp_path)._context(record(value))
    evidence = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    projected = evidence['data']
    assert projected['score'] == score
    assert projected['score_status'] == status
    assert projected['comparison_scope'] == value['comparison_scope']
    assert projected['limitations'] == value['limitations']
    assert json.loads(evidence['data_excerpt']) == projected
    assert context['wire_characters'] <= context['budget_characters']
    assert value == before


def test_ai_bounds_long_static_excerpts_without_losing_scope(tmp_path):
    value = structure(1.0, 'partial')
    value['limitations'] = ['L' * 10000] * 20
    value['decompile_differences'] = [dict(original='left', reimplementation='right',
        evidence_ids=[], diff='- old\n+ changed\n' * 1000)] * 3
    context = Engine(Settings(num_ctx=8192, num_predict=2048), tmp_path)._context(record(value))
    evidence = next(item for item in context['evidence_data'] if item['kind'] == 'structure')
    projected = evidence['data']
    assert projected['comparison_scope'] == value['comparison_scope']
    assert projected['score_status'] == 'partial'
    assert len(projected['limitations']) == 3
    assert all(len(item) <= 160 for item in projected['limitations'])
    assert projected['limitations_truncated'] is True
    assert context['wire_characters'] <= context['budget_characters']


@pytest.mark.parametrize('score,status,label,eligible,unobserved', [
    (None, 'unavailable', '미관측', 0, 2), (1.0, 'partial', '1.000', 1, 1),
])
def test_result_tree_shows_scope_and_exclusions_without_a_false_zero(
    window, score, status, label, eligible, unobserved,
):
    window.populate_measurements(record(structure(score, status)))
    root = next(window.measurements.topLevelItem(index)
                for index in range(window.measurements.topLevelItemCount())
                if window.measurements.topLevelItem(index).text(0) == '함수 구조 비교')
    rows = {root.child(index).text(0): root.child(index) for index in range(root.childCount())}
    assert rows['관측 범위 구조 점수'].text(1) == label
    assert rows['관측 범위 구조 점수'].text(2) == (
        '산출 불가' if status == 'unavailable' else '부분 관측')
    original = rows['원본 비교 범위']
    assert original.text(1) == f'비교 가능 {eligible} / 목록 3'
    assert original.text(2) == f'미관측 {unobserved} · 외부 제외 1'
    assert '실행 일치율 아님' in root.text(3)
    window.measurement_clicked(original)
    details = json.loads(window.result_summary.toPlainText())
    assert details == dict(inventory=3, eligible=eligible, unobserved=unobserved, external=1)
