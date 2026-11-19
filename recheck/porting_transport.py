"""Controller side of the isolated worker protocol."""
from __future__ import annotations

import asyncio
import base64
import re
import shutil
import struct
import uuid
from pathlib import Path

from .adapters import DockerAdapter, _ok
from .porting_manifest import (
    MAX_ENTRY_CAPTURE, MAX_ENTRY_CAPTURE_B64, MAX_OBSERVATION, PLATFORMS, PROTOCOL,
    bounded, canonical, digest, identifier, json_loads, mapping,
)
from .process import run_process

IMAGE = re.compile(r'sha256:[a-f0-9]{64}\Z')
ENDPOINT = re.compile(r'(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*\Z')


class WorkerError(ValueError):
    def __init__(self, message, *, transport=None):
        super().__init__(message)
        self.transport = transport or {}


def _read_entry_capture(payload, expected_hash):
    if payload is None:
        return {'status': 'unavailable'}, None
    if isinstance(payload, dict) and payload.get('status') == 'size_limit':
        return {'status': 'size_limit', 'limit_bytes': MAX_ENTRY_CAPTURE}, None
    if isinstance(payload, dict) and payload.get('status') == 'available':
        encoded = payload.get('data_b64')
        if isinstance(encoded, str) and len(encoded) <= MAX_ENTRY_CAPTURE_B64:
            try:
                artifact = base64.b64decode(encoded, validate=True)
            except ValueError:
                pass
            else:
                if 0 < len(artifact) <= MAX_ENTRY_CAPTURE and digest(artifact) == expected_hash:
                    return {'status': 'verified', 'size_bytes': len(artifact), 'sha256': expected_hash}, artifact
    return {'status': 'invalid'}, None


def _reply_limit(request):
    return MAX_OBSERVATION + 65536 + (MAX_ENTRY_CAPTURE_B64 if request.get('capture_entry') else 0)


def load_workers(path):
    if not path:
        return {}
    with Path(path).open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise WorkerError('Worker 설정 크기가 64 KiB를 초과합니다.')
    config = mapping(json_loads(data.decode('utf-8-sig')), {'workers'})
    workers = {}
    for name, spec in mapping(config.get('workers')).items():
        identifier(name)
        mapping(spec, {'kind', 'os', 'arch', 'image', 'context', 'host', 'port',
                       'worker_id', 'allow_emulation'})
        if (spec.get('os'), spec.get('arch')) not in PLATFORMS:
            raise WorkerError('Worker OS·아키텍처가 올바르지 않습니다.')
        if type(spec.get('allow_emulation', False)) is not bool:
            raise WorkerError('allow_emulation은 불리언입니다.')
        if spec.get('kind') == 'docker':
            if spec['os'] != 'linux' or not IMAGE.fullmatch(spec.get('image', '')):
                raise WorkerError('Linux와 불변 Docker 이미지 ID가 필요합니다.')
            if spec.get('context'):
                identifier(spec['context'])
            workers[name] = DockerWorker(spec)
        elif spec.get('kind') == 'ssh':
            if not isinstance(spec.get('host'), str) or not ENDPOINT.fullmatch(spec['host']):
                raise WorkerError('SSH 호스트 별칭 또는 operator@host가 필요합니다.')
            bounded(spec.get('port', 22), 1, 65535)
            identifier(spec.get('worker_id'))
            workers[name] = SshWorker(spec)
        else:
            raise WorkerError('Worker는 docker 또는 ssh여야 합니다.')
    return workers


