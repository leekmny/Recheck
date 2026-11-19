"""Versioned porting manifests and bounded, content-addressed input bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import stat
from pathlib import Path

SCHEMA = 'recheck-porting-1'
PROTOCOL = 'recheck-porting-worker-1'
SIDES = ('reference', 'candidate')
PLATFORMS = {(os_name, arch) for os_name in ('windows', 'linux', 'macos')
             for arch in ('x64', 'arm64')}
RUNTIMES = {'native', 'python', 'node', 'typescript', 'java', 'dotnet'}
KINDS = {'console', 'http', 'browser', 'desktop', 'library'}
MAX_BUNDLE = 16 * 1024 * 1024
MAX_MESSAGE = 32 * 1024 * 1024
MAX_OBSERVATION = 4 * 1024 * 1024
MAX_ENTRY_CAPTURE = 8 * 1024 * 1024
MAX_ENTRY_CAPTURE_B64 = 4 * ((MAX_ENTRY_CAPTURE + 2) // 3)
BUILT_ENTRY_RUNTIMES = {'native', 'dotnet', 'java'}
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}\Z')
DEVICE = re.compile(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])\Z', re.I)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'중복 JSON 키 {key!r}')
        result[key] = value
    return result


def _invalid_number(value):
    raise ValueError(f'유한 JSON 숫자가 필요합니다 ({value})')


def _finite_float(value):
    number = float(value)
    return number if math.isfinite(number) else _invalid_number(value)


def json_loads(value):
    return json.loads(value, object_pairs_hook=_unique, parse_constant=_invalid_number,
                      parse_float=_finite_float)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def relative(value, *, dot=False):
    if dot and value == '.':
        return value
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('대상 경로는 500자 이하의 상대 경로여야 합니다.')
    for part in value.split('/'):
        if (not part or part in {'.', '..'} or part[-1] in '. '
                or any(char in '<>:"\\|?*' or ord(char) < 32 for char in part)
                or DEVICE.fullmatch(part.split('.')[0])):
            raise ValueError(f'지원하지 않는 상대 경로 {value!r}')
    return value


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f'잘못된 식별자 {value!r}')
    return value


def mapping(value, fields=None):
    if not isinstance(value, dict):
        raise ValueError('JSON 객체가 필요합니다.')
    if fields is not None and (unknown := value.keys() - fields):
        raise ValueError(f'알 수 없는 필드 {sorted(unknown)}')
    return value


def strings(value, limit=128):
    if (not isinstance(value, list) or len(value) > limit
            or any(not isinstance(x, str) or '\0' in x or len(x) > 8192 for x in value)):
        raise ValueError('제한된 문자열 배열이 필요합니다.')
    return value


def bounded(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{minimum}–{maximum} 범위의 정수가 필요합니다.')
    return value


def environment(value):
    for key, item in mapping(value).items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', key) or key.upper() in {
            'PATH', 'PATHEXT', 'SYSTEMROOT', 'COMSPEC', 'HOME', 'USERPROFILE', 'TMP', 'TEMP',
            'TMPDIR', 'NODE_OPTIONS', 'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', 'PYTHONPATH',
            'PYTHONHOME', 'DOTNET_STARTUP_HOOKS',
        } or key.upper().startswith(('LD_', 'DYLD_', 'RECHECK_', 'SSH_')):
            raise ValueError(f'허용되지 않는 대상 환경 변수 {key!r}')
        strings([item])


def target_spec(target):
    mapping(target, {'entry', 'files', 'runtime', 'os', 'arch', 'worker', 'language',
                     'metadata', 'runtime_version', 'args', 'env', 'cwd', 'build',
                     'static', 'java_main', 'java_classpath'})
    relative(target.get('entry'))
    for path in strings(target.get('files', []), 512):
        relative(path)
    relative(target.get('cwd', '.'), dot=True)
    platform = (target.get('os'), target.get('arch'))
    if not all(isinstance(value, str) for value in platform) or platform not in PLATFORMS:
        raise ValueError('대상 OS·아키텍처가 지원 범위에 없습니다.')
    runtime = target.get('runtime')
    if not isinstance(runtime, str) or runtime not in RUNTIMES:
        raise ValueError('대상 런타임이 지원 범위에 없습니다.')
    identifier(target.get('worker', 'default'))
    strings(target.get('args', []))
    environment(target.get('env', {}))
    mapping(target.get('static', {}))
    mapping(target.get('metadata', {}))
    for key in ('runtime_version', 'language', 'java_main'):
        if key in target:
            strings([target[key]])
    if 'java_main' in target:
        if not re.fullmatch(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*', target['java_main']):
            raise ValueError('JVM 클래스 이름이 잘못되었습니다.')
    for path in strings(target.get('java_classpath', [])):
        relative(path, dot=True)
    if 'build' in target:
        build = mapping(target['build'], {'tool', 'args'})
        if build.get('tool') not in ('g++', 'clang++'):
            identifier(build.get('tool'))
        strings(build.get('args', []))
    if runtime == 'typescript' and 'build' not in target:
        raise ValueError('TypeScript에는 JavaScript 산출물 entry와 명시적 build가 필요합니다.')


def validate_scenario(scenario):
    mapping(scenario, {'id', 'description', 'kind', 'input', 'fixtures', 'steps', 'prepare',
                       'cleanup', 'expected', 'policy', 'mapping', 'repetitions', 'required',
                       'timeout', 'output_limit', 'memory_mb', 'cpus', 'base_url'})
    identifier(scenario.get('id'))
    kind = scenario.get('kind', 'console')
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('알 수 없는 실행 방식입니다.')
    data = mapping(scenario.get('input', {}), {'args', 'stdin', 'stdin_b64', 'env'})
    strings(data.get('args', []))
    environment(data.get('env', {}))
    if 'stdin' in data and 'stdin_b64' in data:
        raise ValueError('stdin과 stdin_b64는 함께 지정할 수 없습니다.')
    for key in ('stdin', 'stdin_b64'):
        if key in data:
            strings([data[key]])
    if 'stdin_b64' in data:
        base64.b64decode(data['stdin_b64'], validate=True)
    for path in strings(scenario.get('fixtures', []), 512):
        relative(path)
    for key, default, maximum in [('timeout', 20, 180), ('output_limit', 262144, 1048576),
                                   ('memory_mb', 512, 8192), ('cpus', 1, 8),
                                   ('repetitions', 1, 5)]:
        bounded(scenario.get(key, default), 1 if key != 'memory_mb' else 32, maximum)
    if type(scenario.get('required', True)) is not bool:
        raise ValueError('required는 불리언이어야 합니다.')
    step_ids = set()
    for phase in ('prepare', 'steps', 'cleanup'):
        steps = scenario.get(phase, [])
        if not isinstance(steps, list) or len(steps) > 64:
            raise ValueError('단계 배열의 상한은 64개입니다.')
        for step in steps:
            mapping(step)
            step_id = identifier(step.get('id'))
            identifier(step.get('action'))
            if step_id in step_ids:
                raise ValueError(f'중복 단계 {step_id!r}')
            step_ids.add(step_id)
            if phase != 'steps' and step['action'] not in {'files', 'sqlite'}:
                raise ValueError('준비·정리에는 파일·SQLite 스냅샷을 지정할 수 있습니다.')
    if kind == 'library' and not any(step['action'] == 'call' for step in scenario.get('steps', [])):
        raise ValueError('라이브러리 실행 단계(steps)에 call을 한 개 이상 지정해야 합니다.')
    for side, overrides in mapping(scenario.get('mapping', {}), set(SIDES)).items():
        mapping(overrides, {'reason', 'arg_order', 'arg_prefix', 'base_url', 'steps'})
        if not isinstance(overrides.get('reason'), str) or not overrides['reason'].strip():
            raise ValueError(f'{side} 매핑에 의미 보존 근거가 필요합니다.')
        strings(overrides.get('arg_prefix', []))
        if 'arg_order' in overrides:
            order = overrides['arg_order']
            if (not isinstance(order, list) or any(type(x) is not int for x in order)
                    or sorted(order) != list(range(len(data.get('args', []))))):
                raise ValueError('arg_order는 논리 인자 전체의 순열이어야 합니다.')
        for fields in mapping(overrides.get('steps', {}), step_ids).values():
            mapping(fields, {'path', 'paths', 'locator', 'url', 'selector', 'window', 'port'})
    for expectation in mapping(scenario.get('expected', {}), set(SIDES)).values():
        mapping(expectation)
    mapping(scenario.get('policy', {}))


def load_manifest(path):
    path = Path(path).resolve(strict=True)
    with path.open('rb') as stream:
        content = stream.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        raise ValueError('명세 크기 상한은 1 MiB입니다.')
    manifest = mapping(json_loads(content.decode('utf-8-sig')),
                       {'schema_version', 'targets', 'scenarios', 'structure',
                        'concurrency', 'description', 'uncontrolled'})
    if manifest.get('schema_version') != SCHEMA:
        raise ValueError(f'명세 schema_version은 {SCHEMA}이어야 합니다.')
    targets = mapping(manifest.get('targets'), set(SIDES))
    if targets.keys() != set(SIDES):
        raise ValueError('reference와 candidate가 모두 필요합니다.')
    for target in targets.values():
        target_spec(target)
    scenarios = manifest.get('scenarios')
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 200:
        raise ValueError('시나리오 수는 1–200개입니다.')
    seen = set()
    for scenario in scenarios:
        validate_scenario(scenario)
        if scenario['id'] in seen:
            raise ValueError(f'중복 시나리오 {scenario["id"]!r}')
        seen.add(scenario['id'])
    bounded(manifest.get('concurrency', 1), 1, 8)
    mapping(manifest.get('structure', {}))
    canonical(manifest)
    return manifest, path.parent


def safe_source(root, name):
    path = root
    for part in name.split('/'):
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'심볼릭 링크 또는 reparse point는 허용되지 않습니다 ({name})')
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'일반 파일이 필요합니다 ({name})')
    return path


def bundle_identity(files):
    hashes = {}
    folded = set()
    size = 0
    if not 1 <= len(files) <= 512:
        raise ValueError('전달 파일 수는 1–512개입니다.')
    for name, data in sorted(files.items()):
        relative(name)
        if name.casefold() in folded:
            raise ValueError(f'대소문자가 충돌하는 경로 {name!r}')
        folded.add(name.casefold())
        size += len(data)
        if size > MAX_BUNDLE:
            raise ValueError('전달 파일 전체 크기 상한은 16 MiB입니다.')
        hashes[name] = digest(data)
    return {'sha256': digest(canonical(hashes)), 'file_hashes': hashes}


def pack_target(root: Path, target, fixtures):
    # A generated entry is never read from a prior build by default.
    names = dict.fromkeys([*target.get('files', []), *fixtures,
                           *([] if 'build' in target else [target['entry']])])
    files = {}
    size = 0
    for name in names:
        path = safe_source(root, relative(name))
        with path.open('rb') as stream:
            files[name] = stream.read(MAX_BUNDLE - size + 1)
        size += len(files[name])
        if size > MAX_BUNDLE:
            raise ValueError('전달 파일 전체 크기 상한은 16 MiB입니다.')
    identity = bundle_identity(files)
    return dict(identity, files={name: base64.b64encode(data).decode('ascii')
                                 for name, data in files.items()})


def applied_scenario(scenario, side):
    data = scenario.get('input', {})
    override = scenario.get('mapping', {}).get(side, {})
    logical = data.get('args', [])
    order = override.get('arg_order', list(range(len(logical))))
    steps = override.get('steps', {})
    return {
        'args': [*override.get('arg_prefix', []), *(logical[index] for index in order)],
        'stdin_b64': data.get('stdin_b64', base64.b64encode(
            data.get('stdin', '').encode('utf-8')).decode('ascii')),
        'env': data.get('env', {}),
        'base_url': override.get('base_url', scenario.get('base_url')),
        **{phase: [dict(step, **steps[step['id']]) if step['id'] in steps else dict(step)
                   for step in scenario.get(phase, [])] for phase in ('prepare', 'steps', 'cleanup')},
        'mapping': override,
    }
