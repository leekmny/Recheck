"""GUI and HTML presentation of the versioned porting result."""
from __future__ import annotations

import base64
import binascii
import html
import json
from pathlib import Path

STATUS = {
    'success': '수집 완료', 'succeeded': '수집 완료', 'partial': '부분 완료',
    'blocked': '차단됨', 'unsupported': '미지원', 'skipped': '선택 안 함',
    'failed': '실패', 'cancelled': '취소됨', 'not_requested': '선택 안 함',
    'unmeasured': '미측정', 'unexecuted': '미실행', 'matched': '일치',
    'mismatched': '불일치', 'inconclusive': '판단 유보', 'passed': '충족',
    'not_required': '선택 계약', 'running': '실행 중', 'pending': '대기 중',
    'completed': '관찰 완료', 'environment_error': '환경 오류', 'timed_out': '시간초과',
    'oom': '메모리 부족', 'output_truncated': '출력 잘림', 'observation_error': '관찰 실패',
}


def label(value):
    return STATUS.get(value, value or '미기록')


def _scope_summary(target):
    scope = (target or {}).get('scope') or {}
    observed, total, comparable = (scope.get(key) for key in ('observed', 'total', 'comparable'))
    if observed is None and total is None and comparable is None:
        return '미기록'
    values = [str(value) if value is not None else '미기록' for value in (observed, total, comparable)]
    return f'{values[0]}/{values[1]} (비교 가능 {values[2]})'


def summaries(run):
    behavior = run.get('behavior') or {}
    total, denominator = behavior.get('registered'), behavior.get('denominator')
    rate = behavior.get('match_rate')
    coverage = f'{denominator}/{total}' if denominator is not None and total is not None else '미기록'
    agreement = f'{rate:.1%}' if rate is not None else '미측정'
    structure = run.get('structure') or {}
    similarity = structure.get('similarity')
    metric = structure.get('metric_name') or '구조 특징 기반 유사도'
    status = label(structure.get('status'))
    rows = [
        ('테스트 범위 내 동작 일치율', f'{agreement} (비교 가능 {coverage})'),
        ('필수 기대 결과 수용 상태', label(behavior.get('acceptance'))),
        (metric, f'{similarity:.1f}%' if similarity is not None else status),
    ]
    targets = structure.get('targets') or {}
    comparison = structure.get('comparison')
    has_scope = comparison is not None or bool(targets)
    if has_scope or (similarity is not None and structure.get('status')):
        rows.append(('구조 분석 상태', status))
    if has_scope:
        denominator = (comparison or {}).get('denominator')
        reference = _scope_summary(targets.get('reference'))
        candidate = _scope_summary(targets.get('candidate'))
        rows.extend([
            ('구조 비교 분모', str(denominator) if denominator is not None else '미기록'),
            ('구조 관측 범위', f'원본 {reference} · 포팅본 {candidate}'),
        ])
    return rows


def requirement_summary(row, side):
    states = [attempt['comparison']['expected'][side]['status'] for attempt in row['attempts']]
    return ', '.join(label(state) for state in states) if states else '미기록'


