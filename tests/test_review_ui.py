"""Visible scope and failure exports do not turn gaps into completed checks."""
import importlib
import json
import pytest
from shiboken6 import delete
from PySide6.QtCore import QPoint
from test_gui import app, window
from review_fixtures import issue_run, limited_static_run


def module(name):
    assert importlib.util.find_spec('recheck.'+name), 'Review and recheck presentation is missing'
    return importlib.import_module('recheck.'+name)


def test_scope_presentation_shows_100_of_130_without_fabricating_dynamic_coverage():
    views = module('review_views')
    run = limited_static_run()
    brief = views.coverage_brief(run)
    html = views.coverage_section(run)
    assert '100/130' in brief
    assert '미선택 30' in html
    assert '동작 미검증' in html
    assert '전체 입력' in html


def test_failure_preview_excludes_input_and_output_until_details_selected(app, tmp_path):
    ui = module('review_ui')
    widget = ui.FailureReportPanel()
    try:
        widget.set_run(issue_run())
        assert not widget.include_observations.isChecked()
        assert 'local-sensitive-input' not in widget.browser.toPlainText()
        assert 'alert(1)' not in widget.browser.toPlainText()
        widget.include_observations.setChecked(True)
        assert 'local-sensitive-input' in widget.browser.toPlainText()
        assert 'alert(1)' in widget.browser.toPlainText()
        html = tmp_path / 'failures.html'
        widget.export_to(html, 'html')
        assert '동작 불일치' in html.read_text(encoding='utf-8')
        assert '<script>' not in html.read_text(encoding='utf-8')
        data = tmp_path / 'failures.json'
        widget.export_to(data, 'json')
        report = json.loads(data.read_text(encoding='utf-8'))
        assert report['counts']['behavior_mismatch'] == 1
        assert report['counts']['execution_unmeasured'] == 1
        with pytest.raises(FileExistsError):
            widget.export_to(html, 'html')
    finally:
        widget.close()
        delete(widget)
        app.processEvents()


def test_result_opens_visible_coverage_and_failure_views(window):
    window.show_result(limited_static_run())
    assert hasattr(window, 'coverage_panel'), 'Result needs an explicit observed-scope view'
    assert '100/130' in window.coverage_brief.text()
    assert '미선택 30' in window.coverage_panel.browser.toPlainText()
    window.open_failure_report()
    assert window.result_tabs.currentWidget() is window.failure_panel
    assert window.failure_panel.report['counts']['static_incomplete'] == 2


def test_latest_failed_request_is_visible_without_relabeling_analysis_success(window):
    run = limited_static_run()
    run['last_operation'] = dict(kind='reinterpret', status='failed', error_code='evidence_context')
    window.show_result(run)
    assert '최근 AI 설명 요청: 실패' in window.result_title.text()
    assert '성공' in window.result_title.text()


def test_regression_list_displays_summary_count_and_reference_change_warning(app):
    ui = module('review_ui')
    widget = ui.RegressionPanel()
    try:
        widget.set_suites([dict(suite_id='suite_'+'a'*32, name='saved', source_run_id='run1',
                               created='2026-11-16', case_count=2, status='ready')])
        assert widget.suites.topLevelItem(0).text(1) == '2'
        views = module('review_views')
        html = views.regression_section({'regression': {'suite_id': 'suite1',
            'source_run_id': 'run1', 'reference_changed': True,
            'case_results': [{'test_id': 'case1', 'status': 'resolved'}]}})
        assert '원본 파일이 달라졌습니다' in html
        assert '이번 비교에서 일치' in html
    finally:
        delete(widget)
        app.processEvents()


def test_main_html_shows_scope_and_failed_followup_separately(tmp_path):
    from recheck.report import render_report
    run = limited_static_run()
    run['last_operation'] = dict(kind='reinterpret', status='failed', error_code='evidence_context')
    destination = tmp_path / 'report.html'
    render_report(run, destination)
    content = destination.read_text(encoding='utf-8')
    assert '어디까지 확인했나요?' in content
    assert '100/130' in content
    assert '최근 AI 설명 요청' in content
    assert 'evidence_context' in content


