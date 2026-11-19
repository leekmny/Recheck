import html
import json
import re
from .models import resources_root
from .display import stage_label, status_label, target_label


def analysis_scope_summary(run):
    """Read requested scopes and their outcomes without changing historical runs."""
    legacy_mode = run.get('mode')
    scope = run.get('analysis_mode')
    if scope not in ('static', 'dynamic', 'both'):
        scope = {
            'static': 'static', 'dynamic': 'dynamic', 'automatic': 'both', 'agent': 'both'
        }.get(legacy_mode)
    ai_mode = run.get('ai_mode')
    if ai_mode not in ('off', 'explain', 'agent'):
        settings = run.get('settings') or {}
        llm = run.get('llm') or {}
        if legacy_mode in ('static', 'dynamic'):
            ai_mode = 'off'
        elif legacy_mode == 'agent':
            ai_mode = 'agent'
        elif legacy_mode == 'automatic' and (
            llm.get('status') in ('success', 'failed', 'partial', 'cancelled')
            or settings.get('model')
            or settings.get('active_ai_profile') not in (None, '', 'legacy-local')
        ):
            ai_mode = 'explain'
        else:
            ai_mode = None
    stages = run.get('stages') or {}
    recorded = run.get('analysis_status') or {}

    def outcome(kind, requested, names):
        if requested is False:
            return 'not_requested'
        if kind in recorded:
            return recorded[kind]
        values = [(stages.get(name) or {}).get('status') for name in names]
        if not values or any(value is None for value in values):
            return 'unrecorded'
        if all(value == 'success' for value in values):
            return 'success'
        if 'cancelled' in values:
            return 'cancelled'
        if all(value == 'failed' for value in values):
            return 'failed'
        if 'success' in values:
            return 'partial'
        if 'blocked' in values:
            return 'blocked'
        if 'running' in values:
            return 'running'
        if 'pending' in values:
            return 'pending'
        return 'partial'

    return dict(
        analysis_mode=scope,
        ai_mode=ai_mode,
        static=outcome(
            'static', scope != 'dynamic' if scope else None,
            ('metadata', 'static_original', 'static_reimplementation', 'structure'),
        ),
        dynamic=outcome(
            'dynamic', scope != 'static' if scope else None, ('metadata', 'image', 'dynamic'),
        ),
        ai=outcome(
            'ai', ai_mode != 'off' if ai_mode else None,
            ('llm', 'agent') if ai_mode == 'agent' else ('llm',),
        ),
    )


