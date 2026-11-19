"""Porting structure stage. Match only within an explicitly supported representation."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from contextlib import ExitStack
from fnmatch import fnmatchcase
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
import zipfile

from .analysis_environment import StaticRouter
from .comparison import (
    NATIVE_DEFAULT_WEIGHTS,
    _native_inventory_scope,
    _static_observation_issues,
    compare_static_artifacts,
    managed_member_identity,
)
from .models import Settings, inspect_binary
from .porting_extractors import (
    JS_SUFFIXES,
    MAX_FUNCTIONS,
    MAX_JAR_BYTES,
    MAX_SOURCE_BYTES,
    extract_ecmascript,
    extract_java,
    extract_python,
    python_tool,
    read_file,
)
from .porting_manifest import SIDES, bounded, mapping, relative, safe_source, strings

REPRESENTATIONS = {'native': 'native-instructions', 'dotnet': 'dotnet-metadata-cil',
                   'java': 'jvm-bytecode', 'python': 'python-ast',
                   'node': 'javascript-ast', 'typescript': 'typescript-ast'}
NATIVE_PORTING_WEIGHTS = dict(name=.05, nodes=.25, edges=.20, calls=.10, mnemonics=.40)
SYNTAX_WEIGHTS = {
    'default': dict(name=.25, size=.20, branches=.15, calls=.10, tokens=.30),
    'porting': dict(name=.05, size=.25, branches=.20, calls=.10, tokens=.40),
}
LIMITATIONS = {
    'native-instructions': ['동일 파일 형식·대상 OS·아키텍처·바이트 순서를 비교합니다. ABI는 확인된 범위에서 적용합니다.',
                            '컴파일러·최적화·심볼 제거·생성 코드·이름 맹글링은 함수 수와 대응에 영향을 줍니다.',
                            'CFG 노드·간선 수, 해결된 호출 수와 명령 분포는 그래프 위상이나 연산 의미를 측정하지 않습니다.'],
    'dotnet-metadata-cil': ['선언 형식·메서드 이름·해석된 시그니처로 대응합니다. CIL 바이트 해시와 헤더 차이는 별도로 표시합니다.',
                            'CIL 토큰과 바이트 해시는 빌드·최적화에 따라 달라집니다.'],
    'jvm-bytecode': ['메서드의 바이트코드 명령 분포와 분기·호출 명령 수를 비교합니다. 동적 디스패치와 CFG 위상은 관측하지 않습니다.',
                     '컴파일러가 생성한 메서드도 클래스 범위에 포함됩니다. JAR 의존성은 해석하지 않습니다.'],
    'python-ast': ['명시된 Python 소스의 구문 노드·분기 및 호출 구문 수를 비교합니다. 모듈을 가져오거나 실행하지 않습니다.',
                   'AST 노드 종류의 분포는 상수 값과 연산 순서의 의미를 측정하지 않습니다.'],
    'javascript-ast': ['명시된 JavaScript 소스의 TypeScript 파서 AST를 사용합니다. 가져오기와 의존성은 해석하지 않습니다.'],
    'typescript-ast': ['명시된 TypeScript 소스의 AST를 사용합니다. 빌드를 실행하거나 빌드된 JavaScript를 대신 비교하지 않습니다.'],
}
PAIRING_LIMIT = '함수 매칭은 일대일입니다. 분할·통합·인라이닝의 수동 관찰은 mapping_notes에 근거와 함께 기록할 수 있습니다.'


def _selection(config):
    return {key: strings(config.get(key, []), 256) for key in ('include', 'exclude')}


def _plan_files(target, config):
    if 'path' in config and 'files' in config:
        raise ValueError('static.path와 static.files는 함께 지정할 수 없습니다.')
    files = strings(config.get('files', [config.get('path', target['entry'])]), 64)
    if not files or len({file.casefold() for file in files}) != len(files):
        raise ValueError('분석 파일은 서로 다른 경로를 한 개 이상 지정해야 합니다.')
    if target['runtime'] in {'dotnet', 'native'} and len(files) != 1:
        raise ValueError('네이티브 또는 .NET 정적 분석은 한 개 산출물을 사용합니다.')
    return files


def _function_id(function):
    return function.get('id', function.get('entry', function.get('address')))


def _issues(runtime, function):
    if runtime == 'native':
        return _static_observation_issues(function)
    if runtime == 'dotnet':
        return [] if managed_member_identity(function) else ['resolved_method_identity_missing']
    if function.get('features') is None:
        return [function.get('unobserved_reason', 'features_missing')]
    return []


def _scope(evidence, selection):
    selected, excluded, unobserved = [], {}, {}
    for function in evidence['functions']:
        identifier = _function_id(function)
        names = (function.get('name', ''), str(identifier))
        included = any(fnmatchcase(name, pattern) for name in names for pattern in selection['include'])
        explicitly_excluded = any(fnmatchcase(name, pattern) for name in names
                                  for pattern in selection['exclude'])
        if function.get('external') is True:
            excluded[identifier] = 'external_function'
        elif selection['include'] and not included:
            excluded[identifier] = 'outside_include_patterns'
        elif explicitly_excluded:
            excluded[identifier] = 'matched_exclude_pattern'
        else:
            selected.append(identifier)
            issues = _issues(evidence['runtime'], function)
            if issues:
                unobserved[identifier] = issues
    evidence.update(selection=selection, excluded_reasons=excluded, unobserved_reasons=unobserved,
                    selected_functions=selected, scope=dict(
                        total=evidence['scope']['total'], observed=len(evidence['functions']),
                        selected=len(selected), excluded=len(excluded),
                        comparable=len(selected) - len(unobserved),
                    ))


def _binary_format(metadata, path):
    with path.open('rb') as stream:
        header = stream.read(8)
    facts = {k: metadata[k] for k in ('format', 'os', 'architecture')}
    if facts['format'] == 'ELF':
        facts.update(byte_order={1: 'little', 2: 'big'}[header[5]], elf_osabi=header[7])
    elif facts['format'] == 'Mach-O':
        facts['byte_order'] = 'big' if header[0] == 0xFE else 'little'
    elif facts['format'] == 'PE':
        facts['byte_order'] = 'little'
    return facts


async def _binary(path, target, workspace, settings, timeout, cancel, retained):
    runtime = target['runtime']
    metadata = await asyncio.to_thread(inspect_binary, path)
    facts = _binary_format(metadata, path)
    abi = target.get('metadata', {}).get('abi')
    if isinstance(abi, str) and abi.strip():
        facts['declared_abi'] = abi
    result = dict(**facts, size=metadata['size'], sha256=metadata['sha256'],
                  analysis_kind=metadata['analysis_kind'])
    expected_kind = 'native' if runtime == 'native' else 'managed'
    if runtime == 'native':
        declared_arch = 'x86-64' if target.get('arch') == 'x64' else target.get('arch')
        if (target.get('os') not in (None, facts['os']) or declared_arch not in (None, facts['architecture'])):
            return result | dict(status='unsupported', reason='명세의 대상 OS·아키텍처가 산출물과 다릅니다.')
    if metadata['analysis_kind'] != expected_kind:
        return result | dict(status='unsupported', reason=f'{runtime} 정적 분석용 산출물이 필요합니다.')
    if not metadata['static_supported']:
        return result | dict(status='unsupported', reason=metadata['static_reason'])

    if runtime == 'dotnet' and settings.static_backend == 'auto':
        settings = settings.model_copy(update={'static_backend': 'rea'})
    artifact = await StaticRouter(settings).analyze(path, workspace, timeout=timeout, cancel=cancel)
    state = {'success': 'succeeded', 'partial': 'partial', 'blocked': 'blocked',
             'cancelled': 'cancelled'}.get(artifact.get('status'), 'failed')
    raw = artifact.get('raw_evidence', {})
    functions = artifact.get('functions', [])
    if state in ('succeeded', 'partial'):
        if artifact.get('sha256') != metadata['sha256']:
            return result | dict(status='failed', reason='분석 전후에 산출물 해시가 일치하지 않습니다.')
        inventory = _native_inventory_scope(artifact | {'functions': functions})
    else:
        inventory = dict(inventory_total=None, inventory_complete=False)
    versions = artifact.get('backend_versions') or {}
    environment = artifact.get('environment') or {}
    backend = artifact.get('backend')
    version = (versions.get('rea') if backend == 'rea' else
               artifact.get('tool_version') or environment.get('version'))
    reason = artifact.get('error')
    if state == 'partial' and not reason:
        partial_reasons = artifact.get('partial_reasons') or raw.get('partial_reasons') or []
        reason = next(iter(partial_reasons), '분석 도구가 부분 추출 결과를 반환했습니다.')
    if state == 'blocked' and environment.get('status') == 'java_or_path_mismatch':
        launcher = environment.get('launcher')
        if launcher and not Path(launcher).is_file():
            reason = f'Ghidra Headless 실행 파일을 찾을 수 없습니다 ({launcher}).'
        else:
            reason = f"Ghidra가 지원하는 JDK가 필요합니다 (최소 버전 {environment.get('java_required_min')})."
    result.update(status=state, reason=reason, functions=functions,
                  extractor=dict(name=artifact.get('tool') or ('REA MCP' if backend == 'rea' else 'Ghidra'),
                                 version=version, backend=backend, versions=versions,
                                 provenance=artifact.get('backend_provenance')),
                  total=inventory['inventory_total'], inventory_complete=inventory['inventory_complete'],
                  inventory=inventory, limitations=list(dict.fromkeys([
                      *artifact.get('limitations', []), *artifact.get('partial_reasons', []),
                  ])), raw_evidence=dict(coverage=raw.get('coverage'), files=raw.get('files', []),
                                        retained=retained))
    if state == 'blocked' and environment:
        result['extractor']['probe'] = {key: environment[key] for key in (
            'status', 'ready', 'reason', 'java_major', 'java_required_min', 'image_id',
        ) if key in environment}
    if retained:
        result['raw_evidence'].update(source=artifact.get('backend_source'), logs=artifact.get('logs'))
    if state == 'succeeded' and not inventory['inventory_complete']:
        total = inventory['inventory_total']
        result.update(status='partial', reason=f'추출 함수 {len(functions)}개, 전체 함수 {total if total is not None else "미확인"}.')
    return result


async def _extract_file(path, filename, target, work, settings, timeout, cancel, retained):
    runtime = target['runtime']
    if runtime in {'native', 'dotnet'}:
        return await _binary(path, target, work, settings, timeout, cancel, retained)
    suffixes = {'.py'} if runtime == 'python' else {'.class', '.jar'} if runtime == 'java' else JS_SUFFIXES[runtime]
    maximum = MAX_JAR_BYTES if runtime == 'java' and path.suffix.lower() == '.jar' else MAX_SOURCE_BYTES
    data, identity = await asyncio.to_thread(read_file, path, maximum)
    if path.suffix.lower() not in suffixes:
        return identity | dict(status='unsupported', reason=f"static.path에 다음 형식의 분석 파일을 지정하세요 ({', '.join(sorted(suffixes))}).")
    try:
        if runtime == 'python':
            functions, total = await asyncio.to_thread(extract_python, data, filename)
            result = dict(extractor=python_tool(), functions=functions, total=total)
        elif runtime == 'java':
            result = await extract_java(data, filename, work, timeout, cancel)
        else:
            result = await extract_ecmascript(data, filename, work, timeout, cancel)
    except (OSError, ValueError, SyntaxError, RuntimeError, zipfile.BadZipFile) as exc:
        if runtime == 'python':
            identity['extractor'] = python_tool()
        return identity | dict(status='failed', reason=f'{type(exc).__name__}: {exc}')
    return identity | result


def _combine_files(evidence):
    files = evidence['files']
    counts = [item.get('total') for item in files]
    states = {item['status'] for item in files}
    evidence['scope']['total'] = sum(counts) if all(count is not None for count in counts) else None
    if 'cancelled' in states:
        evidence.update(status='cancelled', reason='정적 분석 취소')
    elif states == {'succeeded'}:
        evidence['status'] = 'succeeded'
    elif states & {'succeeded', 'partial'}:
        evidence.update(status='partial', reason='일부 분석 파일 또는 함수 관측이 누락되었습니다.')
    else:
        evidence['status'] = 'failed' if 'failed' in states else 'blocked' if 'blocked' in states else 'unsupported'
        evidence['reason'] = next(item.get('reason') for item in files if item['status'] == evidence['status'])


def _entry_binding(values):
    recorded = [value for value in values
                if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)]
    hashes = sorted(set(recorded))
    binding = dict(observed_sha256=hashes, observed_attempts=len(recorded), attempts=len(values),
                   local_sha256=None)
    if len(hashes) == 1:
        return dict(binding, status='verified' if len(recorded) == len(values) else 'partial')
    reason = ('worker 빌드 산출물의 실행별 SHA-256이 일치하지 않습니다.' if hashes else
              'worker 빌드 산출물의 SHA-256을 관측하지 못했습니다.')
    return dict(binding, status='blocked', reason=reason)


async def _verify_local_entry(path, binding):
    _, identity = await asyncio.to_thread(read_file, path, MAX_JAR_BYTES)
    binding['local_sha256'] = identity['sha256']
    if identity['sha256'] != binding['observed_sha256'][0]:
        binding.update(status='blocked', reason='로컬 구조 분석 파일이 worker 빌드 산출물과 다릅니다.')
    return identity


async def _extract(root, target, workspace, settings, timeout, cancel, retained, execution_entries, captured_entry):
    runtime = target['runtime']
    evidence = dict(runtime=runtime, representation=REPRESENTATIONS[runtime],
                    status='failed', reason=None, extractor=None, files=[], functions=[],
                    scope={'total': None}, limitations=list(LIMITATIONS[REPRESENTATIONS[runtime]]),
                    dependency_scope='explicit input files; import and dependency resolution disabled')
    selection = {'include': [], 'exclude': []}
    try:
        config = mapping(target.get('static', {}), {'path', 'files', 'include', 'exclude'})
        selection = _selection(config)
        files = _plan_files(target, config)
        deadline = time.monotonic() + timeout
        workspace.mkdir(parents=True, exist_ok=True)
        for index, filename in enumerate(files):
            remaining = deadline - time.monotonic()
            if (cancel is not None and cancel.is_set()) or remaining <= 0:
                state = 'cancelled' if cancel is not None and cancel.is_set() else 'failed'
                evidence['files'].append(dict(path=filename, status=state, reason='정적 분석 시간 제한 또는 취소'))
                continue
            item = dict(path=filename, status='succeeded')
            work = workspace / str(index)
            binding = None
            if execution_entries is not None and filename == target['entry']:
                binding = item['entry_binding'] = _entry_binding(execution_entries)
                if binding['status'] == 'blocked':
                    item.update(status='blocked', reason=binding['reason'])
                    evidence['files'].append(item)
                    continue
            try:
                with ExitStack() as inputs:
                    if binding and captured_entry is not None:
                        directory = inputs.enter_context(TemporaryDirectory(prefix='recheck-porting-built-'))
                        path = Path(directory) / Path(relative(filename)).name
                        path.write_bytes(captured_entry)
                        binding['source'] = 'worker_capture'
                    else:
                        path = safe_source(root, relative(filename))
                    if binding:
                        binding.setdefault('source', 'local_file')
                        item.update(await _verify_local_entry(path, binding))
                        if binding['status'] == 'blocked':
                            item.update(status='blocked', reason=binding['reason'])
                            evidence['files'].append(item)
                            continue
                    work.mkdir()
                    artifact = await _extract_file(path, filename, target, work, settings,
                                                   max(1, int(remaining + .999)), cancel, retained)
                if binding and artifact.get('sha256') != binding['local_sha256']:
                    binding.update(status='blocked', local_sha256=artifact.get('sha256'),
                                   reason='검증 후 구조 분석 파일의 SHA-256이 변경되었습니다.')
                    item.update(status='blocked', reason=binding['reason'])
                else:
                    item.update(artifact)
                    if binding and binding['status'] == 'partial' and item['status'] == 'succeeded':
                        item.update(status='partial', reason='일부 실행의 빌드 산출물 해시를 관측하지 못했습니다.')
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                if binding and binding['local_sha256'] is None:
                    binding.update(status='blocked', reason='worker 산출물과 대조할 로컬 파일을 읽을 수 없습니다.')
                    item.update(status='blocked', reason=binding['reason'])
                else:
                    item.update(status='failed', reason=f'{type(exc).__name__}: {exc}')
            if item.get('extractor') and evidence['extractor'] is None:
                evidence['extractor'] = item['extractor']
            evidence['limitations'].extend(item.get('limitations', []))
            functions = item.pop('functions', [])
            room = len(functions) if runtime in {'native', 'dotnet'} else MAX_FUNCTIONS - len(evidence['functions'])
            evidence['functions'].extend(functions[:room])
            if len(functions) > room or (item.get('total') is not None and item['total'] > len(functions)):
                if item['status'] == 'succeeded':
                    item['status'] = 'partial'
                item['reason'] = item.get('reason') or '함수 목록 상한 또는 추출 누락'
            item['returned_functions'] = min(len(functions), room)
            if cancel is not None and cancel.is_set():
                item.update(status='cancelled', reason='정적 분석 취소')
            evidence['files'].append(item)
        _combine_files(evidence)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        evidence['reason'] = str(exc)
    _scope(evidence, selection)
    return evidence


def _selected(evidence):
    identifiers = set(evidence['selected_functions'])
    return [function for function in evidence['functions'] if _function_id(function) in identifiers]


def _resolve_pairs(requested, left, right):
    if not requested:
        return {}, []
    functions = [{_function_id(f): f for f in _selected(target)
                  if _function_id(f) not in target['unobserved_reasons']} for target in (left, right)]
    names = [defaultdict(list), defaultdict(list)]
    for indexed, named in zip(functions, names):
        for identifier, function in indexed.items():
            named[function['name']].append(identifier)
    pairs, failures = {}, []
    for a, b in requested.items():
        options = [[selector] if selector in indexed else named.get(selector, [])
                   for selector, indexed, named in zip((a, b), functions, names)]
        why = None
        if any(len(values) != 1 for values in options):
            why = 'Each selector must identify one comparable selected function'
        elif options[0][0] in pairs or options[1][0] in pairs.values():
            why = 'One-to-one mapping cannot repeat a function'
        if why:
            failures.append(dict(reference=a, candidate=b, reason=why,
                                 reference_options=options[0], candidate_options=options[1]))
        else:
            pairs[options[0][0]] = options[1][0]
    return pairs, failures


def _score_features(a, b, weights, counts):
    left, right = counts[0][a['id']], counts[1][b['id']]
    a_name, b_name = a['name'], b['name']
    known_name = not any(marker in a_name for marker in ('<lambda>', '<anonymous>'))
    score = weights['name'] * (known_name and a_name == b_name)
    common = sum(min(count, right.get(token, 0)) for token, count in left.items())
    total = a['features']['size'] + b['features']['size'] - common
    score += weights['tokens'] * common / max(total, 1)
    for key in ('size', 'branches', 'calls'):
        x, y = a['features'][key], b['features'][key]
        score += weights[key] * (1 - abs(x - y) / max(x, y, 1))
    return round(score, 6)


def _compare_features(left, right, weights, manual):
    groups = [{f['id']: f for f in _selected(evidence) if f['features'] is not None}
              for evidence in (left, right)]
    original, rebuilt = groups
    if not original or not rebuilt:
        return dict(score=None, denominator=0, score_status='unavailable', matches=[],
                    unmatched_original=list(original), unmatched_reimplementation=list(rebuilt),
                    candidate_search=dict(examined_pairs=0, total_possible_pairs=0, omitted_pairs=0))
    counts = [{identifier: row['features']['tokens'] for identifier, row in items.items()} for items in groups]
    used_a, used_b = set(manual), set(manual.values())
    matches = [dict(original=a, reimplementation=b, source='user_mapping', alternatives=[],
                    ambiguous=False, score=_score_features(original[a], rebuilt[b], weights, counts))
               for a, b in manual.items()]
    candidates = []
    for a in original.keys() - used_a:
        for b in rebuilt.keys() - used_b:
            score = _score_features(original[a], rebuilt[b], weights, counts)
            if score >= .5:
                candidates.append((score, a, b))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    by_left, by_right = defaultdict(list), defaultdict(list)
    for pair in candidates:
        by_left[pair[1]].append(pair)
        by_right[pair[2]].append(pair)
    for pair in candidates:
        score, a, b = pair
        if a in used_a or b in used_b:
            continue
        competitors = sorted((other for other in by_left[a] + by_right[b]
                              if other != pair and abs(score - other[0]) < .03),
                             key=lambda item: (-item[0], item[1], item[2]))
        alternatives = [dict(original=x, reimplementation=y, score=value, source='automatic')
                        for value, x, y in competitors[:5]]
        matches.append(dict(original=a, reimplementation=b, score=score, source='automatic',
                            ambiguous=bool(alternatives), alternatives=alternatives))
        used_a.add(a)
        used_b.add(b)
    denominator = max(len(original), len(rebuilt))
    possible = len(original) * len(rebuilt)
    examined = (len(original) - len(manual)) * (len(rebuilt) - len(manual)) + len(manual)
    return dict(score=sum(row['score'] for row in matches) / denominator, denominator=denominator,
                matches=matches, score_status='measured',
                unmatched_original=sorted(original.keys() - used_a),
                unmatched_reimplementation=sorted(rebuilt.keys() - used_b),
                candidate_search=dict(examined_pairs=examined, total_possible_pairs=possible,
                                      omitted_pairs=0, pairs_reserved_by_mapping=possible - examined))


def _algorithm(representation, profile, purpose):
    if representation == 'dotnet-metadata-cil':
        return dict(id='recheck-managed-member-identity-1', profile=profile, profile_reason=purpose,
                    formula='고유하게 대응된 선언 형식·이름·해석 시그니처 수 / 양쪽에서 더 많은 식별 가능 메서드 수',
                    score_kind='managed_identity_coverage', weights=None, profile_effect='selection_only')
    native = representation == 'native-instructions'
    weights = dict(NATIVE_DEFAULT_WEIGHTS if profile == 'default' else NATIVE_PORTING_WEIGHTS) if native else dict(SYNTAX_WEIGHTS[profile])
    return dict(id='recheck-native-functions-1' if native else 'recheck-function-features-1',
                profile=profile, profile_reason=purpose, weights=weights, score_kind='heuristic',
                formula=('가중합(이름 일치, CFG 노드 수비, CFG 간선 수비, 해결된 호출 수비, 명령 다중집합 Jaccard)'
                         if native else '가중합(이름 일치, 노드/명령 수비, 분기 구문/명령 수비, 호출 구문/명령 수비, 종류 다중집합 Jaccard)')
                        + '. 수비 = 1 − |a−b| / max(a,b,1). 대응 점수 합 / 양쪽 비교 가능 함수 수 중 큰 값.',
                automatic_minimum=.5, ambiguity_gap=.03, alternatives_limit=5)


def _compare_native(left, right, algorithm, pairs):
    def artifact(evidence):
        selected = _selected(evidence)
        complete = evidence['status'] == 'succeeded'
        return dict(analysis_kind='native', functions=selected, total_functions=len(selected) if complete else None,
                    complete=complete, status='success' if complete else 'partial')

    comparison = compare_static_artifacts(artifact(left), artifact(right),
                                          weights=None if algorithm['profile'] == 'default' else algorithm['weights'],
                                          manual_pairs=pairs)
    comparison['denominator'] = (max(left['scope']['comparable'], right['scope']['comparable'])
                                 if comparison['score'] is not None else 0)
    return comparison


def _compare_managed(left, right):
    groups = []
    for evidence in (left, right):
        identities = defaultdict(list)
        for f in _selected(evidence):
            if identity := managed_member_identity(f):
                identities[identity].append(f)
        groups.append(identities)
    comparison = compare_static_artifacts(*(dict(analysis_kind='managed', functions=[
        row for rows in group.values() for row in rows]) for group in groups))
    denominator = max(left['scope']['comparable'], right['scope']['comparable']) if all(groups) else 0
    if not denominator:
        comparison['score'] = None
    comparison.update(denominator=denominator, score_status='measured' if denominator else 'unavailable',
                      cil_observations=dict(Counter(row['cil_observation'] for row in comparison['matches'])),
                      ambiguous_identities=[dict(declaring_type=key[0], name=key[1], signature_sha256=key[2],
                                                original=[r['id'] for r in groups[0].get(key, [])],
                                                reimplementation=[r['id'] for r in groups[1].get(key, [])])
                                            for key in sorted(groups[0].keys() | groups[1].keys())
                                            if any(len(group.get(key, [])) > 1 for group in groups)],
                      candidate_search=dict(policy='exact declaring type, method name and decoded signature digest',
                                            examined_identity_keys=len(groups[0].keys() | groups[1].keys())))
    return comparison


def _compatible(left, right):
    if left['representation'] != right['representation']:
        return '양쪽 정적 표현을 함께 비교하는 검증된 프로필이 없습니다.'
    if left['runtime'] == 'native' and left['files'] and right['files']:
        files = (left['files'][0], right['files'][0])
        for key in ('format', 'os', 'architecture', 'byte_order', 'declared_abi'):
            if files[0].get(key) is not None and files[1].get(key) is not None and files[0][key] != files[1][key]:
                return '네이티브 파일 형식·OS·아키텍처·바이트 순서 또는 명시된 ABI가 다릅니다.'
    return None


def _compare(result, options):
    left, right = (result['targets'][side] for side in SIDES)
    result['limitations'] = list(dict.fromkeys(left['limitations'] + right['limitations'] + [PAIRING_LIMIT]))
    result['representation'] = left['representation'] if left['representation'] == right['representation'] else None
    if result['representation']:
        result['algorithm'] = _algorithm(result['representation'], options['profile'], options['profile_reason'])
    if any(item['status'] == 'cancelled' for item in (left, right)):
        result.update(status='cancelled', reason='정적 분석 취소')
        return
    if reason := _compatible(left, right):
        result.update(status='unsupported', reason=reason)
        return
    for state in ('failed', 'blocked', 'unsupported'):
        if any(item['status'] == state for item in (left, right)):
            result.update(status=state, reason=next(item['reason'] for item in (left, right) if item['status'] == state))
            return
    requested = options['function_map']
    if left['runtime'] == 'dotnet' and requested:
        result.update(status='unsupported', reason='.NET 메서드 비교는 선언 형식·이름·시그니처 자동 대응을 사용합니다.')
        return
    pairs, failures = _resolve_pairs(requested, left, right)
    if failures:
        result.update(status='failed', reason='사용자 제공 함수 매핑을 해석할 수 없습니다.', mapping_issues=failures)
        return
    if left['runtime'] == 'native':
        comparison = _compare_native(left, right, result['algorithm'], pairs)
    elif left['runtime'] == 'dotnet':
        comparison = _compare_managed(left, right)
        result['metric_name'] = '관리 메서드 식별 일치율'
    else:
        comparison = _compare_features(left, right, result['algorithm']['weights'], pairs)
    result['comparison'] = comparison
    for match in comparison['matches']:
        match['mapping_source'] = 'user' if match['source'] == 'user_mapping' else 'automatic'
    if comparison['score'] is None:
        result.update(status='unsupported', reason='양쪽에서 비교 가능하게 관측된 함수가 필요합니다.')
        return
    partial = (any(item['status'] == 'partial' or item['unobserved_reasons'] for item in (left, right))
               or comparison['score_status'] == 'partial')
    comparison['score_status'] = 'partial' if partial else 'measured'
    result.update(status='partial' if partial else 'succeeded',
                  reason='부분 관측 범위에서 계산했습니다.' if partial else None,
                  similarity=round(comparison['score'] * 100, 4))


def _parse_options(manifest):
    options = mapping(manifest.get('structure', {}), {'enabled', 'profile', 'profile_reason',
                                                      'function_map', 'mapping_notes', 'timeout_seconds'})
    profile = options.get('profile', 'default')
    if profile not in ('default', 'porting') or type(options.get('enabled', True)) is not bool:
        raise ValueError('구조 비교 profile은 default 또는 porting, enabled는 불리언이어야 합니다.')
    purpose = options.get('profile_reason', '기존 기본 가중치 유지' if profile == 'default' else
                          '포팅에서 이름 변경의 영향을 줄이고 관찰한 구조 특징의 비중을 높임')
    strings([purpose])
    if not purpose.strip():
        raise ValueError('구조 비교 profile_reason에는 변경 근거가 필요합니다.')
    manual = mapping(options.get('function_map', {}))
    strings(list(manual), 256)
    strings(list(manual.values()), 256)
    notes = options.get('mapping_notes', [])
    if not isinstance(notes, list) or len(notes) > 128:
        raise ValueError('구조 매핑 참고 사항은 최대 128개의 배열이어야 합니다.')
    return options | dict(profile=profile, profile_reason=purpose, function_map=manual, mapping_notes=notes)


async def compare_structure(manifest: dict, root: Path, *, workspace=None, settings=None,
                            cancel=None, execution_entries=None, captured_entries=None) -> dict:
    """Extract both targets, preserve evidence and return a JSON-compatible stage result."""
    result = dict(status='failed', reason=None, similarity=None, representation=None,
                  metric_name='구조 특징 기반 유사도', algorithm=None, targets={}, comparison=None)
    try:
        options = _parse_options(manifest)
        if not options.get('enabled', True):
            return result | dict(status='skipped', reason='명세에서 구조 분석을 끔')
        settings = settings if settings is not None else Settings()
        timeout = bounded(options.get('timeout_seconds', settings.static_timeout), 1, 3600)
        root = Path(root).resolve(strict=True)
        result.update(requested_mapping=options['function_map'], mapping_notes=options['mapping_notes'])
        if workspace is None:
            with TemporaryDirectory(prefix='recheck-porting-static-') as temporary:
                targets = await _extract_targets(manifest, root, Path(temporary), settings,
                                                 timeout, cancel, False, execution_entries, captured_entries)
        else:
            targets = await _extract_targets(manifest, root, Path(workspace).resolve(), settings,
                                             timeout, cancel, True, execution_entries, captured_entries)
        result['targets'] = dict(zip(SIDES, targets))
        _compare(result, options)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        result.update(status='failed', reason=f'{type(exc).__name__}: {exc}')
    return result


async def _extract_targets(manifest, root, workspace, settings, timeout, cancel, retained,
                           execution_entries, captured_entries):
    entries = execution_entries or {}
    captured = captured_entries or {}
    return await asyncio.gather(*(
        _extract(root, manifest['targets'][side], workspace / side, settings, timeout,
                 cancel, retained, entries.get(side), captured.get(side))
        for side in SIDES
    ))