@pytest.mark.parametrize('linked', [False, True])
def test_workflow_without_readable_comparison_cannot_export_previous_failure(window, tmp_path, monkeypatch, linked):
    window.show_result(issue_run())
    workflow = dict(workflow_id='workflow_fixture', status='blocked')
    if linked:
        workflow['comparison_run_id'] = 'run_missing'
        def unreadable(_):
            raise ValueError('unreadable result')
        monkeypatch.setattr(window.engine.store, 'load_run', unreadable, raising=False)
    window.show_workflow_result(workflow)
    assert window.run_result == {}
    assert window.failure_panel.run == {}
    destination = tmp_path / 'wrong-result.html'
    with pytest.raises(ValueError):
        window.failure_panel.export_to(destination, 'html')
    assert not destination.exists()
    assert 'local-sensitive-input' not in window.failure_panel.browser.toPlainText()


def test_cached_match_rate_cannot_hide_invalid_raw_observations(window, tmp_path):
    from recheck.report import render_report
    run = issue_run()
    run['behavior'] = dict(matched=3, denominator=3, match_rate=1.0, tests=[])
    run['dynamic'][0]['original']['stdout_b64'] = '%%%'
    window.show_result(run)
    assert window.result_cards['match'].value.text() == '0.0%'
    assert window.result_cards['matched'].value.text() == '0개'
    destination = tmp_path / 'verified.html'
    render_report(run, destination)
    content = destination.read_text(encoding='utf-8')
    assert '동작 일치율: <b>0.00%</b> (0/1)' in content
    assert '손상된 관찰: 1' in content


def test_saved_input_not_reproduced_does_not_display_pair_match_as_regression_success(window, tmp_path):
    from recheck.report import render_report
    run = issue_run()
    run['tests'] = run['tests'][:1]
    run['dynamic'] = run['dynamic'][:1]
    run['behavior'] = dict(matched=1, denominator=1, match_rate=1.0, tests=[])
    run['regression'] = dict(suite_id='saved', source_run_id='source',
                             case_results=[dict(test_id='same', status='unmeasured', saved_input_reproduced=False)])
    window.show_result(run)
    assert window.result_cards['match'].value.text() == '—'
    assert '미측정' in window.coverage_brief.text()
    destination = tmp_path / 'recheck.html'
    render_report(run, destination)
    assert '동작 일치율: <b>미측정</b> (0/0)' in destination.read_text(encoding='utf-8')


def test_review_actions_and_results_fit_supported_small_window(window, app):
    window.show_result(issue_run())
    window.resize(960, 640)
    window.show()
    app.processEvents()
    assert window.minimumSizeHint().height() <= 640
    assert window.result_tabs.isVisible() and window.result_tabs.height() >= 120
    for title in ('확인한 범위', '실패 항목 보고서', '문제 입력 저장', '재검사 보관함'):
        button = next(button for button in window.result_content.findChildren(type(window.start_button))
                      if button.text() == title)
        point = button.mapTo(window, QPoint(button.width(), button.height()))
        assert point.x() <= window.width() and point.y() <= window.height()


def test_unknown_historical_policy_does_not_claim_zero_differences(window, tmp_path):
    from recheck.report import render_report
    run = issue_run()
    run['settings'] = {}
    run['tests'] = run['tests'][:1]
    run['dynamic'] = run['dynamic'][:1]
    window.show_result(run)
    assert window.result_cards['matched'].value.text() == '—'
    assert window.result_cards['mismatched'].value.text() == '—'
    assert '정책 미확인' in window.result_cards['match'].detail.text()
    destination = tmp_path / 'unknown.html'
    render_report(run, destination)
    text = destination.read_text(encoding='utf-8')
    assert '판정 미확인</b> (?/0)' in text
    assert '유효 원시 관찰 쌍: 1' in text and '비교 가능: 0' in text