def render_report(run, destination):
    if run.get('mode') == 'porting':
        from .porting_report import render_porting_report

        return render_porting_report(run, destination)

    def mapping(value):
        # Historical tool responses may record JSON null or a non-object value.
        # Normalize only the read view; detailed JSON below retains the evidence.
        return value if isinstance(value, dict) else {}

    def dump(value):
        return html.escape(json.dumps(value, ensure_ascii=False, indent=2, default=str))

    esc = lambda value: html.escape(str(value))
    evidence = {ev['evidence_id']: ev for ev in run.get('evidence', [])}

    def evidence_link(eid, label=None):
        label = esc(label if label is not None else eid)
        return f'<a href="#{esc(eid)}">{label}</a>' if eid in evidence else label + ' (근거 없음)'

    interpretation = run.get('llm', {}).get('interpretation', {})

    def function_links(findings):
        links = {}
        for finding in findings:
            scope = set(finding.get('targets', [])) & {'original', 'reimplementation'}
            for eid in finding.get('evidence_ids', []):
                ev = evidence.get(eid, {})
                data = ev.get('data', {})
                if (
                    ev.get('kind') == 'function'
                    and isinstance(data, dict)
                    and data.get('id') in finding.get('function_ids', [])
                    and (not scope or ev.get('target') in scope)
                ):
                    links[eid] = evidence_link(
                        eid, f'{target_label(ev["target"])} · {data.get("name","")} [{data["id"]}]'
                    )
        return ' · '.join(links.values())

    from .review_views import verified_behavior
    behavior = verified_behavior(run)
    scope = analysis_scope_summary(run)
    rate = behavior.get('match_rate')
    rate_text = f'{rate*100:.2f}%' if isinstance(rate, (int, float)) else '미측정'
    matched_text = str(behavior['matched']) if type(behavior['matched']) is int else '?'
    if behavior['matched'] is None:
        rate_text = '판정 미확인'
    observation_scope = behavior.get('scope', '등록하여 실행한 입력 범위에서만 확인한 결과')
    if scope['dynamic'] == 'not_requested':
        rate_text = '선택하지 않음'
        observation_scope = '파일에서 수집한 정적 근거만 확인한 결과 · 실행 동작 미검증'
    pieces = [
        f"<section><h2>{esc(run['run_id'])}</h2><p>실행 상태: <b>{esc(status_label(run['status']))}</b> · 동작 일치율: <b>{rate_text}</b> ({matched_text}/{behavior['denominator']})</p><p>{esc(observation_scope)}</p><small>실측 수치·상태 생성: Recheck. 불일치가 발견되어도 분석 절차가 모두 끝나면 실행 상태는 성공입니다.</small></section>"
    ]
    scope_names = {'static': '정적분석', 'dynamic': '동적분석', 'both': '둘 다 분석'}
    ai_names = {'off': '사용 안 함', 'explain': '결과 설명', 'agent': '결과 설명 + AI 추가 확인'}
    pieces.append(
        '<section><h2>선택한 분석 범위와 완료 상태</h2><table>'
        '<tr><th>항목</th><th>선택·상태</th></tr>'
    )
    scope_rows = [
        ('선택한 분석', scope_names.get(scope['analysis_mode'], '미기록')),
        ('AI 사용', ai_names.get(scope['ai_mode'], '미기록')),
    ]
    for kind, label in (
        ('static', '정적분석'), ('dynamic', '동적분석'), ('ai', 'AI 설명·추가 확인')
    ):
        value = {'not_requested': '선택하지 않음', 'unrecorded': '미기록'}.get(
            scope[kind], status_label(scope[kind])
        )
        scope_rows.append((label, value))
    for label, value in scope_rows:
        pieces.append(f'<tr><td>{esc(label)}</td><td>{esc(value)}</td></tr>')
    pieces.append('</table>')
    if structure := run.get('structure'):
        score = structure.get('score')
        score_text = f'{score:.4f}' if type(score) in (int, float) else '산출 불가'
        pieces.append('<section><h2>구조 점수: ' + esc(score_text) + '</h2>'
                      '<p>함수 구조의 휴리스틱 점수 또는 관리 메서드 대응 범위입니다. '
                      '동작 일치율·동일성 확률·복제 확률이 아닙니다.</p>')
        if structure.get('comparison_scope'):
            pieces.append('<p>비교 가능 함수와 제외 범위 (미관측·외부 함수):</p><pre>'
                          + dump(structure['comparison_scope']) + '</pre>')
        pieces.append('<p>점수 상태: ' + esc(structure.get('score_status', '기존 기록'))
                      + '</p></section>')
    if scope['dynamic'] == 'not_requested':
        pieces.append('<p>동적분석을 선택하지 않아 실행 동작은 검증하지 않았습니다.</p>')
    elif scope['dynamic'] != 'success':
        pieces.append(
            '<p>동적분석을 완료하지 않았습니다. '
            '정적분석의 성공만으로 실행 동작이 같다고 판단할 수 없습니다.</p>'
        )
    pieces.append(
        '<p>각 분석의 성공은 선택한 절차의 완료를 뜻합니다. '
        '동작의 일치 여부는 실제로 측정한 입력별 결과에서 확인하세요.</p></section>'
    )
    from .review_views import coverage_section, regression_section
    from .failure_report import build_failure_report, CATEGORY_LABELS
    pieces.append(coverage_section(run))
    pieces.append(regression_section(run))
    failure = build_failure_report(run)
    pieces.append('<section><h2>별도 실패 항목 보고서</h2><p>결과 화면의 실패 항목 탭에서 '
                  '문제별 원인·다음 조치와 HTML/JSON 내보내기를 확인할 수 있습니다.</p><ul>')
    for category, label in CATEGORY_LABELS.items():
        pieces.append('<li>' + esc(label) + ': ' + str(failure['counts'][category]) + '</li>')
    pieces.append('</ul><p>동작 차이·미측정·근거 수집 미완료는 서로 다른 범주입니다.</p></section>')
    operation = run.get('last_operation') or {}
    if operation.get('kind') == 'reinterpret':
        pieces.append('<section><h2>최근 AI 설명 요청</h2><p>기존 분석 상태: '
                      + esc(status_label(run.get('status'))) + ' · 추가 요청 상태: '
                      + esc(status_label(operation.get('status'))) + '</p><p>원인 코드: '
                      + esc(operation.get('error_code', '없음')) + '</p></section>')
    environment = mapping(run.get('environment'))
    structure = run.get('structure') or {}
    if structure.get('analysis_kind') == 'managed':
        pieces.append('<section><h2>.NET 메서드 본문 차이</h2>'
                      '<p>REA가 읽은 CIL 바이트·메서드 헤더·예외 영역을 비교합니다. '
                      '메서드 대응과 동일한 바이트는 실행 결과의 일치나 C# 소스 복원을 뜻하지 않습니다.</p>')
        for difference in structure.get('managed_differences', []):
            pieces.append('<h3>' + esc(difference.get('original_name', difference.get('original', '')))
                          + ' ↔ ' + esc(difference.get('reimplementation_name', difference.get('reimplementation', '')))
                          + '</h3><p>CIL 본문 차이</p><pre>'
                          + dump({'기존 프로그램': difference.get('original_cil'),
                                  '새 프로그램': difference.get('reimplementation_cil')}) + '</pre>')
        if not structure.get('managed_differences'):
            pieces.append('<p>대응 가능한 메서드에서 관측된 본문 차이가 없습니다. 미관측·미대응 항목은 상세 근거를 확인하세요.</p>')
        pieces.append('</section>')
    ghidra = mapping(environment.get('ghidra'))
    java = mapping(ghidra.get('java'))
    docker = mapping(environment.get('docker'))
    docker_version = mapping(docker.get('version'))
    docker_server = mapping(docker_version.get('Server'))
    docker_observation = '버전 미기록'
    if docker.get('status') in ('failed', 'partial', 'cancelled', 'blocked'):
        docker_observation = status_label(docker['status'])
    elif 'Server' in docker_version:
        docker_observation = (
            '서버 응답 없음 (null)' if docker_version['Server'] is None
            else '버전 기록됨' if docker_server.get('Version')
            else '불완전한 서버 응답'
        )
    llm = run.get('llm', {})
    execution = llm.get('execution') or {}
    settings = run.get('settings', {})
    versions = run.get('versions', {})
    provider = llm.get('provider', 'ollama')
    environment_rows = [
        ('생성 시각', run.get('created', run.get('created_at'))),
        ('현재 시도 ID', run.get('attempt_id')),
        ('Recheck 버전', versions.get('recheck')),
        ('근거 스키마 버전', run.get('schema_version', versions.get('schema'))),
        ('프롬프트 버전', llm.get('prompt_version', versions.get('prompt'))),
        ('Ghidra', ghidra.get('version')),
        ('Java', java.get('stderr') or java.get('stdout')),
        ('Docker Engine', docker_server.get('Version')),
        ('Docker 관측 상태', docker_observation),
        ('Docker 컨텍스트', docker.get('context_name', docker.get('context'))),
        ('실행 이미지 ID', run.get('image', {}).get('image_id')),
        ('AI 제공자', provider),
        ('AI 역할', llm.get('role', 'analysis')),
        ('AI 연결 ID', llm.get('profile_id')),
        ('인증 방식', llm.get('auth_mode', 'none' if provider == 'ollama' else None)),
        ('Ollama', mapping(llm.get('server_info', environment.get('ollama'))).get('version')),
        ('선택 모델', llm.get('model', settings.get('model'))),
        ('모델 다이제스트', execution.get('digest')),
        (
            '모델 실행 위치',
            (
                '원격 API'
                if execution.get('kind') in ('remote', 'remote_api')
                else status_label(execution.get('kind'))
            ),
        ),
        ('추론 서버', llm.get('server', settings.get('ollama_url'))),
        ('제공자 사용량', llm.get('usage')),
        ('해석 요청 사용량 합계', llm.get('usage_totals')),
        ('비용', '비용 확인 불가 · 앱 호출 예산과 계정 결제 한도는 별도'),
    ]
    pieces.append(
        '<section><h2>도구와 실행 환경 버전</h2><table><tr><th>항목</th><th>기록된 값</th></tr>'
    )
    for label, value in environment_rows:
        display = (
            json.dumps(value, ensure_ascii=False, default=str)
            if isinstance(value, (dict, list))
            else value
        )
        pieces.append(
            f'<tr><td>{label}</td><td>{esc(display if display not in (None,"") else "미기록")}</td></tr>'
        )
    pieces.append(
        '</table><p>미기록 항목은 해당 실행에서 확인하지 못한 값입니다. 전체 도구 응답은 아래 도구·환경 상세에서 확인하세요.</p></section>'
    )
    pieces.append(
        '<section><h2>단계별 완료 상태와 소요 시간</h2><table><tr><th>단계</th><th>상태</th><th>소요 시간 (초)</th><th>누락 사유</th></tr>'
    )
    for name, stage in run.get('stages', {}).items():
        pieces.append(
            '<tr>'
            + ''.join(
                f'<td>{esc(value)}</td>'
                for value in (
                    stage_label(name),
                    status_label(stage.get('status')),
                    stage.get('duration_seconds', '미기록'),
                    stage.get('reason', ''),
                )
            )
            + '</tr>'
        )
    pieces.append('</table></section>')
    pieces.append(
        '<section><h2>파일과 테스트 관찰 요약 · Recheck 실측</h2><table><tr><th>대상</th><th>파일</th><th>형식 / 환경</th><th>SHA-256</th></tr>'
    )
    for target, info in run.get('files', {}).items():
        pieces.append(
            f'<tr><td>{esc(target_label(target))}</td><td>{esc(info.get("name",""))}</td><td>{esc(info.get("format",""))} / {esc(info.get("os",""))} / {esc(info.get("architecture",""))}</td><td><code>{esc(info.get("sha256",""))}</code></td></tr>'
        )
    pieces.append(
        '</table><table>'
    )
    outcome_names = {'matched': '일치', 'mismatched': '불일치', 'inconclusive': '판정 불가',
                     'unsupported': '미지원·선행 조건 차단', 'infrastructure_error': '인프라 오류'}
    counts = behavior.get('outcome_counts', {})
    pieces.append('<caption>' + ' · '.join(esc(label) + ': ' + str(counts.get(key, 0))
                                           for key, label in outcome_names.items()) + '</caption>')
    pieces.append('<tr><th>테스트 ID</th><th>관찰</th><th>결과·사유</th><th>일치</th>'
                  '<th>원본 종료</th><th>재구현 종료</th></tr>')
    for test in behavior.get('tests', []):
        pieces.append(
            '<tr>'
            + ''.join(
                f'<td>{esc(value)}</td>'
                for value in (
                    test['test_id'],
                    status_label(test.get('execution_status')),
                    outcome_names.get(test.get('outcome'), status_label(test.get('execution_status')))
                    + ' · ' + ', '.join(test.get('observation_categories', [])),
                    (
                        '일치'
                        if test.get('match') is True
                        else '불일치' if test.get('match') is False else '미측정'
                    ),
                    test.get('original_exit_code'),
                    test.get('reimplementation_exit_code'),
                )
            )
            + '</tr>'
        )
    pieces.append('</table>')
    pieces.append('<p>계획·등록: ' + esc(behavior.get('registered'))
                  + ' · 시도: ' + esc(behavior.get('attempted'))
                  + ' · 비교 가능: ' + esc(behavior.get('denominator'))
                  + ' · 유효 원시 관찰 쌍: ' + esc(behavior.get('observed_pairs'))
                  + ' · 제외: ' + esc(behavior.get('excluded'))
                  + ' · 미실행: ' + esc(behavior.get('not_run'))
                  + ' · 별도 미등록 관찰: ' + esc(behavior.get('unregistered_observations', 0)) + '</p>'
                  + '<p>정규화로 일치한 원시 차이: ' + str(behavior.get('normalization_masked', 0))
                  + ' · 원시 Base64/bytes는 보존합니다. 사유 범주는 중복될 수 있고 결과 상태는 배타적입니다.</p>'
                  + '<p>시간 초과는 판정 불가입니다. 실행 도구·컨테이너 상태와 반복 관찰 없이 '
                  '프로그램의 비종료 결함으로 단정하지 않습니다.</p>')
    mismatches = {x['test_id'] for x in behavior.get('tests', []) if x.get('match') is False}
    for row in run.get('dynamic', []):
        if row['test_id'] in mismatches:
            pieces.append(
                f'<h3>불일치: {esc(row["test_id"])}</h3><p>입력: <code>{esc(json.dumps(row.get("input",{}),ensure_ascii=False))}</code></p>'
            )
            for target in ('original', 'reimplementation'):
                value = row.get(target, {})
                pieces.append(
                    f'<h4>{esc(target_label(target))} · 종료 {esc(value.get("exit_code"))} · {esc(status_label(value.get("status")))}</h4><pre>stdout:\n{esc(value.get("stdout",""))}\nstderr:\n{esc(value.get("stderr",""))}</pre>'
                )
            related = function_links(
                f
                for f in interpretation.get('findings', [])
                if row['test_id'] in f.get('test_ids', [])
            )
            if related:
                pieces.append(f'<p>관련 함수 근거 · 원인 미확정 가설: {related}</p>')
    pieces.append('</section>')
    if interpretation:
        title = '로컬 LLM 해석' if provider == 'ollama' else 'AI 해석'
        pieces.append(
            f'<section><h2>{title} · 검증 필요 가설</h2><p>{esc(interpretation.get("summary",""))}</p>'
        )
        for finding in interpretation.get('findings', []):
            links = ' '.join(evidence_link(eid) for eid in finding.get('evidence_ids', []))
            functions = function_links([finding])
            if functions:
                pieces.append(f'<p>관련 함수 근거 · 원인 미확정 가설: {functions}</p>')
            pieces.append(
                f'<article><h3>{esc(finding.get("finding_id"))} · {esc(finding.get("claim"))}</h3><p>{esc(finding.get("rationale"))}</p><p>근거: {links}</p><p>추가 확인: {esc(" / ".join(finding.get("additional_checks",[])))}</p><p>권장 조치: {esc(" / ".join(finding.get("recommended_actions",[])))}</p><small>상태: {esc(status_label(finding.get("verification_status")))} · 생성: {esc(run.get("llm",{}).get("model",""))}</small></article>'
            )
        pieces.append('</section>')
    reconstruction = run.get('reconstruction')
    if isinstance(reconstruction, dict) and reconstruction:
        mode_names = {'reconstruction': '레거시 재구현', 'modernization': '유지보수·현대화'}
        pieces.append(
            '<section><h2>재구현·현대화 작업과 변경 요구</h2><table><tr><th>항목</th><th>기록</th></tr>'
        )
        for label, key in [
            ('작업 ID', 'workflow_id'),
            ('격리 작업공간', 'workspace'),
            ('생성·수정 소스', 'source_path'),
            ('원본 실행 파일 SHA-256', 'original_sha256'),
            ('사용자 원본 소스 SHA-256', 'input_source_sha256'),
            ('최대 수리 횟수', 'max_repairs'),
        ]:
            pieces.append(
                f'<tr><td>{label}</td><td>{esc(reconstruction.get(key,"미기록"))}</td></tr>'
            )
        pieces.append(
            '</table><p>작업 모드: '
            + esc(mode_names.get(reconstruction.get('mode'), '미기록'))
            + '</p><p>사용자가 요청한 변경:</p><pre>'
            + esc(reconstruction.get('requirements', '미기록'))
            + '</pre>'
        )
        if reconstruction.get('completion_reason'):
            pieces.append(
                '<p>완료·중단 사유: '
                + esc(status_label(reconstruction['completion_reason']))
                + '</p>'
            )
        pieces.append(
            '<details><summary>근거 기반 행동 명세</summary><pre>'
            + dump(reconstruction.get('specification', {}))
            + '</pre></details>'
        )
        pieces.append(
            '<details><summary>빌드·수리 시도</summary><pre>'
            + dump(
                {
                    'builds': reconstruction.get('builds', []),
                    'repairs': reconstruction.get('repairs', []),
                }
            )
            + '</pre></details>'
        )
        pieces.append(
            '<details><summary>허용된 모델 도구 실행 이력</summary><pre>'
            + dump(reconstruction.get('tool_calls', []))
            + '</pre></details>'
        )
        pieces.append(
            '<p>생성 코드는 관찰 근거와 등록 입력으로 검증한 후보입니다. 빌드나 테스트의 성공은 모든 입력의 동등성 또는 변경 요구 전체의 충족을 보장하지 않습니다. 사용자 원본 소스의 변경 여부는 저장된 원본 해시와 작업 기록으로 확인하세요.</p><details><summary>전체 생성 작업 기록</summary><pre>'
            + dump(reconstruction)
            + '</pre></details></section>'
        )
    pieces.append(
        '<section><h2>재현 입력과 실행 설정</h2><ol><li>위 SHA-256과 일치하는 원본 및 재구현 실행 파일을 준비하세요.</li><li>분석 화면에서 아래 테스트를 가져오고 기록된 이미지·모델·자원 제한·비교 정규화 설정을 적용하세요.</li><li>전체 분석을 시작하고 테스트 ID별 출력·종료 코드·근거를 비교하세요. 새 실행 ID의 보고서가 생성됩니다.</li></ol>'
    )
    for test in run.get('tests', []):
        pieces.append(
            f'<details><summary>{esc(test.get("id",""))} · {esc(test.get("description",""))}</summary><p>인자 배열: <code>{esc(json.dumps(test.get("args",[]),ensure_ascii=False))}</code></p><p>표준입력:</p><pre>{esc(test.get("stdin",""))}</pre><p>환경 변수: <code>{esc(json.dumps(test.get("env",{}),ensure_ascii=False))}</code></p><p>보조 파일: <code>{esc(json.dumps(test.get("aux_files",[]),ensure_ascii=False))}</code></p><p>제한 시간: {esc(test.get("timeout_seconds","미기록"))}초</p></details>'
        )
    reproduction = run.get('reproduction', {})
    if reproduction.get('argv'):
        pieces.append(
            '<p>개발 CLI 재현 인자 배열입니다. Python 프로젝트 개발 환경에서 사용하고 아래 기록 설정을 함께 적용하세요. 배포판 사용자는 같은 입력으로 GUI에서 실행할 수 있습니다.</p><pre>'
            + dump(reproduction['argv'])
            + '</pre>'
        )
    pieces.append(
        '<details><summary>실제 분석 설정</summary><pre>'
        + dump(settings)
        + '</pre></details><p>비교 규칙: '
        + esc(behavior.get('formula', '원본 stdout·stderr bytes와 종료 코드 및 실행 상태 비교'))
        + '</p><p>정규화 설정: <code>'
        + dump(behavior.get('normalization', {}))
        + '</code></p></section>'
    )
    pieces.append('<section><h2>복구 시도와 로그 근거</h2>')
    attempts = run.get('attempts', [])
    if attempts:
        pieces.append('<table><tr><th>시도 ID</th><th>완료 시각</th><th>결과</th></tr>')
        for attempt in attempts:
            pieces.append(
                '<tr>'
                + ''.join(
                    f'<td>{esc(value)}</td>'
                    for value in (
                        attempt.get('attempt_id', ''),
                        attempt.get('finished', '미기록'),
                        status_label(attempt.get('status')),
                    )
                )
                + '</tr>'
            )
        pieces.append('</table>')
    else:
        pieces.append('<p>저장된 완료 시도 기록이 없습니다.</p>')
    log_evidence = [
        ev
        for ev in evidence.values()
        if ev.get('kind') in ('log', 'log_segment') or 'events.jsonl' in str(ev.get('source', ''))
    ]
    if log_evidence:
        pieces.append('<ul>')
        for ev in log_evidence:
            pieces.append(
                '<li>'
                + evidence_link(ev['evidence_id'])
                + ' · '
                + esc(ev.get('source', ''))
                + '</li>'
            )
        pieces.append('</ul>')
    else:
        pieces.append('<p>인용된 로그 근거가 없습니다. 단계별 상태와 진단 상세를 확인하세요.</p>')
    if run.get('events'):
        pieces.append(
            '<p>전체 이벤트와 읽기용 로그는 같은 실행 폴더의 <code>events.jsonl</code>, <code>recheck.log</code>에 보존됩니다. 로그 근거의 event ID 또는 줄 범위로 원문을 확인하세요.</p>'
        )
    pieces.append(
        '</section><section><h2>확인된 사실과 결론의 제한</h2><p>테스트 입력·출력·종료 코드·일치율은 실행 관찰을 계산한 결과입니다. LLM 설명과 관련 함수 연결은 별도 검증이 필요한 가설입니다. 원인 확정 여부는 실제 추가 실험으로 확인해야 합니다.</p><p>일치율은 유효하게 비교한 테스트 쌍을 분모로 하며 시간 초과·취소·OOM·출력 잘림·인프라 실패는 제외합니다. 미측정 단계가 있으면 해당 범위의 결론을 내릴 수 없습니다.</p><p>등록한 입력에서의 일치는 모든 입력에서의 동등성을 증명하지 않습니다. 구조 유사도는 동작 동등성의 증명이 아니며, Ghidra 디컴파일은 추정 의사 코드입니다. Docker 격리도 임의의 적대적 코드에 대한 완전한 보안을 보장하지 않습니다.</p></section>'
    )
    for key, title in [
        ('files', '파일 정보'),
        ('stages', '단계 상태·누락 범위'),
        ('structure', '함수 매칭·구조 차이'),
        ('tests', '실제 등록 입력'),
        ('behavior', '테스트 일치율'),
        ('dynamic', '실행 결과·불일치'),
        ('diagnostics', '실패 진단·해결 절차'),
        ('llm', 'LLM 해석·가설'),
        ('questions', '사용자 질문·근거 답변'),
        ('hypothesis_checks', '제안 입력 검증 결과'),
        ('agent', '에이전트 수행 작업'),
        ('attempts', '복구·재시도'),
        ('environment', '도구·환경'),
        ('reproduction', '재현 방법'),
    ]:
        value = run.get(key, {})
        text = dump(value)
        # Replace evidence ID strings with document-local anchors after HTML escaping.
        if evidence:
            ids = {esc(eid): eid for eid in evidence}
            text = re.sub(
                '|'.join(re.escape(eid) for eid in sorted(ids, key=len, reverse=True)),
                lambda match: evidence_link(ids[match.group()]),
                text,
            )
        pieces.append(
            f'<section><h2>{title}</h2><details><summary>근거와 상세 JSON 열기</summary><pre>{text}</pre></details></section>'
        )
    pieces.append('<section><h2>조회 가능한 근거</h2>')
    for ev in run.get('evidence', []):
        eid = ev['evidence_id']
        pieces.append(
            f'<details id="{esc(eid)}"><summary>{html.escape(eid+" · "+ev["kind"]+" · "+target_label(ev["target"]))}</summary><pre>{dump(ev)}</pre></details>'
        )
    pieces.append('</section>')
    template = (resources_root() / 'report.html').read_text(encoding='utf-8')
    destination.write_text(template.replace('{{CONTENT}}', '\n'.join(pieces)), encoding='utf-8')
    return destination