def _screenshots(observation):
    images = {}
    for step, value in observation.get('observations', {}).items():
        if (not isinstance(value, dict) or value.get('visual_only') is not True
                or value.get('media_type') != 'image/png'):
            continue
        payload = value.get('content_b64')
        limit = 512 * 1024
        if not isinstance(payload, str) or len(payload) > 4 * ((limit + 2) // 3):
            continue
        try:
            image = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            continue
        if not 33 <= len(image) <= limit or not image.startswith(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'):
            continue
        if any(not 1 <= int.from_bytes(image[offset:offset + 4], 'big') <= 4096
               for offset in (16, 20)):
            continue
        images[step] = base64.b64encode(image).decode('ascii')
    return images


def _visual_report(run):
    rows = []
    for row in run.get('scenarios', []):
        for attempt in row.get('attempts', []):
            observations = attempt.get('observations', {})
            left = _screenshots(observations.get('reference', {}))
            right = _screenshots(observations.get('candidate', {}))
            for step in sorted(left.keys() | right.keys()):
                original, candidate = left.get(step), right.get(step)
                text = ('PNG 비교 미측정' if original is None or candidate is None else
                        'PNG 바이트 일치' if original == candidate else 'PNG 바이트 다름')
                pictures = []
                for title, image in (('원본', original), ('포팅본', candidate)):
                    content = (f'<img loading="lazy" src="data:image/png;base64,{image}" alt="{title} 화면">'
                               if image else '<p>시각 근거 미기록</p>')
                    pictures.append(f'<figure><figcaption>{title}</figcaption>{content}</figure>')
                heading = html.escape(f"{row['id']} · {attempt.get('index')}회 · {step} · {text}")
                rows.append(f'<h3>{heading}</h3><div class="visual-grid">{"".join(pictures)}</div>')
    return '<h2>시각 관찰</h2>' + ''.join(rows) if rows else ''


def _structure_candidates(structure):
    matches = (structure.get('comparison') or {}).get('matches') or []
    if not matches:
        return ''
    visible = matches[:12]
    targets = structure.get('targets') or {}
    columns = (('reference', 'original'), ('candidate', 'reimplementation'))
    labels = {}
    for side, key in columns:
        identifiers = {match.get(key) for match in visible}
        labels[key] = {}
        for function in (targets.get(side) or {}).get('functions') or []:
            identifier = function.get('id', function.get('entry', function.get('address')))
            name = function.get('name')
            if identifier in identifiers and name and name != identifier:
                labels[key][identifier] = f'{name} ({identifier})'
    rows = []
    for match in visible:
        cells = []
        for _, key in columns:
            identifier = match.get(key, '미기록')
            text = labels[key].get(identifier, identifier)
            cells.append(f'<td>{html.escape(str(text))}</td>')
        rows.append('<tr>' + ''.join(cells) + '</tr>')
    omitted = len(matches) - len(visible)
    more = f'<p>이외 {omitted}개는 저장된 JSON에 포함되어 있습니다.</p>' if omitted else ''
    return ('<h2>구조 대응 후보</h2><table><thead><tr><th>원본 함수</th><th>포팅본 함수</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table>' + more)


def render_porting_report(run, destination):
    def esc(value):
        return html.escape(str(value))

    metrics = ''.join(f'<dt>{esc(name)}</dt><dd>{esc(value)}</dd>' for name, value in summaries(run))
    stages = ''.join(f'<li>{esc(name)} · {esc(label(item.get("status")))}</li>'
                     for name, item in run.get('stages', {}).items())
    rows = ''.join(
        '<tr>' + ''.join(f'<td>{esc(value)}</td>' for value in (
            row['id'], row['kind'], label(row['outcome']), requirement_summary(row, 'reference'),
            requirement_summary(row, 'candidate'), len(row['attempts']),
        )) + '</tr>'
        for row in run.get('scenarios', [])
    )
    proof = esc(json.dumps(run, ensure_ascii=False, indent=2))
    structure = run.get('structure') or {}
    content = f"""<!doctype html>
<html lang="ko"><meta charset="utf-8"><title>RECHECK 포팅 검증</title>
<style>
body {{font:16px/1.55 system-ui,sans-serif; margin:2rem auto; padding:0 1rem; max-width:72rem;}}
h1,h2 {{line-height:1.25;}}
dt {{font-weight:650;}} dd {{margin:0 0 .8rem;}}
table {{border-collapse:collapse; width:100%;}}
th,td {{text-align:left; padding:.55rem; border-bottom:1px solid #bbb; overflow-wrap:anywhere;}}
pre {{overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere;}}
.visual-grid {{display:grid; grid-template-columns:repeat(auto-fit,minmax(min(19rem,100%),1fr)); gap:1rem;}}
.visual-grid figure {{margin:0;}}
.visual-grid img {{display:block; max-width:100%; height:auto; border:1px solid #bbb;}}
</style>
<h1>RECHECK 포팅 검증</h1><p>{esc(run.get('run_id'))} · {esc(label(run.get('status')))}</p>
<dl>{metrics}</dl>
<h2>단계별 관찰</h2><ul>{stages}</ul>
<p>{esc(structure.get('representation') or '')} {esc(structure.get('reason') or '')}</p>
{_structure_candidates(structure)}
<h2>시나리오 결과</h2>
<table><thead><tr><th>시나리오</th><th>방식</th><th>동작 비교</th><th>원본 기대 결과</th>
<th>새 구현 기대 결과</th><th>시도 수</th></tr></thead><tbody>{rows}</tbody></table>
{_visual_report(run)}
<h2>정책·재현·원시 근거</h2><details><summary>저장된 JSON 전체 보기</summary><pre>{proof}</pre></details>
</html>
"""
    path = Path(destination)
    path.write_text(content, encoding='utf-8')
    return path