class Worker:
    def __init__(self, spec, worker_id, isolation, concurrency=1):
        self.spec = spec
        self.worker_id = worker_id
        self.isolation = isolation
        self.proof = None
        self.slots = asyncio.Semaphore(concurrency)

    async def _exchange(self, request, cancel):
        raise NotImplementedError

    def _check(self, request, reply):
        if not isinstance(reply, dict) or reply.get('protocol') != PROTOCOL or reply.get('job_id') != request['job_id']:
            raise WorkerError('Worker 응답의 프로토콜 또는 작업 ID가 다릅니다.')
        environment = reply.get('environment')
        expected = dict(id=self.worker_id, isolation=self.isolation,
                        os=self.spec['os'], arch=self.spec['arch'])
        if not isinstance(environment, dict) or any(environment.get(k) != v for k, v in expected.items()):
            raise WorkerError('Worker ID, 격리 종류 또는 실제 OS·아키텍처가 다릅니다.')
        if self.isolation != 'trusted-fixtures':
            code_hash = environment.get('worker_code_sha256')
            if not isinstance(code_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', code_hash):
                raise WorkerError('Worker 코드의 SHA-256 근거가 없습니다.')
            if self.proof and code_hash != self.proof['worker_code_sha256']:
                raise WorkerError('준비 단계와 실행의 worker 코드가 다릅니다.')
            if environment.get('emulated') is True and not self.spec.get('allow_emulation'):
                raise WorkerError('이 Worker의 에뮬레이션 사용을 명시적으로 허용해야 합니다.')
        if request['op'] == 'probe':
            if reply.get('status') != 'ready':
                raise WorkerError('Worker 준비 확인이 실패했습니다.')
            return environment
        if (reply.get('request_sha256') != digest(canonical(request))
                or reply.get('target_sha256') != request['bundle']['sha256']):
            raise WorkerError('Worker 응답의 요청 또는 대상 해시가 다릅니다.')
        row = reply.get('observation')
        state_unknown = isinstance(row, dict) and row.get('status') == 'oom' and (
            row.get('started') is None and row.get('process_executions') is None)
        if (not isinstance(row, dict) or row.get('target_sha256') != reply['target_sha256']
                or row.get('status') not in {'completed', 'blocked', 'cancelled', 'timed_out',
                    'output_truncated', 'observation_error', 'environment_error', 'unsupported', 'oom'}
                or (not state_unknown and (type(row.get('started')) is not bool
                    or type(row.get('process_executions')) is not int or row['process_executions'] < 0))
                or (row.get('exit_code') is not None and type(row['exit_code']) is not int)
                or not isinstance(row.get('observations'), dict)):
            raise WorkerError('Worker 응답의 관찰 스키마 또는 대상 해시가 다릅니다.')
        for stream in ('stdout_b64', 'stderr_b64'):
            if not isinstance(row.get(stream), str):
                raise WorkerError('Worker 응답에 원시 스트림이 없습니다.')
            base64.b64decode(row[stream], validate=True)
        result = dict(row, job_id=request['job_id'], environment=environment)
        result.pop('_entry_artifact', None)
        result.pop('entry_capture', None)
        if request.get('capture_entry') and result['started']:
            evidence, artifact = _read_entry_capture(reply.get('entry_capture'), result.get('entry_sha256'))
            result['entry_capture'] = evidence
            if artifact is not None:
                result['_entry_artifact'] = artifact
        return result

    async def run(self, request, cancel):
        async with self.slots:
            if cancel is not None and cancel.is_set():
                return {'status': 'cancelled', 'reason': 'cancelled_before_dispatch'}
            if (request['target']['os'], request['target']['arch']) != (self.spec['os'], self.spec['arch']):
                return {'status': 'unsupported', 'reason': 'configured_worker_platform_mismatch'}
            body = dict(request, protocol=PROTOCOL, op='run', job_id=uuid.uuid4().hex)
            active = body
            try:
                if self.proof is None:
                    active = dict(protocol=PROTOCOL, op='probe', job_id=uuid.uuid4().hex)
                    self.proof = self._check(active, await self._exchange(active, cancel))
                    active = body
                return self._check(body, await self._exchange(body, cancel))
            except (OSError, ValueError, RuntimeError) as exc:
                details = dict(exc.transport) if isinstance(exc, WorkerError) else {}
                details.update(operation=active['op'], job_id=active['job_id'],
                               request_sha256=digest(canonical(active)))
                if active['op'] == 'run':
                    details['submitted_target_sha256'] = active['bundle']['sha256']
                raise WorkerError(str(exc), transport=details) from exc


def _decode(result):
    if result.get('cancelled') or not _ok(result) or result.get('cleanup_errors'):
        details = {key: result.get(key) for key in (
            'returncode', 'cancelled', 'timed_out', 'truncated', 'cleanup_errors')}
        reason = result.get('error') or (result.get('stderr') or '').strip()
        if not reason:
            reason = ('cancelled' if details['cancelled'] else 'transport deadline exceeded' if details['timed_out']
                      else 'transport output limit exceeded' if details['truncated']
                      else 'transport cleanup unverified' if details['cleanup_errors']
                      else f"process exit status {details['returncode']}")
        raise WorkerError(f'Worker transport failed: {reason[:2000]}', transport=details)
    return json_loads(base64.b64decode(result['stdout_b64'], validate=True))


def _frame(request):
    body = canonical(request)
    return struct.pack('>I', len(body)) + body



class SshWorker(Worker):
    def __init__(self, spec):
        super().__init__(spec, spec['worker_id'], 'dedicated-vm')

    async def _exchange(self, request, cancel):
        executable = shutil.which('ssh')
        if not executable:
            raise WorkerError('OpenSSH CLI가 필요합니다.')
        argv = [executable, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5',
                '-o', 'ServerAliveCountMax=2', '-o', 'ClearAllForwardings=yes',
                '-p', str(self.spec.get('port', 22)), self.spec['host'], 'recheck-porting-worker']
        return _decode(await run_process(argv, stdin=_frame(request), cancel=cancel, keep_stdin_open=True,
                                         timeout=request.get('scenario', {}).get('timeout', 20) + 15,
                                         output_limit=_reply_limit(request)))


class DockerWorker(Worker):
    def __init__(self, spec):
        super().__init__(spec, spec['image'][7:], 'docker', concurrency=4)
        self.docker = DockerAdapter(context=spec.get('context', ''))
        self.engine = None

    async def _verify_image(self, cancel):
        probe = await self.docker.probe()
        if not probe.get('ready') or not probe.get('local_endpoint') or probe.get('os') != 'linux':
            raise WorkerError('연결 가능한 로컬 Linux Docker 엔진이 필요합니다.')
        expected = {'x64': 'amd64', 'arm64': 'arm64'}[self.spec['arch']]
        observed = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(probe.get('arch'), probe.get('arch'))
        emulated = observed != expected
        if emulated and not self.spec.get('allow_emulation'):
            raise WorkerError('Docker 엔진 아키텍처가 다릅니다. 에뮬레이션에는 명시적 설정이 필요합니다.')
        result = await self.docker.call('image', 'inspect', self.spec['image'], timeout=15, cancel=cancel)
        if not _ok(result):
            raise WorkerError('미리 준비한 불변 Docker 실행 이미지가 없습니다.')
        image = json_loads(result['stdout'])[0]
        if (image.get('Id'), image.get('Os'), image.get('Architecture')) != (
                self.spec['image'], 'linux', expected):
            raise WorkerError('Docker 이미지 ID 또는 플랫폼이 다릅니다.')
        self.engine = {'image_id': image['Id'], 'docker_version': probe.get('server_version'),
                       'host_arch': observed, 'emulated': emulated, 'image_arch': expected}

    async def _exchange(self, request, cancel):
        if self.engine is None:
            await self._verify_image(cancel)
        scenario = request.get('scenario', {})
        name = 'recheck-porting-' + request['job_id']
        complete = False
        create_timed_out = False
        try:
            if cancel is not None and cancel.is_set():
                raise WorkerError('Docker worker dispatch cancelled.')
            created = await self.docker.call(
                'create', '-i', '--name', name, '--label', 'org.recheck.purpose=porting-worker',
                '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid,nodev,size=128m',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '128',
                '--init', '--user', '65532:65532',
                '--memory', str(scenario.get('memory_mb', 512)) + 'm',
                '--memory-swap', str(scenario.get('memory_mb', 512)) + 'm',
                '--cpus', str(scenario.get('cpus', 1)), '--pull', 'never',
                '--platform', 'linux/' + self.engine['image_arch'],
                '--env', 'RECHECK_WORKER_ISOLATION=docker', '--env', 'RECHECK_WORKER_ID=' + self.worker_id,
                '--entrypoint', 'python', self.spec['image'], '-m', 'recheck.porting_worker',
                timeout=45)
            create_timed_out = created.get('timed_out', False)
            if not _ok(created):
                reason = (created.get('error') or created.get('stderr') or
                          ('create timeout' if create_timed_out else f'exit {created.get("returncode")}'))
                raise WorkerError(f'Docker worker 준비 실패 ({name}): {reason[:1000]}')
            result = await self.docker.call('start', '-ai', name, stdin=_frame(request),
                                            keep_stdin_open=True,
                                            timeout=scenario.get('timeout', 20) + 15, cancel=cancel,
                                            output_limit=_reply_limit(request))
            state = await self.docker.call('inspect', '--format', '{{json .State}}', name, timeout=8)
            if not _ok(state):
                raise WorkerError('Docker worker 종료 상태를 확인하지 못했습니다.')
            container_state = json_loads(state['stdout'])
            if container_state.get('OOMKilled') and request['op'] == 'run':
                target_sha = request['bundle']['sha256']
                reply = dict(protocol=PROTOCOL, job_id=request['job_id'],
                             request_sha256=digest(canonical(request)), target_sha256=target_sha,
                             environment=dict(self.proof),
                             observation=dict(status='oom', reason='docker_oom_killed',
                                              started=None, process_executions=None, observations={},
                                              stdout_b64='', stderr_b64='', target_sha256=target_sha))
            else:
                reply = _decode(result)
            if not isinstance(reply, dict) or not isinstance(reply.get('environment'), dict):
                raise WorkerError('Docker worker 응답에 실행 환경 근거가 없습니다.')
            reply['environment'].update(self.engine)
            complete = True
            return reply
        finally:
            removed = await self.docker.call('rm', '-f', name, timeout=10)
            if create_timed_out and not _ok(removed):
                # The daemon may finish an admitted create after the CLI deadline.
                for _ in range(3):
                    await asyncio.sleep(1)
                    removed = await self.docker.call('rm', '-f', name, timeout=10)
                    if _ok(removed):
                        break
            if not _ok(removed) and (complete or 'No such container' not in removed.get('stderr', '')):
                raise WorkerError(f'작업 컨테이너 정리를 확인하지 못했습니다 ({name}).')


class TrustedFixtures(Worker):
    """The repository verifier uses only repository-owned programs through this adapter."""

    def __init__(self):
        from .porting_worker import worker_platform

        self.environment = dict(worker_platform(), id='repository-test-fixtures', isolation='trusted-fixtures')
        super().__init__(self.environment, self.environment['id'], 'trusted-fixtures')

    async def _exchange(self, request, cancel):
        from .porting_worker import handle

        return await handle(request, self.environment, cancel=cancel)
