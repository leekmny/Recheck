import argparse
import asyncio
import json
import os
import signal
import re
import sys
import tempfile
import threading
from pathlib import Path
from .models import load_settings, inspect_binary
from .engine import Engine


class CLIError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class UsageError(CLIError):
    pass


class HelpRequest(Exception):
    def __init__(self, text):
        self.text = text


class SafeParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('allow_abbrev', False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError('invalid_arguments', '명령과 옵션 형식을 확인하세요.')

    def print_help(self, file=None):
        self._requested_help = self.format_help()

    def exit(self, status=0, message=None):
        if status == 0:
            raise HelpRequest(getattr(self, '_requested_help', self.format_help()))
        raise UsageError('invalid_arguments', '명령과 옵션 형식을 확인하세요.')


def extract_json_output(argv):
    """The output flag is global, including after nested subcommands."""
    result = []
    output = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == '--':
            result.extend(argv[index:])
            break
        if value == '--json-output' or value.startswith('--json-output='):
            if output is not None:
                raise CLIError('invalid_arguments', 'JSON 출력 파일은 한 번만 지정하세요.')
            if value == '--json-output':
                index += 1
                if index >= len(argv) or argv[index].startswith('--'):
                    raise CLIError('invalid_arguments', '새 JSON 출력 파일을 지정하세요.')
                output = argv[index]
            else:
                output = value.split('=', 1)[1]
            if not output or '\0' in output:
                raise CLIError('invalid_arguments', '새 JSON 출력 파일을 지정하세요.')
        else:
            result.append(value)
        index += 1
    return result, Path(output) if output is not None else None


def prepare_json_output(path):
    if path is None:
        return
    if path.exists() or path.is_symlink():
        raise CLIError('output_conflict', '기존 JSON 출력 파일을 덮어쓰지 않습니다.')
    path.parent.mkdir(parents=True, exist_ok=True)


def _stdout(text, fallback=None):
    if sys.stdout is None:
        return
    try:
        sys.stdout.write(text + '\n')
    except UnicodeEncodeError:
        sys.stdout.write(
            (fallback() if fallback else text.encode('ascii', 'backslashreplace').decode('ascii'))
            + '\n'
        )


def emit_json_result(result, path=None):
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if path is not None:
        path = Path(path)
        descriptor, temporary = tempfile.mkstemp(
            prefix='.recheck-json-', suffix='.tmp', dir=path.parent
        )
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(text + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            # Windows rename refuses an existing destination. POSIX link gives
            # the same atomic no-replacement contract without rename's overwrite.
            if os.name == 'nt':
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            raise CLIError('output_conflict', '기존 JSON 출력 파일을 덮어쓰지 않습니다.') from None
        finally:
            if temporary.exists():
                temporary.unlink()
    _stdout(text, lambda: json.dumps(result, ensure_ascii=True, indent=2, default=str))


def safe_error(error):
    from ai_providers.contracts import ProviderError

    code = getattr(error, 'code', '')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code):
        code = (
            'cli_io'
            if isinstance(error, OSError)
            else (
                'input_format' if isinstance(error, (ValueError, TypeError)) else 'operation_failed'
            )
        )
    message = (
        str(error)
        if isinstance(error, CLIError)
        else (
            '파일 접근에 실패했습니다.'
            if isinstance(error, OSError)
            else (
                'AI 제공자 작업을 완료하지 못했습니다.'
                if isinstance(error, ProviderError)
                else '입력 형식 또는 작업 상태를 확인하세요.'
            )
        )
    )
    return dict(status='failed', code=code, message=message)


def result_code(result):
    return (
        2
        if isinstance(result, dict)
        and str(result.get('status', '')).lower()
        in ('failed', 'blocked', 'partial', 'cancelled', 'error')
        else 0
    )


SETUP_DIAGNOSTICS = {
    'docker_not_installed': ('Docker Desktop을 찾지 못했습니다.',
                             ['도구 설치·관리에서 Docker Desktop을 설치하세요.',
                              'Docker Desktop을 실행한 뒤 분석 환경 준비를 다시 누르세요.']),
    'docker_engine_unavailable': ('Docker 엔진에 연결하지 못했습니다.',
                                  ['Docker Desktop을 실행하고 Linux 컨테이너가 준비될 때까지 기다리세요.',
                                   '선택한 Docker context와 연결 상태를 다시 확인하세요.']),
    'docker_remote_context': ('관리 분석 환경에는 로컬 Docker 연결이 필요합니다.',
                              ['분석 설정에서 로컬 Docker context를 선택하세요.',
                               'Docker Desktop의 연결 상태를 확인하고 환경 준비를 다시 실행하세요.']),
    'docker_linux_x64_required': ('Linux x64 Docker 환경이 필요합니다.',
                                 ['Docker Desktop을 Linux 컨테이너 모드로 전환하세요.',
                                  '로컬 엔진의 운영체제·아키텍처를 확인하세요.']),
    'runtime_prepare_failed': ('분석 환경을 준비하지 못했습니다.',
                               ['도구 설치·관리에서 Docker와 배포 파일의 준비 상태를 확인하세요.',
                                '최초 실패 기록을 보존하고 분석 환경 준비를 다시 실행하세요.']),
    'runtime_prepare_cancelled': ('분석 환경 준비를 중단했습니다.',
                                  ['취소 시점의 준비 상태를 확인하세요.', '필요할 때 분석 환경 준비를 다시 실행하세요.']),
}


def preparation_diagnostic(result):
    """Derive an allowlisted cause; never echo setup stderr or private paths."""
    if not result_code(result):
        return result
    environment = result.get('environment') if isinstance(result.get('environment'), dict) else {}
    if result.get('status') == 'cancelled':
        code = 'runtime_prepare_cancelled'
    elif environment.get('status') == 'not_installed':
        code = 'docker_not_installed'
    elif environment.get('ready') is False:
        code = 'docker_engine_unavailable'
    elif environment.get('local_endpoint') is False:
        code = 'docker_remote_context'
    elif (environment.get('os') not in (None, 'linux')
          or environment.get('arch') not in (None, 'amd64', 'x86_64')):
        code = 'docker_linux_x64_required'
    else:
        code = 'runtime_prepare_failed'
    message, actions = SETUP_DIAGNOSTICS[code]
    return dict(result, code=code, message=message, next_actions=list(actions))


def regression_result_summary(run):
    """CLI rechecks expose outcomes, not stored inputs, profiles or raw outputs."""
    regression = run.get('regression') or {}
    summary = {key: regression.get(key) for key in ('suite_id', 'source_run_id', 'reference_changed')}
    summary['policy'] = 'pair_equivalence'
    summary['counts'] = {key: value for key, value in (regression.get('counts') or {}).items()
                          if key in ('resolved', 'still_different', 'unmeasured') and type(value) is int}
    summary['case_results'] = [
        {key: row.get(key) for key in ('test_id', 'status', 'saved_input_reproduced')}
        for row in regression.get('case_results', []) if isinstance(row, dict)
    ]
    return dict(status=run.get('status'), run_id=run.get('run_id'),
                analysis_status=run.get('analysis_status'), regression=summary)


def public_result(result):
    if not result_code(result):
        return result
    code = (
        result.get('code')
        or result.get('error_code')
        or result.get('completion_reason')
        or 'operation_failed'
    )
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', code):
        code = 'operation_failed'
    summary = dict(
        status=result['status'],
        code=code,
        message='작업이 완료되지 않았습니다. 실패 단계와 상태 코드를 확인하세요.',
    )
    if code in SETUP_DIAGNOSTICS:
        summary['message'], actions = SETUP_DIAGNOSTICS[code]
        summary['next_actions'] = list(actions)
    if code == 'analysis_preflight_blocked':
        summary['message'] = '분석 시작 조건을 확인하세요. 분석 기록은 생성하지 않았습니다.'
        summary['next_actions'] = list(result.get('next_actions', []))
    for key in ('run_id', 'workflow_id', 'report', 'comparison_report', 'directory'):
        if isinstance(result.get(key), str):
            if key not in ('report', 'comparison_report') or Path(result[key]).is_file():
                summary[key] = result[key]
    if isinstance(result.get('analysis_result_status'), str):
        summary['analysis_result_status'] = result['analysis_result_status']
    if isinstance(result.get('analysis_status'), dict):
        summary['analysis_status'] = {key: value for key, value in result['analysis_status'].items()
                                      if key in ('static', 'dynamic', 'ai') and isinstance(value, str)}
    operation = result.get('last_operation')
    if isinstance(operation, dict):
        summary['operation'] = {key: value for key, value in operation.items()
                                if key in ('operation_id', 'kind', 'status', 'error_code') and isinstance(value, str)}
    if isinstance(result.get('regression'), dict):
        summary['regression'] = regression_result_summary(result)['regression']
    return summary


def load_json(path):
    path = Path(path)
    if path.stat().st_size > 64 * 1024 * 1024:
        raise CLIError('input_format', 'JSON 입력 파일의 크기 제한을 초과했습니다.')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _ai_result(args, cancel):
    from ai_providers.contracts import ProviderError, ProviderProfile
    from ai_providers.profiles import ProfileManager

    manager = ProfileManager()
    operation = args.ai_command
    if operation == 'profiles':
        return [profile.model_dump() for profile in manager.list_profiles()]
    if operation == 'import':
        path = Path(args.file)
        if path.stat().st_size > 65536:
            raise ProviderError('profile_configuration', '프로필 JSON은 64KB 이하여야 합니다')
        try:
            profile = ProviderProfile.model_validate_json(path.read_bytes())
        except ValueError:
            raise ProviderError(
                'profile_configuration', '프로필 형식을 확인하세요. API 키 값은 파일에 넣지 마세요'
            ) from None
        return manager.save_profile(profile).model_dump()
    if operation == 'select':
        return manager.select_profile(args.profile_id, role=args.role).model_dump()
    if operation == 'status':
        return manager.account_status(args.profile_id)
    if operation == 'delete':
        manager.delete_profile(args.profile_id)
        return {'deleted': True, 'profile_id': args.profile_id}
    if operation == 'login':
        return asyncio.run(
            manager.login(
                args.profile_id,
                {
                    'client_id': args.client_id,
                    'scopes': args.scope,
                    'google_project_id': args.project_id,
                },
                cancel=cancel,
            )
        )
    method = {
        'connection': 'probe_connection',
        'models': 'list_models',
        'test': 'probe_capabilities',
        'logout': 'logout',
    }[operation]
    result = asyncio.run(getattr(manager, method)(args.profile_id, cancel=cancel))
    return [model.model_dump() for model in result] if operation == 'models' else result


def _selected_settings(args):
    from ai_providers.contracts import ProviderError
    from ai_providers.profiles import resolve_profile

    settings = load_settings().model_copy(deep=True)
    values = settings.model_dump()
    if args.ai_profile:
        values['active_ai_profile'] = args.ai_profile
    for role in ('generation', 'review'):
        if getattr(args, role + '_profile', None):
            values['ai_role_profiles'][role] = getattr(args, role + '_profile', None)
    try:
        settings = type(settings).model_validate(values)
    except ValueError:
        raise ProviderError('profile_missing', '명시한 역할의 AI 프로필이 없습니다') from None
    if args.ai_key_env:
        profile = resolve_profile(settings)
        if profile.auth_mode != 'api_key':
            raise ProviderError(
                'credential_auth', '환경 키 참조는 API 키 인증 프로필에만 사용할 수 있습니다'
            )
        try:
            changed = type(profile).model_validate(
                {
                    **profile.model_dump(),
                    'credential_ref': 'env:' + args.ai_key_env,
                    'credential_origin': profile.origin,
                    'credential_version': 0,
                }
            )
        except ValueError:
            raise ProviderError(
                'credential_binding', '명시할 환경변수 이름 형식을 확인하세요'
            ) from None
        settings.ai_profiles = [
            changed if item.profile_id == profile.profile_id else item
            for item in settings.ai_profiles
        ]
    if args.command == 'command' or (args.command == 'analyze' and (
        args.ai_mode == 'agent' or (args.ai_mode is None and args.mode == 'agent')
    )):
        from ai_providers.profiles import ProfileManager

        proof = ProfileManager(settings=settings).cached_capabilities(settings.active_ai_profile)
        if proof:
            settings.capabilities = proof
    return settings


def _parser():
    parser = SafeParser(description='Recheck CLI (GUI와 동일 엔진)')
    parser.add_argument(
        '--json-output',
        help='기존 파일을 덮어쓰지 않는 새 UTF-8 JSON 출력 파일; 모든 명령 위치에서 사용 가능',
    )
    parser.add_argument('--data-dir')
    parser.add_argument('--settings-dir', help='설정·보안 저장소 폴더; 결과 폴더와 별도')
    parser.add_argument('--ai-profile', help='이번 실행의 분석 AI 프로필 ID; 저장하지 않음')
    parser.add_argument(
        '--ai-key-env', help='선택 API 프로필에 사용할 명시 환경변수 이름; 키 값 인자 금지'
    )
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('environment')
    sub.add_parser('tools', help='기본 도구 설치 상태만 확인; 설치·서비스 시작 없음')
    prepare_tools = sub.add_parser('prepare-tools', help='처음 사용할 분석 도구를 내려받아 Docker 환경 만들기')
    prepare_tools.add_argument('--build', action='store_true', required=True,
                               help='다운로드·Docker 이미지 만들기를 명시적으로 허용')
    prepare_tools.add_argument('--include-local-ai', action='store_true',
                               help='선택 사항: Ollama 실행 환경도 만들기; 모델은 내려받지 않음')
    ai = sub.add_parser('ai', help='AI 연결 관리; test 명령만 실제 추론·비용 발생 가능')
    ai_sub = ai.add_subparsers(dest='ai_command', required=True)
    ai_sub.add_parser('profiles')
    ai_import = ai_sub.add_parser('import')
    ai_import.add_argument('file', help='비밀값 없는 프로필 JSON')
    for operation in (
        'select',
        'status',
        'delete',
        'connection',
        'models',
        'test',
        'login',
        'logout',
    ):
        item = ai_sub.add_parser(operation)
        item.add_argument('profile_id')
        if operation == 'select':
            item.add_argument('--role', choices=['analysis'], default='analysis')
        if operation == 'login':
            item.add_argument('--client-id', default='')
            item.add_argument('--scope', action='append', default=[])
            item.add_argument('--project-id', default='')
    sub.add_parser('history')
    sub.add_parser('porting-capabilities', help='포팅 검증의 플랫폼·런타임·관찰 지원 범위')
    porting_result = sub.add_parser('porting-result', help='저장된 포팅 검증 JSON 읽기')
    porting_result.add_argument('run_id')
    for name in ('porting-run', 'porting-rerun'):
        porting = sub.add_parser(name, help='격리 worker에서 시나리오 포팅 검증')
        if name == 'porting-rerun':
            porting.add_argument('run_id')
            porting.add_argument('--scenario', action='append')
        porting.add_argument('manifest')
        porting.add_argument('--worker', required=True, help='관리자가 준비한 worker 설정 JSON')
        porting.add_argument('--approve-build', action='store_true', help='격리 worker에서 명세의 빌드 실행')
    regression_save = sub.add_parser('regression-save', help='저장된 불일치·미측정 입력을 회귀 테스트로 보관')
    regression_save.add_argument('run_id')
    regression_save.add_argument('--test-id', action='append', dest='test_ids')
    regression_save.add_argument('--name', default='')
    sub.add_parser('regression-list', help='보관한 문제 입력 목록; 새 실행 없음')
    regression_run = sub.add_parser('regression-run', help='보관한 입력으로 새 동적분석; AI 호출 없음')
    regression_run.add_argument('suite_id')
    regression_run.add_argument('--original', required=True)
    regression_run.add_argument('--reimplementation', required=True)
    failure = sub.add_parser('failure-report', help='저장된 실패·차이 항목만 별도 보고서로 저장; 새 실행 없음')
    failure.add_argument('run_id')
    failure.add_argument('--output', required=True)
    failure.add_argument('--format', choices=('html', 'json'), default='html')
    failure.add_argument('--include-observations', action='store_true',
                         help='입력·출력 원문 포함; 민감 정보가 있을 수 있으므로 공유 전 검토')
    inspect = sub.add_parser('inspect')
    inspect.add_argument('path')
    package = sub.add_parser('compare-package', help='ZIP 구성 파일 비교; 추출·실행·AI 호출 없음')
    package.add_argument('--original', required=True)
    package.add_argument('--reimplementation', required=True)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--original', required=True)
    analyze.add_argument('--reimplementation', required=True)
    analyze.add_argument('--tests')
    analyze.add_argument(
        '--mode', choices=['automatic', 'agent', 'static', 'dynamic'], default='automatic'
    )
    analyze.add_argument('--analysis-mode', choices=['static', 'dynamic', 'both'],
                         help='분석 범위; 미지정 시 기존 --mode 선택을 따릅니다')
    analyze.add_argument('--ai-mode', choices=['off', 'explain', 'agent'],
                         help='AI 설명을 분석 범위와 별도로 선택; off는 AI를 호출하지 않습니다')
    for name in ('reinterpret', 'recover', 'report', 'diagnostic-zip'):
        p = sub.add_parser(name)
        p.add_argument('run_id')
    question = sub.add_parser('question')
    question.add_argument('run_id')
    question.add_argument('question')
    command_parser = sub.add_parser('command')
    command_parser.add_argument('run_id')
    command_parser.add_argument('instruction')
    for name in ('reconstruct', 'modernize'):
        workflow = sub.add_parser(name)
        workflow.add_argument('--original', required=True)
        workflow.add_argument('--tests', required=True)
        workflow.add_argument('--requirements', required=True)
        workflow.add_argument('--source', required=name == 'modernize')
        workflow.add_argument('--language', choices=['c', 'cpp'], default='c')
        workflow.add_argument('--approve-build', action='store_true')
        workflow.add_argument('--max-repairs', type=int, choices=range(3), default=1)
    sub.add_parser('workflows')
    workflow_info = sub.add_parser('workflow')
    workflow_info.add_argument('workflow_id')
    return parser


def _execute(args, cancel):
    if args.command == 'porting-capabilities':
        from .porting import capability_matrix

        return capability_matrix()
    if args.command in ('regression-save', 'regression-list'):
        from .regression import RegressionLibrary

        library = RegressionLibrary(args.data_dir)
        if args.command == 'regression-list':
            return library.list_suites()
        suite = library.save_from_run(args.run_id, test_ids=args.test_ids, name=args.name)
        return dict(status='success', suite_id=suite['suite_id'], name=suite['name'],
                    source_run_id=suite['source_run_id'], case_count=len(suite['tests']))
    if args.command == 'failure-report':
        from .storage import Store
        from .failure_report import build_failure_report, export_failure_report

        run = Store(args.data_dir).load_run(args.run_id)
        path = export_failure_report(run, args.output, format=args.format,
                                     include_observations=args.include_observations)
        summary = build_failure_report(run)
        return dict(status='success', run_id=args.run_id, report=str(path),
                    issue_count=summary['issue_count'], counts=summary['counts'],
                    observations_included=args.include_observations)
    if args.command == 'compare-package':
        from .package_comparison import compare_packages

        return compare_packages(args.original, args.reimplementation, cancel=cancel)
    if args.command == 'tools':
        from .prerequisites import inspect_prerequisites

        return asyncio.run(inspect_prerequisites(cancel=cancel))
    if args.command == 'prepare-tools':
        from .runtime_source import build_analysis_environment

        return preparation_diagnostic(asyncio.run(build_analysis_environment(
            _selected_settings(args), cancel=cancel, include_ollama=args.include_local_ai,
        )))
    if args.command == 'ai':
        return _ai_result(args, cancel)
    if args.command == 'inspect':
        return inspect_binary(args.path)
    engine = Engine(_selected_settings(args), data_dir=args.data_dir)
    command = args.command
    if command == 'porting-result':
        result = engine.store.load_run(args.run_id)
        if result.get('mode') != 'porting':
            raise CLIError('wrong_run_kind', '포팅 검증 이력이 필요합니다.')
    elif command in ('porting-run', 'porting-rerun'):
        try:
            result = asyncio.run(engine.porting(
                args.manifest, args.worker, cancel=cancel, allow_build=args.approve_build,
                previous_run=getattr(args, 'run_id', None), scenario_ids=getattr(args, 'scenario', None),
            ))
        except ValueError as exc:
            raise CLIError('porting_input', str(exc)) from exc
    elif command == 'environment':
        result = asyncio.run(engine.environment(cancel))
    elif command == 'history':
        result = engine.store.list_runs()
    elif command == 'regression-run':
        preflight = asyncio.run(engine.analysis_preflight(
            args.original, args.reimplementation, analysis_mode='dynamic', cancel=cancel))
        if preflight['status'] != 'ready':
            return preflight
        run = asyncio.run(engine.recheck_regression(args.suite_id, args.original, args.reimplementation, cancel))
        result = regression_result_summary(run)
    elif command == 'analyze':
        scope = args.analysis_mode or (args.mode if args.mode in ('static', 'dynamic') else 'both')
        if args.analysis_mode is not None and scope != 'static' and not args.tests:
            raise CLIError('tests_required', '동적 분석에는 --tests로 등록한 입력이 필요합니다.')
        tests = (
            load_json(args.tests)
            if args.tests
            else [] if scope == 'static' else [dict(
                id='baseline', description='입력 없는 기본 실행', args=[], stdin='', source='user',
            )]
        )
        preflight = asyncio.run(engine.analysis_preflight(
            args.original, args.reimplementation, analysis_mode=scope, cancel=cancel))
        if preflight['status'] != 'ready':
            return preflight
        result = asyncio.run(
            engine.analyze(args.original, args.reimplementation, tests, args.mode, cancel,
                           analysis_mode=args.analysis_mode, ai_mode=args.ai_mode)
        )
    elif command == 'question':
        result = asyncio.run(engine.question(args.run_id, args.question, cancel))
    elif command == 'command':
        result = asyncio.run(engine.command(args.run_id, args.instruction, cancel))
    elif command == 'workflows':
        result = engine.store.list_workflows()
    elif command == 'workflow':
        result = engine.store.load_workflow(args.workflow_id)
    elif command in ('reconstruct', 'modernize'):
        from .reconstruction import ReconstructionService

        tests = json.loads(Path(args.tests).read_text(encoding='utf-8-sig'))
        result = asyncio.run(
            ReconstructionService(engine.settings, data_dir=args.data_dir).run(
                args.original,
                tests,
                args.requirements,
                mode='modernization' if command == 'modernize' else 'reconstruction',
                source_path=args.source,
                language=args.language,
                approve_build=args.approve_build,
                max_repairs=args.max_repairs,
                cancel=cancel,
            )
        )
    elif command in ('recover', 'reinterpret'):
        result = asyncio.run(getattr(engine, command)(args.run_id, cancel))
        if command == 'reinterpret' and isinstance(result.get('last_operation'), dict):
            operation = result['last_operation']
            result = dict(result, analysis_result_status=result['status'], status=operation['status'])
            if operation.get('error_code'):
                result['code'] = operation['error_code']
    elif command == 'report':
        result = str(engine.report(args.run_id))
    else:
        result = str(engine.diagnostic_zip(args.run_id))
    return result


def main(argv=None):
    output = None
    handler = None
    parse_failure = False
    try:
        args, output = extract_json_output(list(sys.argv[1:] if argv is None else argv))
        prepare_json_output(output)
        args = _parser().parse_args(args)
        if args.settings_dir:
            os.environ['RECHECK_DATA_DIR'] = str(Path(args.settings_dir).resolve())
        cancel = threading.Event()
        if threading.current_thread() is threading.main_thread():
            handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: cancel.set())
        result = _execute(args, cancel)
        code = result_code(result)
        # Missing prerequisites are the reason for this diagnostic command: keep
        # its safe path/version rows visible even when readiness is partial.
        emit_json_result(result if args.command in (
            'tools', 'compare-package', 'porting-run', 'porting-rerun', 'porting-result')
                         else public_result(result), output)
        return code
    except HelpRequest as help_request:
        if output is not None:
            emit_json_result({'status': 'success', 'help': help_request.text}, output)
        else:
            _stdout(help_request.text.rstrip())
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        result = dict(status='cancelled', code='cancelled', message='작업이 취소되었습니다.')
    except Exception as exc:
        parse_failure = isinstance(exc, UsageError)
        result = safe_error(exc)
        if result['code'] == 'output_conflict':
            output = None
    finally:
        if handler is not None:
            signal.signal(signal.SIGINT, handler)
    try:
        emit_json_result(result, output)
    except (OSError, CLIError) as exc:
        emit_json_result(safe_error(exc))
    if parse_failure:
        raise SystemExit(2)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
